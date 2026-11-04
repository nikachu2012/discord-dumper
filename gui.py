"""ブラウザで操作する GUI。`python3 discord_dumper.py gui` で起動する。

127.0.0.1 だけで待ち受け、起動時に表示される鍵付き URL を開いたブラウザだけが操作できる。
トークンはこのプロセスのメモリにだけ保持し、ディスクには書かない。
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import webbrowser
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import discord_dumper as dd
from render import CHANNEL_ICONS, avatar_url, display_name, dm_label, read_json

ADMINISTRATOR = 1 << 3
VIEW_CHANNEL = 1 << 10
READ_MESSAGE_HISTORY = 1 << 16
LISTED_TYPES = dd.MESSAGE_CHANNEL_TYPES | dd.FORUM_TYPES
VOICE_TYPES = {dd.GUILD_VOICE, dd.GUILD_STAGE_VOICE}


# ---------------------------------------------------------------- permissions


def can_read(
    base: int | None,
    ch: dict[str, Any],
    guild_id: str,
    user_id: str,
    roles: list[str] | None,
) -> bool | None:
    """チャンネルを読めるか。判定できないときは None。"""
    if base is None:
        return None
    if base & ADMINISTRATOR:
        return True
    overwrites = {o["id"]: o for o in ch.get("permission_overwrites", [])}
    perms = base
    if o := overwrites.get(guild_id):  # @everyone
        perms = (perms & ~int(o["deny"])) | int(o["allow"])
    if roles is None:
        return None
    allow = deny = 0
    for role_id in roles:
        if o := overwrites.get(role_id):
            allow |= int(o["allow"])
            deny |= int(o["deny"])
    perms = (perms & ~deny) | allow
    if o := overwrites.get(user_id):
        perms = (perms & ~int(o["deny"])) | int(o["allow"])
    need = VIEW_CHANNEL if ch["type"] in dd.FORUM_TYPES else VIEW_CHANNEL | READ_MESSAGE_HISTORY
    return perms & need == need


# ---------------------------------------------------------------- saved state


def saved_states(parent: Path) -> dict[str, dict[str, Any]]:
    """`<名前>_<id>/state.json` を ID ごとにまとめる。"""
    out: dict[str, dict[str, Any]] = {}
    if not parent.is_dir():
        return out
    for d in parent.iterdir():
        m = re.search(r"_(\d+)$", d.name)
        if not m or not d.is_dir():
            continue
        st = read_json(d / "state.json") or {}
        threads = sum(1 for _ in (d / "threads").glob("*/state.json")) if (d / "threads").is_dir() else 0
        if st or threads:
            out[m[1]] = {
                "count": st.get("count"),
                "updated_at": st.get("updated_at"),
                "threads": threads,
            }
    return out


def find_dir(parent: Path, entity_id: str) -> Path | None:
    if parent.is_dir():
        for p in parent.glob(f"*_{entity_id}"):
            if p.is_dir():
                return p
    return None


# ------------------------------------------------------------------------ app


class Job:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.running = True
        self.cancelled = False
        self.error: str | None = None
        self.stop = threading.Event()
        self._lock = threading.Lock()

    def log(self, msg: str) -> None:
        with self._lock:
            self.lines.append(msg)

    def since(self, n: int) -> list[str]:
        with self._lock:
            return self.lines[n:]


class App:
    def __init__(self, out: Path, token: str | None, bot: bool, delay: float):
        self.secret = secrets.token_urlsafe(24)
        self.out = out
        self.bot = bot
        self.delay = delay
        self.api: dd.DiscordApi | None = None
        self.me: dict[str, Any] | None = None
        self.job: Job | None = None
        self.hosts: set[str] = set()
        self._lock = threading.Lock()
        self._guilds: list[dict[str, Any]] | None = None
        self._channels: dict[str, list[dict[str, Any]]] = {}
        self._roles: dict[str, list[str] | None] = {}
        if token:
            try:
                self.login(token)
            except dd.ApiError as e:
                dd.log(f"トークンでログインできませんでした: {e}")

    # ---- auth

    def login(self, token: str) -> None:
        api = dd.DiscordApi(token.strip(), bot=self.bot, delay=self.delay)
        me = api.get("/users/@me")
        with self._lock:
            self.api, self.me = api, me
            self._guilds, self._channels, self._roles = None, {}, {}

    def logout(self) -> None:
        with self._lock:
            self.api = self.me = None
            self._guilds, self._channels, self._roles = None, {}, {}

    def require_api(self) -> dd.DiscordApi:
        if not self.api:
            raise dd.ApiError(401, {"message": "ログインしていません"})
        return self.api

    def set_out(self, value: str | None) -> None:
        if value and value.strip():
            self.out = Path(value.strip()).expanduser()

    def result_path(self) -> Path:
        return self.out.resolve() / "index.html"

    def open_result(self, out: str | None) -> dict[str, str]:
        """ダンプの index.html をローカルファイルとして既定のブラウザで開く。"""
        self.set_out(out)
        index = self.result_path()
        if not index.is_file():
            raise ValueError(f"まだダンプされていません ({index})")
        webbrowser.open(index.as_uri())
        return {"path": str(index)}

    # ---- listing

    def guilds(self, refresh: bool) -> list[dict[str, Any]]:
        api = self.require_api()
        if self._guilds is None or refresh:
            self._guilds = api.get("/users/@me/guilds")
        saved = saved_states(self.out / "guilds")
        return [
            {
                "id": g["id"],
                "name": g["name"],
                "icon": f"{dd.render.CDN}/icons/{g['id']}/{g['icon']}.png?size=64" if g.get("icon") else None,
                "saved": g["id"] in saved or find_dir(self.out / "guilds", g["id"]) is not None,
            }
            for g in self._guilds
        ]

    def _member_roles(self, guild_id: str) -> list[str] | None:
        if guild_id in self._roles:
            return self._roles[guild_id]
        api = self.require_api()
        roles = None
        for path in (f"/users/@me/guilds/{guild_id}/member", f"/guilds/{guild_id}/members/{self.me['id']}"):
            try:
                roles = api.get(path).get("roles", [])
                break
            except dd.ApiError:
                continue
        self._roles[guild_id] = roles
        return roles

    def guild_channels(self, guild_id: str, refresh: bool) -> dict[str, Any]:
        api = self.require_api()
        if guild_id not in self._channels or refresh:
            self._channels[guild_id] = api.get(f"/guilds/{guild_id}/channels")
            self._roles.pop(guild_id, None)
        channels = self._channels[guild_id]
        if self._guilds is None:
            self.guilds(False)
        guild = next((g for g in self._guilds or [] if g["id"] == guild_id), {})
        base = int(guild["permissions"]) if guild.get("permissions") is not None else None
        roles = self._member_roles(guild_id) if base is not None and not base & ADMINISTRATOR else []
        gdir = find_dir(self.out / "guilds", guild_id)
        saved = saved_states(gdir) if gdir else {}

        cats = {c["id"]: c for c in channels if c["type"] == dd.GUILD_CATEGORY}
        groups: dict[str | None, list[dict[str, Any]]] = {}
        for ch in channels:
            if ch["type"] not in LISTED_TYPES:
                continue
            groups.setdefault(ch.get("parent_id") if ch.get("parent_id") in cats else None, []).append(ch)

        def channel_view(ch: dict[str, Any]) -> dict[str, Any]:
            return {
                "id": ch["id"],
                "name": ch.get("name", ""),
                "icon": CHANNEL_ICONS.get(ch["type"], "#"),
                "forum": ch["type"] in dd.FORUM_TYPES,
                "readable": can_read(base, ch, guild_id, self.me["id"], roles),
                "saved": saved.get(ch["id"]),
            }

        def order(ch: dict[str, Any]) -> tuple[int, int, int]:
            return (1 if ch["type"] in VOICE_TYPES else 0, ch.get("position", 0), int(ch["id"]))

        result = []
        for cat_id in [None, *sorted(cats, key=lambda c: (cats[c].get("position", 0), int(c)))]:
            if cat_id not in groups:
                continue
            result.append({
                "id": cat_id or "",
                "name": cats[cat_id]["name"] if cat_id else "",
                "channels": [channel_view(c) for c in sorted(groups[cat_id], key=order)],
            })
        return {"categories": result}

    def dms(self) -> dict[str, Any]:
        api = self.require_api()
        channels = api.get("/users/@me/channels")
        channels.sort(key=lambda c: -int(c.get("last_message_id") or 0))
        saved = saved_states(self.out / "dms")
        items = []
        for ch in channels:
            recipients = ch.get("recipients", [])
            if ch.get("icon"):
                icon = f"{dd.render.CDN}/channel-icons/{ch['id']}/{ch['icon']}.png?size=64"
            elif recipients:
                icon = avatar_url(recipients[0])
            else:
                icon = None
            items.append({
                "id": ch["id"],
                "name": dm_label(ch),
                "icon": CHANNEL_ICONS.get(ch["type"], "@"),
                "avatar": icon,
                "sub": ", ".join(r.get("username", "") for r in recipients) if ch["type"] == dd.GROUP_DM else
                (recipients[0].get("username", "") if recipients else ""),
                "readable": True,
                "saved": saved.get(ch["id"]),
            })
        return {"categories": [{"id": "", "name": "", "channels": items}]}

    # ---- jobs

    def start_job(self, payload: dict[str, Any]) -> None:
        api = self.require_api()
        if self.job and self.job.running:
            raise ValueError("すでに実行中です")
        opts = payload.get("options", {})
        guilds = {gid: set(cids) for gid, cids in (payload.get("guilds") or {}).items() if cids}
        dms = set(payload.get("dms") or [])
        saved = bool(payload.get("saved"))
        if not saved and not guilds and not dms:
            raise ValueError("チャンネルが選択されていません")
        formats = {f for f in opts.get("formats", dd.FORMATS) if f in dd.FORMATS}
        if not formats:
            raise ValueError("出力形式を 1 つ以上選んでください")
        self.set_out(opts.get("out"))

        refresh_from = None
        if opts.get("full"):
            refresh_from = 0
        elif float(opts.get("refresh_days") or 0) > 0:
            since = datetime.now().astimezone() - timedelta(days=float(opts["refresh_days"]))
            refresh_from = dd.date_to_snowflake(since)

        job = Job()
        dumper = dd.Dumper(
            api,
            self.out,
            attachments=bool(opts.get("attachments")),
            refresh_from=refresh_from,
            threads=bool(opts.get("threads", True)),
            formats=formats,
            stop=job.stop,
        )

        def run() -> None:
            dd.log_hook = job.log
            try:
                try:
                    if saved:
                        dumper.dump_saved()
                    for gid, cids in guilds.items():
                        dumper.dump_guild(gid, cids)
                    if dms:
                        dumper.dump_dms(dms)
                finally:
                    dumper.render_index()
                dd.log("完了")
            except dd.Cancelled:
                job.cancelled = True
                dd.log("中止しました (次回は続きから取得します)")
            except Exception as e:  # noqa: BLE001
                job.error = str(e)
                dd.log(f"エラー: {e}")
            finally:
                job.running = False
                dd.log_hook = None

        self.job = job
        threading.Thread(target=run, daemon=True).start()

    def job_status(self, since: int) -> dict[str, Any]:
        job = self.job
        if not job:
            return {"exists": False}
        lines = job.since(since)
        return {
            "exists": True,
            "running": job.running,
            "cancelled": job.cancelled,
            "error": job.error,
            "lines": lines,
            "next": since + len(lines),
        }


# -------------------------------------------------------------------- server


class Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "discord-dumper"

    def log_message(self, *args: Any) -> None:
        pass

    # ---- helpers

    def _authorized(self) -> bool:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        c = cookie.get("dd_key")
        return c is not None and secrets.compare_digest(c.value, self.app.secret)

    def _host_ok(self) -> bool:
        # DNS リバインディング対策
        return self.headers.get("Host", "") in self.app.hosts

    def _send(self, status: int, body: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, status: int = 200) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _text(self, status: int, msg: str) -> None:
        self._send(status, msg.encode(), "text/plain; charset=utf-8")

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _guard(self) -> bool:
        if not self._host_ok():
            self._text(403, "forbidden")
            return False
        if not self._authorized():
            self._text(403, "ターミナルに表示された URL から開いてください")
            return False
        return True

    def _run(self, fn: Any) -> None:
        try:
            fn()
        except dd.ApiError as e:
            self._json({"error": str(e)}, 401 if e.status == 401 else 502)
        except (ValueError, KeyError) as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    # ---- routes

    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/" and "k" in q and self._host_ok():
            if not secrets.compare_digest(q["k"], self.app.secret):
                return self._text(403, "鍵が違います")
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", f"dd_key={self.app.secret}; HttpOnly; SameSite=Strict; Path=/")
            self.end_headers()
            return
        if not self._guard():
            return
        if u.path == "/":
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        app = self.app
        if "out" in q:
            app.set_out(q["out"])
        refresh = q.get("refresh") == "1"
        if u.path == "/api/me":
            return self._json({
                "user": {"name": display_name(app.me), "username": app.me.get("username"),
                         "avatar": avatar_url(app.me)} if app.me else None,
                "out": str(app.out),
                "job": bool(app.job and app.job.running),
            })
        if u.path == "/api/guilds":
            return self._run(lambda: self._json(app.guilds(refresh)))
        if m := re.fullmatch(r"/api/guilds/(\d+)/channels", u.path):
            return self._run(lambda: self._json(app.guild_channels(m[1], refresh)))
        if u.path == "/api/dms":
            return self._run(lambda: self._json(app.dms()))
        if u.path == "/api/job":
            return self._json(app.job_status(int(q.get("since", 0))))
        self._text(404, "not found")

    def do_POST(self) -> None:
        if not self._guard():
            return
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc not in self.app.hosts:
            return self._text(403, "forbidden")
        u = urlparse(self.path)
        app = self.app

        def login() -> None:
            app.login(self._body().get("token", ""))
            self._json({"ok": True})

        def start() -> None:
            app.start_job(self._body())
            self._json({"ok": True})

        if u.path == "/api/login":
            return self._run(login)
        if u.path == "/api/logout":
            app.logout()
            return self._json({"ok": True})
        if u.path == "/api/jobs":
            return self._run(start)
        if u.path == "/api/open":
            return self._run(lambda: self._json(app.open_result(self._body().get("out"))))
        if u.path == "/api/job/cancel":
            if app.job:
                app.job.stop.set()
            return self._json({"ok": True})
        self._text(404, "not found")


def serve(out: Path, token: str | None, bot: bool, delay: float, port: int, open_browser: bool) -> None:
    app = App(out, token, bot, delay)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    real_port = server.server_address[1]
    app.hosts = {f"127.0.0.1:{real_port}", f"localhost:{real_port}"}
    Handler.app = app
    url = f"http://127.0.0.1:{real_port}/?k={app.secret}"
    print(f"GUI を起動しました: {url}\n(Ctrl+C で終了)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n終了します")
    finally:
        if app.job:
            app.job.stop.set()
        server.server_close()


# ---------------------------------------------------------------------- page

PAGE = r"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Discord Dumper</title>
<style>
:root{--bg:#fff;--bg2:#f2f3f5;--bg3:#e3e5e8;--fg:#2e3338;--muted:#5c5e66;--line:#e3e5e8;--accent:#5865f2;--accent-fg:#fff;
--hover:rgba(116,127,141,.12);--sel:rgba(88,101,242,.12);--ok:#248046;--err:#d83c3e;--shadow:0 8px 24px rgba(0,0,0,.18)}
@media (prefers-color-scheme:dark){:root{--bg:#313338;--bg2:#2b2d31;--bg3:#1e1f22;--fg:#dbdee1;--muted:#949ba4;--line:#3f4147;
--hover:rgba(78,80,88,.4);--sel:rgba(88,101,242,.22);--ok:#3ba55c;--err:#f23f43;--shadow:0 8px 24px rgba(0,0,0,.5)}}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif}
button,input,select{font:inherit;color:inherit}
button,.btn{border:0;border-radius:4px;padding:6px 14px;cursor:pointer;background:var(--bg3);text-decoration:none;display:inline-block}
button:hover,.btn:hover{filter:brightness(1.1)}button:disabled{opacity:.5;cursor:not-allowed}
.primary{background:var(--accent);color:var(--accent-fg);font-weight:600}
.ghost{background:transparent;color:var(--muted)}.ghost:hover{background:var(--hover);color:var(--fg)}
input[type=search],input[type=text],input[type=password],input[type=number]{background:var(--bg3);border:1px solid transparent;border-radius:4px;padding:6px 8px}
input:focus{outline:2px solid var(--accent);outline-offset:-1px}
input[type=checkbox]{accent-color:var(--accent);width:16px;height:16px;margin:0}
[hidden]{display:none!important}
.app{display:grid;grid-template-rows:auto minmax(0,1fr) auto;grid-template-columns:minmax(0,1fr);height:100vh}
.top{display:flex;align-items:center;gap:10px;padding:8px 16px;border-bottom:1px solid var(--line);background:var(--bg2)}
.top .me{display:flex;align-items:center;gap:6px;margin-left:auto;color:var(--muted)}.top .me img{width:24px;height:24px;border-radius:50%}
.layout{display:grid;grid-template-columns:260px minmax(0,1fr);min-height:0}
.side{background:var(--bg2);border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
.side input{margin:10px;width:calc(100% - 20px)}
.glist{overflow:auto;padding:0 8px 8px}
.gitem{display:flex;align-items:center;gap:10px;width:100%;padding:6px 8px;border-radius:4px;background:none;text-align:left;color:var(--muted)}
.gitem:hover{background:var(--hover);color:var(--fg)}.gitem.active{background:var(--sel);color:var(--fg)}
.gicon{width:32px;height:32px;border-radius:50%;flex:none;background:var(--bg3);display:grid;place-items:center;font-size:12px;font-weight:600;overflow:hidden}
.gicon img{width:100%;height:100%}
.gname{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.count{background:var(--accent);color:var(--accent-fg);border-radius:8px;padding:0 6px;font-size:12px;font-weight:700}
.dot{width:6px;height:6px;border-radius:50%;background:var(--ok);flex:none}
.sep{height:1px;background:var(--line);margin:6px 8px}
.main{display:flex;flex-direction:column;min-height:0;min-width:0}
.toolbar{display:flex;align-items:center;gap:8px;padding:10px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.toolbar h2{font-size:16px;margin:0 auto 0 0}
.tree{overflow:auto;padding:8px 16px 24px}
.empty{color:var(--muted);padding:40px 0;text-align:center}
.cat{display:flex;align-items:center;gap:8px;margin:14px 0 4px;font-size:12px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.02em}
.ch{display:flex;align-items:center;gap:10px;padding:6px 8px;border-radius:4px;cursor:pointer}
.ch:hover{background:var(--hover)}.ch.on{background:var(--sel)}
.ch.locked{opacity:.45;cursor:not-allowed}
.ch .icon{width:20px;text-align:center;color:var(--muted)}
.ch .avatar{width:24px;height:24px;border-radius:50%}
.ch .name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ch .sub{color:var(--muted);font-size:12px;margin-left:6px}
.ch .meta{color:var(--muted);font-size:12px;white-space:nowrap}
.ch .meta.saved{color:var(--ok)}
.bar{display:flex;flex-direction:column;gap:10px;padding:10px 16px;border-top:1px solid var(--line);background:var(--bg2)}
.actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.summary{font-weight:600;margin-right:auto}
.opts{display:flex;gap:6px 16px;flex-wrap:wrap;align-items:center;color:var(--muted)}
.opts label{display:flex;gap:5px;align-items:center;white-space:nowrap}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.5);display:grid;place-items:center;padding:16px;z-index:10}
.card{background:var(--bg);border-radius:8px;box-shadow:var(--shadow);padding:24px;width:min(440px,100%)}
.card h1{margin:0 0 8px;font-size:20px}.card p{color:var(--muted);margin:0 0 16px}
.card form{display:flex;flex-direction:column;gap:10px}
.err{color:var(--err);min-height:1.2em;margin:0}
.login{background:var(--bg2)}
.jobcard{width:min(760px,100%);display:flex;flex-direction:column;gap:10px;max-height:calc(100vh - 32px)}
.jobhead{display:flex;gap:10px;align-items:baseline}.jobhead span{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.spinner{width:14px;height:14px;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:spin 1s linear infinite;align-self:center}
@keyframes spin{to{transform:rotate(360deg)}}
pre.log{background:var(--bg3);border-radius:6px;padding:10px;margin:0;height:360px;overflow:auto;font:12px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-all}
.jobactions{display:flex;gap:8px;justify-content:flex-end}
.toast{position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:var(--err);color:#fff;padding:8px 14px;border-radius:6px;box-shadow:var(--shadow);z-index:20}
@media (max-width:700px){
 .layout{grid-template-columns:minmax(0,1fr);grid-template-rows:auto minmax(0,1fr)}
 .side{border-right:0;border-bottom:1px solid var(--line);max-height:28vh}
 .top,.toolbar,.tree,.bar{padding-left:12px;padding-right:12px}
 .toolbar h2{width:100%}.toolbar input{flex:1;min-width:0}
 .ch .meta{max-width:40%;overflow:hidden;text-overflow:ellipsis}
 .opts label:last-child{flex:1}.opts #optOut{flex:1;min-width:0;width:auto!important}
 .actions .summary{width:100%}
}
</style></head>
<body>
<div id="login" class="overlay login" hidden>
  <div class="card">
    <h1>Discord Dumper</h1>
    <p>ユーザートークンを入力してください。トークンはこの PC で動いているプロセスのメモリにだけ保持され、ファイルには保存されません。</p>
    <form id="loginForm">
      <input id="token" type="password" placeholder="トークン" autocomplete="off" required>
      <button class="primary">ログイン</button>
      <p id="loginErr" class="err"></p>
    </form>
  </div>
</div>

<div id="app" class="app" hidden>
  <header class="top">
    <b>Discord Dumper</b>
    <span class="me"><img id="meAvatar" alt=""><span id="meName"></span></span>
    <button id="logout" class="ghost">ログアウト</button>
  </header>
  <div class="layout">
    <aside class="side">
      <input id="gfilter" type="search" placeholder="サーバーを検索">
      <div id="glist" class="glist"></div>
    </aside>
    <main class="main">
      <div class="toolbar">
        <h2 id="title">サーバーを選択してください</h2>
        <input id="cfilter" type="search" placeholder="絞り込み">
        <button id="selAll" class="ghost">すべて選択</button>
        <button id="selNone" class="ghost">選択解除</button>
        <button id="reload" class="ghost" title="一覧を再取得">↻ 再読み込み</button>
      </div>
      <div id="tree" class="tree"><div class="empty">左の一覧からサーバーか DM を選んでください</div></div>
    </main>
  </div>
  <footer class="bar">
    <div class="opts">
      <label><input type="checkbox" id="optThreads" checked> スレッド</label>
      <label><input type="checkbox" id="optAttach"> 添付ファイル</label>
      <label><input type="checkbox" id="optHtml" checked> HTML</label>
      <label><input type="checkbox" id="optTxt" checked> txt</label>
      <label title="直近 N 日分を取り直して編集・削除・リアクションを反映">再取得 <input type="number" id="optRefresh" min="0" step="1" value="0" style="width:4.5em"> 日</label>
      <label title="全期間を取り直して編集・削除を反映 (時間がかかります)"><input type="checkbox" id="optFull"> 全期間を再取得</label>
      <label>出力先 <input type="text" id="optOut" style="width:16em"></label>
    </div>
    <div class="actions">
      <div id="summary" class="summary">未選択</div>
      <button id="clearAll" class="ghost">全解除</button>
      <button class="ghost openResult">結果を開く</button>
      <button id="updateSaved" title="保存済みのチャンネル / スレッド / DM をまとめて差分更新 (選択内容は使いません)">保存済みをすべて更新</button>
      <button id="start" class="primary" disabled>ダンプ開始</button>
    </div>
  </footer>
</div>

<div id="job" class="overlay" hidden>
  <div class="card jobcard">
    <div class="jobhead"><div id="spin" class="spinner"></div><b id="jobStatus">実行中…</b><span id="jobCurrent"></span></div>
    <pre id="jobLog" class="log"></pre>
    <div class="jobactions">
      <button id="cancel">中止</button>
      <button id="jobView" class="primary openResult" hidden>結果を開く</button>
      <button id="jobClose" hidden>閉じる</button>
    </div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const h = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const store = {
  get(k, d) { try { const v = localStorage.getItem('dd:' + k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem('dd:' + k, JSON.stringify(v)); } catch {} },
};

async function api(path, body) {
  const opts = body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({error: res.statusText}));
  if (res.status === 401) { showLogin(data.error); throw new Error(data.error); }
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function toast(msg) {
  const t = document.createElement('div');
  t.className = 'toast'; t.textContent = msg;
  document.body.append(t); setTimeout(() => t.remove(), 5000);
}

// ---- 選択状態 (localStorage に保存して次回も使えるように)
const saved = store.get('selection', {guilds: {}, dms: []});
const sel = {
  guilds: Object.fromEntries(Object.entries(saved.guilds || {}).map(([k, v]) => [k, new Set(v)])),
  dms: new Set(saved.dms || []),
};
const names = store.get('names', {});  // id -> 表示名 (未読み込みサーバーの集計表示用)
function selSet(key) { return key === 'dms' ? sel.dms : (sel.guilds[key] ||= new Set()); }
function persist() {
  store.set('selection', {
    guilds: Object.fromEntries(Object.entries(sel.guilds).filter(([, s]) => s.size).map(([k, s]) => [k, [...s]])),
    dms: [...sel.dms],
  });
  store.set('names', names);
  renderSummary(); renderGuildCounts();
}

// ---- オプション
const optIds = ['optThreads', 'optAttach', 'optHtml', 'optTxt', 'optRefresh', 'optFull', 'optOut'];
function loadOpts(defaultOut) {
  const o = store.get('options', {});
  for (const id of optIds) {
    const el = $('#' + id); if (!(id in o)) continue;
    if (el.type === 'checkbox') el.checked = o[id]; else el.value = o[id];
  }
  if (!$('#optOut').value) $('#optOut').value = defaultOut;
}
function saveOpts() {
  store.set('options', Object.fromEntries(optIds.map(id => { const el = $('#' + id); return [id, el.type === 'checkbox' ? el.checked : el.value]; })));
}
for (const id of optIds) $('#' + id).addEventListener('change', () => {
  saveOpts();
  if (id === 'optOut' && current) openView(current, false);
});

// ---- サーバー一覧
let guilds = [], current = null, currentData = null;

function initials(name) { return name.split(/\s+/).map(w => [...w][0]).join('').slice(0, 3); }

function renderGuilds() {
  const q = $('#gfilter').value.trim().toLowerCase();
  const items = [{id: 'dms', name: 'ダイレクトメッセージ', icon: null, dm: true}, null, ...guilds];
  $('#glist').innerHTML = items.filter(g => !g || g.dm || !q || g.name.toLowerCase().includes(q)).map(g => {
    if (!g) return '<div class="sep"></div>';
    const icon = g.icon ? `<img src="${h(g.icon)}" alt="" loading="lazy">` : (g.dm ? '@' : h(initials(g.name)));
    return `<button class="gitem${current === g.id ? ' active' : ''}" data-id="${h(g.id)}">
      <span class="gicon">${icon}</span><span class="gname">${h(g.name)}</span>
      ${g.saved ? '<span class="dot" title="保存済みデータあり"></span>' : ''}<span class="count" data-count="${h(g.id)}" hidden></span></button>`;
  }).join('');
  renderGuildCounts();
}

function renderGuildCounts() {
  document.querySelectorAll('[data-count]').forEach(el => {
    const n = el.dataset.count === 'dms' ? sel.dms.size : (sel.guilds[el.dataset.count]?.size || 0);
    el.textContent = n; el.hidden = !n;
  });
}

$('#glist').addEventListener('click', e => {
  const b = e.target.closest('.gitem'); if (b) openView(b.dataset.id, false);
});
$('#gfilter').addEventListener('input', renderGuilds);

// ---- チャンネル一覧
function outParam() { return 'out=' + encodeURIComponent($('#optOut').value); }

async function openView(id, refresh) {
  current = id; renderGuilds(); store.set('current', id);
  const g = guilds.find(g => g.id === id);
  $('#title').textContent = id === 'dms' ? 'ダイレクトメッセージ' : (g?.name || id);
  $('#tree').innerHTML = '<div class="empty">読み込み中…</div>';
  try {
    const path = id === 'dms' ? '/api/dms' : `/api/guilds/${id}/channels`;
    const data = await api(`${path}?${outParam()}${refresh ? '&refresh=1' : ''}`);
    if (current !== id) return;
    currentData = data;
    for (const c of data.categories) for (const ch of c.channels) names[ch.id] = ch.name;
    if (g) names['g' + id] = g.name;
    renderTree();
  } catch (e) {
    if (current === id) $('#tree').innerHTML = `<div class="empty">読み込めませんでした: ${h(e.message)}</div>`;
  }
}

function fmtSaved(s) {
  if (!s) return '';
  const parts = [];
  if (s.count != null) parts.push(`${s.count.toLocaleString()} 件`);
  if (s.threads) parts.push(`スレッド ${s.threads}`);
  if (s.updated_at) {
    const d = new Date(s.updated_at);
    parts.push(`${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`);
  }
  return '保存済み ' + parts.join(' · ');
}

function visibleChannels() {
  const q = $('#cfilter').value.trim().toLowerCase();
  const out = [];
  for (const c of currentData?.categories || []) {
    const chs = c.channels.filter(ch => !q || ch.name.toLowerCase().includes(q) || (ch.sub || '').toLowerCase().includes(q) || c.name.toLowerCase().includes(q));
    if (chs.length) out.push({...c, channels: chs});
  }
  return out;
}

function renderTree() {
  const s = selSet(current);
  const cats = visibleChannels();
  if (!cats.length) { $('#tree').innerHTML = '<div class="empty">チャンネルがありません</div>'; return; }
  $('#tree').innerHTML = cats.map(c => {
    const selectable = c.channels.filter(ch => ch.readable !== false);
    const on = selectable.filter(ch => s.has(ch.id)).length;
    const head = c.name ? `<label class="cat"><input type="checkbox" data-cat="${h(c.id)}" ${on && on === selectable.length ? 'checked' : ''} data-partial="${on && on < selectable.length ? 1 : 0}"> ${h(c.name)}</label>` : '';
    return head + c.channels.map(ch => {
      const locked = ch.readable === false;
      const lead = ch.avatar ? `<img class="avatar" src="${h(ch.avatar)}" alt="" loading="lazy">` : `<span class="icon">${h(ch.icon)}</span>`;
      const meta = locked ? '🔒 閲覧権限なし' : (ch.saved ? fmtSaved(ch.saved) : (ch.forum ? 'フォーラム (スレッドを取得)' : ''));
      return `<label class="ch${locked ? ' locked' : ''}${s.has(ch.id) ? ' on' : ''}">
        <input type="checkbox" data-id="${h(ch.id)}" ${s.has(ch.id) ? 'checked' : ''} ${locked ? 'disabled' : ''}>
        ${lead}<span class="name">${h(ch.name)}${ch.sub && ch.sub !== ch.name ? `<span class="sub">${h(ch.sub)}</span>` : ''}</span>
        <span class="meta${ch.saved && !locked ? ' saved' : ''}">${h(meta)}</span></label>`;
    }).join('');
  }).join('');
  document.querySelectorAll('[data-partial="1"]').forEach(el => el.indeterminate = true);
}

$('#tree').addEventListener('change', e => {
  const s = selSet(current);
  const t = e.target;
  if (t.dataset.id) {
    t.checked ? s.add(t.dataset.id) : s.delete(t.dataset.id);
  } else if (t.dataset.cat !== undefined) {
    const cat = visibleChannels().find(c => c.id === t.dataset.cat);
    for (const ch of cat?.channels || []) if (ch.readable !== false) t.checked ? s.add(ch.id) : s.delete(ch.id);
  }
  persist(); renderTree();
});
$('#cfilter').addEventListener('input', () => currentData && renderTree());
$('#selAll').addEventListener('click', () => {
  if (!currentData) return;
  const s = selSet(current);
  for (const c of visibleChannels()) for (const ch of c.channels) if (ch.readable !== false) s.add(ch.id);
  persist(); renderTree();
});
$('#selNone').addEventListener('click', () => {
  if (!currentData) return;
  const s = selSet(current);
  for (const c of visibleChannels()) for (const ch of c.channels) s.delete(ch.id);
  persist(); renderTree();
});
$('#reload').addEventListener('click', async () => {
  await loadGuilds(true);
  if (current) openView(current, true);
});
$('#clearAll').addEventListener('click', () => {
  if (!confirm('すべてのサーバーと DM の選択を解除しますか？')) return;
  sel.guilds = {}; sel.dms.clear(); persist(); if (currentData) renderTree();
});

function renderSummary() {
  const gs = Object.entries(sel.guilds).filter(([, s]) => s.size);
  const nch = gs.reduce((n, [, s]) => n + s.size, 0);
  const parts = [];
  if (gs.length) parts.push(`${gs.length} サーバー · ${nch} チャンネル`);
  if (sel.dms.size) parts.push(`DM ${sel.dms.size} 件`);
  $('#summary').textContent = parts.length ? parts.join(' / ') + ' を選択中' : '未選択';
  $('#summary').title = gs.map(([g, s]) => `${names['g' + g] || g}: ${[...s].map(id => names[id] || id).join(', ')}`).join('\n');
  $('#start').disabled = !parts.length;
}

// ---- 実行
function jobOptions() {
  const formats = [];
  if ($('#optHtml').checked) formats.push('html');
  if ($('#optTxt').checked) formats.push('txt');
  return {
    threads: $('#optThreads').checked, attachments: $('#optAttach').checked, formats,
    refresh_days: Number($('#optRefresh').value) || 0, full: $('#optFull').checked, out: $('#optOut').value,
  };
}
async function openResult() {
  try {
    const r = await api('/api/open', {out: $('#optOut').value});
    toast('開きました: ' + r.path);
  } catch (e) { toast(e.message); }
}
document.querySelectorAll('.openResult').forEach(b => b.addEventListener('click', openResult));

async function startJob(payload) {
  try { await api('/api/jobs', payload); startPolling(true); }
  catch (e) { toast(e.message); }
}
$('#start').addEventListener('click', () => startJob({
  guilds: Object.fromEntries(Object.entries(sel.guilds).filter(([, s]) => s.size).map(([k, s]) => [k, [...s]])),
  dms: [...sel.dms],
  options: jobOptions(),
}));
$('#updateSaved').addEventListener('click', () => startJob({saved: true, options: jobOptions()}));

let polling = false;
async function startPolling(reset) {
  if (polling) return;
  polling = true;
  let since = 0;
  $('#job').hidden = false; $('#jobLog').textContent = '';
  $('#jobStatus').textContent = '実行中…'; $('#jobCurrent').textContent = '';
  $('#spin').hidden = false; $('#cancel').hidden = false; $('#cancel').disabled = false;
  $('#jobView').hidden = true; $('#jobClose').hidden = true;
  while (true) {
    let st;
    try { st = await api('/api/job?since=' + since); } catch { await new Promise(r => setTimeout(r, 2000)); continue; }
    if (!st.exists) break;
    if (st.lines.length) {
      const log = $('#jobLog');
      const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
      log.textContent += st.lines.join('\n') + '\n';
      if (atBottom) log.scrollTop = log.scrollHeight;
      const cur = [...st.lines].reverse().find(l => l.startsWith('# ') || l.startsWith('==='));
      if (cur) $('#jobCurrent').textContent = cur.replace(/^#\s*/, '');
    }
    since = st.next;
    if (!st.running) {
      $('#spin').hidden = true; $('#cancel').hidden = true; $('#jobClose').hidden = false; $('#jobView').hidden = false;
      $('#jobStatus').textContent = st.error ? 'エラーで終了しました' : st.cancelled ? '中止しました' : '完了しました';
      $('#jobCurrent').textContent = st.error || '';
      break;
    }
    await new Promise(r => setTimeout(r, 700));
  }
  polling = false;
}
$('#cancel').addEventListener('click', async () => {
  $('#cancel').disabled = true; $('#jobStatus').textContent = '中止しています…';
  await api('/api/job/cancel', {});
});
$('#jobClose').addEventListener('click', () => {
  $('#job').hidden = true;
  loadGuilds(false); if (current) openView(current, false);
});

// ---- ログイン
function showLogin(msg) {
  $('#app').hidden = true; $('#login').hidden = false;
  $('#loginErr').textContent = msg && !/ログインしていません/.test(msg) ? msg : '';
  $('#token').focus();
}
$('#loginForm').addEventListener('submit', async e => {
  e.preventDefault();
  $('#loginErr').textContent = '';
  try { await api('/api/login', {token: $('#token').value}); $('#token').value = ''; boot(); }
  catch (err) { $('#loginErr').textContent = 'ログインできませんでした: ' + err.message; }
});
$('#logout').addEventListener('click', async () => { await api('/api/logout', {}); showLogin(); });

async function loadGuilds(refresh) {
  guilds = await api('/api/guilds?' + outParam() + (refresh ? '&refresh=1' : ''));
  for (const g of guilds) names['g' + g.id] = g.name;
  renderGuilds(); renderSummary();
}

async function boot() {
  const me = await api('/api/me');
  if (!me.user) return showLogin();
  $('#login').hidden = true; $('#app').hidden = false;
  $('#meName').textContent = me.user.name; $('#meAvatar').src = me.user.avatar;
  loadOpts(me.out);
  await loadGuilds(false);
  const last = location.hash.slice(1) || store.get('current', null);
  if (last && (last === 'dms' || guilds.some(g => g.id === last))) openView(last, false);
  if (me.job) startPolling();
}
boot().catch(e => toast(e.message));
</script>
</body></html>
"""
