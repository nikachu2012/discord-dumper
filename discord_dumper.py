#!/usr/bin/env python3
"""Discord のサーバーチャンネル / スレッド / DM をダンプするツール (標準ライブラリのみ)。

使い方は README.md を参照。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import render
from render import dm_label, read_json, write_atomic

API_BASE = "https://discord.com/api/v9"
DISCORD_EPOCH_MS = 1420070400000
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) discord/1.0.9200 Chrome/134.0.6998.205 "
    "Electron/35.3.0 Safari/537.36"
)

# チャンネル種別
GUILD_TEXT = 0
DM = 1
GUILD_VOICE = 2
GROUP_DM = 3
GUILD_CATEGORY = 4
GUILD_ANNOUNCEMENT = 5
ANNOUNCEMENT_THREAD = 10
PUBLIC_THREAD = 11
PRIVATE_THREAD = 12
GUILD_STAGE_VOICE = 13
GUILD_FORUM = 15
GUILD_MEDIA = 16

THREAD_TYPES = {ANNOUNCEMENT_THREAD, PUBLIC_THREAD, PRIVATE_THREAD}
MESSAGE_CHANNEL_TYPES = {GUILD_TEXT, GUILD_VOICE, GUILD_ANNOUNCEMENT, GUILD_STAGE_VOICE}
THREAD_PARENT_TYPES = {GUILD_TEXT, GUILD_ANNOUNCEMENT, GUILD_FORUM, GUILD_MEDIA}
FORUM_TYPES = {GUILD_FORUM, GUILD_MEDIA}

# 権限不足などで読めないときに返るステータス
SKIPPABLE_STATUS = {401, 403, 404}

FORMATS = ("html", "txt")


# GUI などがログを受け取るためのフック
log_hook: Callable[[str], None] | None = None


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
    if log_hook:
        log_hook(msg)


class Cancelled(Exception):
    """stop イベントによる中止。"""


# --------------------------------------------------------------------------- API


class ApiError(Exception):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        code = body.get("code") if isinstance(body, dict) else None
        message = body.get("message") if isinstance(body, dict) else body
        super().__init__(f"HTTP {status} (code={code}): {message}")


class DiscordApi:
    def __init__(self, token: str, bot: bool = False, delay: float = 0.0):
        self.auth = f"Bot {token}" if bot else token
        self.delay = delay

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
        headers = {
            "Authorization": self.auth,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        attempt = 0
        while True:
            attempt += 1
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as res:
                    body = json.loads(res.read() or b"null")
                    self._respect_bucket(res.headers)
                    if self.delay:
                        time.sleep(self.delay)
                    return body
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = raw.decode("utf-8", "replace")
                if e.code == 429:
                    wait = float(body.get("retry_after", 5)) if isinstance(body, dict) else 5.0
                    log(f"  レート制限: {wait:.1f} 秒待機")
                    time.sleep(wait + 0.25)
                    continue
                if e.code >= 500 and attempt < 5:
                    time.sleep(2**attempt)
                    continue
                raise ApiError(e.code, body) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < 5:
                    log(f"  通信エラー ({e}); 再試行します")
                    time.sleep(2**attempt)
                    continue
                raise

    @staticmethod
    def _respect_bucket(headers: Any) -> None:
        remaining = headers.get("X-RateLimit-Remaining")
        reset_after = headers.get("X-RateLimit-Reset-After")
        if remaining == "0" and reset_after:
            time.sleep(float(reset_after) + 0.1)


def download(url: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 0:
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=120) as res, open(tmp, "wb") as f:
                while chunk := res.read(1 << 16):
                    f.write(chunk)
            tmp.replace(dest)
            return True
        except Exception as e:  # noqa: BLE001
            log(f"  添付ファイルの取得に失敗 ({attempt}/3): {dest.name}: {e}")
            time.sleep(attempt)
    tmp.unlink(missing_ok=True)
    return False


# ----------------------------------------------------------------------- helpers


def safe_name(name: str, limit: int = 60) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "").strip(" .")
    return name[:limit] or "unnamed"


def entity_dir(parent: Path, name: str, entity_id: str) -> Path:
    """`<名前>_<id>` のディレクトリ。名前が変わっていても既存のものを再利用する。"""
    if parent.is_dir():
        for p in parent.glob(f"*_{entity_id}"):
            if p.is_dir():
                return p
    return parent / f"{safe_name(name)}_{entity_id}"


def date_to_snowflake(value: str | datetime) -> int:
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    if dt.tzinfo is None:
        dt = dt.astimezone()  # ローカルタイムとして解釈
    return max(0, int(dt.timestamp() * 1000) - DISCORD_EPOCH_MS) << 22


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_SIGNED_URL_PARAMS = re.compile(r"([?&])(?:ex|is|hm)=[0-9a-f]+&?")


def fingerprint(msg: dict[str, Any]) -> str:
    """変更検出用。ツール独自のキー (_ 始まり) と CDN 署名付き URL のパラメータを除く。"""
    s = json.dumps({k: v for k, v in msg.items() if not k.startswith("_")}, sort_keys=True)
    return _SIGNED_URL_PARAMS.sub(r"\1", s)


# ------------------------------------------------------------------------ store


class ChannelStore:
    """チャンネル 1 つ分の保存データ。

    messages.jsonl は追記型のログで、同じ ID が複数回出てきたら後の行が最新版。
    state.json に最終メッセージ ID などを記録し、次回はそこから差分取得する。
    """

    def __init__(self, d: Path):
        self.dir = d
        self.jsonl = d / "messages.jsonl"
        self.state_path = d / "state.json"
        self.state = self._load_state()

    def _jsonl_size(self) -> int:
        return self.jsonl.stat().st_size if self.jsonl.exists() else 0

    def _load_state(self) -> dict[str, Any]:
        state = read_json(self.state_path) or {}
        if state.get("jsonl_size") != self._jsonl_size():
            # 中断などで state と jsonl がずれている → jsonl から作り直す
            state = self._state_from(self.load())
        return state

    @property
    def last_id(self) -> int:
        return int(self.state.get("last_id") or 0)

    @property
    def thread_ids(self) -> set[str]:
        return set(self.state.get("thread_ids", []))

    def load(self) -> dict[str, dict[str, Any]]:
        msgs: dict[str, dict[str, Any]] = {}
        if not self.jsonl.exists():
            return msgs
        with open(self.jsonl, encoding="utf-8") as f:
            for line in f:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue  # 中断で壊れた行は無視
                msgs[msg["id"]] = msg
        return msgs

    @staticmethod
    def sorted(msgs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(msgs.values(), key=lambda m: int(m["id"]))

    def compact(self, msgs: dict[str, dict[str, Any]]) -> None:
        """重複行を除いて ID 順に書き直す。"""
        write_atomic(
            self.jsonl,
            "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in self.sorted(msgs)),
        )

    def _state_from(self, msgs: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return {
            "last_id": max((int(i) for i in msgs), default=0),
            "count": sum(1 for m in msgs.values() if not m.get("_deleted")),
            "deleted": sum(1 for m in msgs.values() if m.get("_deleted")),
            "thread_ids": sorted({m["thread"]["id"] for m in msgs.values() if m.get("thread")}),
            "jsonl_size": self._jsonl_size(),
        }

    def save_state(self, msgs: dict[str, dict[str, Any]]) -> None:
        self.state = self._state_from(msgs) | {"updated_at": now_iso()}
        write_atomic(self.state_path, json.dumps(self.state, ensure_ascii=False, indent=2))


# ----------------------------------------------------------------------- dumper


class Dumper:
    def __init__(
        self,
        api: DiscordApi | None,
        out: Path,
        *,
        attachments: bool = False,
        after: int = 0,
        before: int | None = None,
        refresh_from: int | None = None,
        threads: bool = True,
        formats: set[str] = set(FORMATS),
        stop: threading.Event | None = None,
    ):
        self.api = api
        self.out = out
        self.attachments = attachments
        self.after = after
        self.before = before
        # この ID 以降の保存済みメッセージを取り直して、編集・削除を検出する
        self.refresh_from = refresh_from
        self.threads = threads
        self.formats = formats
        self.stop = stop
        self._guild_cache: dict[str, dict[str, Any]] = {}
        self._channel_cache: dict[str, dict[str, Any]] = {}

    def check_stop(self) -> None:
        if self.stop and self.stop.is_set():
            raise Cancelled()

    # ---- metadata

    def guild(self, guild_id: str) -> dict[str, Any]:
        if guild_id not in self._guild_cache:
            self._guild_cache[guild_id] = self.api.get(f"/guilds/{guild_id}")
        return self._guild_cache[guild_id]

    def channel(self, channel_id: str) -> dict[str, Any]:
        if channel_id not in self._channel_cache:
            self._channel_cache[channel_id] = self.api.get(f"/channels/{channel_id}")
        return self._channel_cache[channel_id]

    def guild_dir(self, guild_id: str) -> Path:
        return entity_dir(self.out / "guilds", self.guild(guild_id)["name"], guild_id)

    def channel_dir(self, ch: dict[str, Any]) -> Path:
        if ch["type"] in (DM, GROUP_DM):
            return entity_dir(self.out / "dms", dm_label(ch), ch["id"])
        if ch["type"] in THREAD_TYPES:
            parent = self.channel(ch["parent_id"])
            return entity_dir(self.channel_dir(parent) / "threads", ch.get("name", ""), ch["id"])
        return entity_dir(self.guild_dir(ch["guild_id"]), ch.get("name", ""), ch["id"])

    def write_channel_meta(self, ch: dict[str, Any]) -> Path:
        self._channel_cache[ch["id"]] = ch
        d = self.channel_dir(ch)
        d.mkdir(parents=True, exist_ok=True)
        write_atomic(d / "channel.json", json.dumps(ch, ensure_ascii=False, indent=2))
        return d

    def names(self) -> dict[str, str]:
        """<#id> / <@&id> を表示名に置き換えるための対応表。"""
        names = {cid: c.get("name") or "" for cid, c in self._channel_cache.items()}
        for g in self._guild_cache.values():
            names.update({r["id"]: r["name"] for r in g.get("roles", [])})
        return names

    def subtitle(self, ch: dict[str, Any]) -> str:
        if ch["type"] in (DM, GROUP_DM):
            return "ダイレクトメッセージ"
        guild = self._guild_cache.get(ch.get("guild_id", "")) or (
            self.guild(ch["guild_id"]) if self.api else {}
        )
        parts = [guild.get("name", "")]
        if ch["type"] in THREAD_TYPES and ch.get("parent_id") in self._channel_cache:
            parts.append(render.channel_title(self._channel_cache[ch["parent_id"]]))
        return " › ".join(parts)

    # ---- messages

    def iter_messages(self, channel_id: str, after: int) -> Iterator[dict[str, Any]]:
        """after より新しいメッセージを古い順に返す。"""
        cursor = after
        while True:
            self.check_stop()
            batch = self.api.get(
                f"/channels/{channel_id}/messages", {"after": cursor, "limit": 100}
            )
            if not batch:
                return
            batch.sort(key=lambda m: int(m["id"]))
            for msg in batch:
                if self.before is not None and int(msg["id"]) >= self.before:
                    return
                yield msg
            cursor = int(batch[-1]["id"])
            if len(batch) < 100:
                return

    def dump_channel(self, ch: dict[str, Any]) -> set[str]:
        """1 チャンネル分を差分取得し、メッセージから見つかったスレッド ID を返す。"""
        d = self.write_channel_meta(ch)
        store = ChannelStore(d)
        last = store.last_id

        start = max(self.after, last)
        if self.refresh_from is not None and last:
            start = max(self.after, min(last, self.refresh_from))

        latest = int(ch.get("last_message_id") or 0)
        if last and start >= last and latest and latest <= last:
            log("  更新なし")
            if self._outputs_missing(d):
                self.render_channel(d, ch, store.sorted(store.load()))
            return store.thread_ids

        # 取り直し範囲と保存済みデータが重なる場合だけ既存データを読む
        existing = store.load() if start < last else {}
        seen: set[str] = set()
        new = updated = deleted = 0
        with open(store.jsonl, "a", encoding="utf-8") as f:
            for msg in self.iter_messages(ch["id"], start):
                seen.add(msg["id"])
                old = existing.get(msg["id"])
                if old is not None:
                    self._carry_over(old, msg)
                files_added = self.attachments and self._save_attachments(d, msg)
                if old is not None:
                    if not files_added and fingerprint(old) == fingerprint(msg):
                        continue
                    updated += 1
                else:
                    new += 1
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
                if (new + updated) % 1000 == 0:
                    log(f"  … {new + updated} 件")

            # 取り直した範囲にあったのに返ってこなかったメッセージ = 削除された
            for mid, old in existing.items():
                n = int(mid)
                if (
                    n > start
                    and (self.before is None or n < self.before)
                    and mid not in seen
                    and not old.get("_deleted")
                ):
                    f.write(json.dumps(old | {"_deleted": True, "_deleted_at": now_iso()}, ensure_ascii=False) + "\n")
                    deleted += 1

        msgs = store.load()
        if updated or deleted:
            store.compact(msgs)
        store.save_state(msgs)
        self.render_channel(d, ch, store.sorted(msgs))
        log(f"  新規 {new} / 更新 {updated} / 削除 {deleted} → {d}")
        return store.thread_ids

    @staticmethod
    def _carry_over(old: dict[str, Any], msg: dict[str, Any]) -> None:
        """保存済みの版からツール独自の情報 (ローカルファイル・編集履歴) を引き継ぐ。"""
        if old.get("_files"):
            msg["_files"] = dict(old["_files"])
        edits = list(old.get("_edits", []))
        if (old.get("content") or "") != (msg.get("content") or ""):
            edits.append({
                "content": old.get("content"),
                "edited_timestamp": old.get("edited_timestamp") or old.get("timestamp"),
            })
        if edits:
            msg["_edits"] = edits

    def _save_attachments(self, d: Path, msg: dict[str, Any]) -> bool:
        """未取得の添付ファイルをダウンロードし、1 つでも増えたら True。"""
        files = msg.setdefault("_files", {})
        added = False
        for att in msg.get("attachments", []):
            if att["id"] in files:
                continue
            name = f"{msg['id']}_{att['id']}_{safe_name(att.get('filename', 'file'), 100)}"
            dest = d / "attachments" / name
            if download(att["url"], dest):
                files[att["id"]] = dest.relative_to(d).as_posix()
                added = True
        if not files:
            del msg["_files"]
        return added

    # ---- output

    def _outputs_missing(self, d: Path) -> bool:
        return any(not (d / f"messages.{fmt}").exists() for fmt in self.formats)

    def render_channel(self, d: Path, ch: dict[str, Any], msgs: list[dict[str, Any]]) -> None:
        if "txt" in self.formats:
            render.render_text(msgs, d / "messages.txt")
        if "html" in self.formats:
            render.render_html(
                msgs,
                d / "messages.html",
                ch,
                self.subtitle(ch),
                os.path.relpath(self.out / "index.html", d),
                self.names(),
            )

    def render_index(self) -> None:
        if "html" in self.formats:
            render.render_index(self.out)
            log(f"一覧: {self.out / 'index.html'}")

    def try_dump_channel(self, ch: dict[str, Any]) -> set[str]:
        self.check_stop()
        label = ch.get("name") or dm_label(ch)
        log(f"# {label} ({ch['id']})")
        try:
            return self.dump_channel(ch)
        except ApiError as e:
            if e.status in SKIPPABLE_STATUS:
                log(f"  スキップ: {e}")
                self._remove_if_empty(ch)
                return set()
            raise

    def _remove_if_empty(self, ch: dict[str, Any]) -> None:
        """読めなかったチャンネルの空ディレクトリを残さない。"""
        d = self.channel_dir(ch)
        jsonl = d / "messages.jsonl"
        if jsonl.exists() and jsonl.stat().st_size == 0:
            jsonl.unlink()
        if d.is_dir() and {p.name for p in d.iterdir()} <= {"channel.json"}:
            (d / "channel.json").unlink(missing_ok=True)
            d.rmdir()

    # ---- threads

    def iter_threads(self, parent: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """親チャンネルのスレッド (アクティブ + アーカイブ) を列挙する。"""
        try:
            yield from self._search_threads(parent["id"])
            return
        except ApiError as e:
            if e.status not in SKIPPABLE_STATUS | {400}:
                raise
        # threads/search が使えない場合 (Bot トークンなど) は archived エンドポイントへ
        for kind in ("public", "private"):
            before = None
            while True:
                try:
                    data = self.api.get(
                        f"/channels/{parent['id']}/threads/archived/{kind}",
                        {"limit": 100, "before": before},
                    )
                except ApiError as e:
                    if e.status in SKIPPABLE_STATUS:
                        break
                    raise
                threads = data.get("threads", [])
                yield from threads
                if not data.get("has_more") or not threads:
                    break
                before = threads[-1]["thread_metadata"]["archive_timestamp"]

    def _search_threads(self, channel_id: str) -> Iterator[dict[str, Any]]:
        for archived in ("false", "true"):
            offset = 0
            while True:
                data = self.api.get(
                    f"/channels/{channel_id}/threads/search",
                    {
                        "archived": archived,
                        "sort_by": "last_message_time",
                        "sort_order": "desc",
                        "limit": 25,
                        "offset": offset,
                    },
                )
                if "threads" not in data and "retry_after" in data:
                    # 検索インデックス作成中 (202)
                    time.sleep(float(data["retry_after"]) + 0.5)
                    continue
                threads = data.get("threads", [])
                yield from threads
                offset += len(threads)
                if not data.get("has_more") or not threads:
                    break

    def dump_threads(self, parent: dict[str, Any], extra_ids: set[str]) -> None:
        seen: set[str] = set()
        for th in self.iter_threads(parent):
            if th["id"] in seen:
                continue
            seen.add(th["id"])
            self.try_dump_channel(th)
        # 検索に出てこなかったがメッセージから参照されていたスレッド
        for tid in extra_ids - seen:
            try:
                th = self.channel(tid)
            except ApiError as e:
                if e.status in SKIPPABLE_STATUS:
                    continue
                raise
            self.try_dump_channel(th)

    def dump_with_threads(self, ch: dict[str, Any]) -> None:
        found: set[str] = set()
        if ch["type"] in FORUM_TYPES:
            self.write_channel_meta(ch)  # フォーラム自体にメッセージはない
        else:
            found = self.try_dump_channel(ch)
        if self.threads and ch["type"] in THREAD_PARENT_TYPES:
            try:
                self.dump_threads(ch, found)
            except ApiError as e:
                if e.status not in SKIPPABLE_STATUS:
                    raise
                log(f"  スレッド一覧を取得できません: {e}")

    # ---- high level

    def dump_any_channel(self, channel_id: str) -> None:
        ch = self.channel(channel_id)
        if ch.get("guild_id"):
            self._load_guild(ch["guild_id"])
        self.dump_with_threads(ch)

    def _load_guild(self, guild_id: str) -> list[dict[str, Any]]:
        g = self.guild(guild_id)
        d = self.guild_dir(guild_id)
        d.mkdir(parents=True, exist_ok=True)
        write_atomic(d / "guild.json", json.dumps(g, ensure_ascii=False, indent=2))
        channels = self.api.get(f"/guilds/{guild_id}/channels")
        write_atomic(d / "channels.json", json.dumps(channels, ensure_ascii=False, indent=2))
        for ch in channels:
            ch.setdefault("guild_id", guild_id)
            self._channel_cache.setdefault(ch["id"], ch)
        return channels

    def dump_guild(self, guild_id: str, only: set[str] | None = None) -> None:
        log(f"=== サーバー: {self.guild(guild_id)['name']} ({guild_id}) ===")
        channels = self._load_guild(guild_id)
        channels.sort(key=lambda c: (c.get("position", 0), int(c["id"])))
        for ch in channels:
            if only and ch["id"] not in only:
                continue
            if ch["type"] in MESSAGE_CHANNEL_TYPES | FORUM_TYPES:
                self.dump_with_threads(ch)

    def dump_dms(self, only: set[str] | None = None) -> None:
        log("=== DM ===")
        for ch in self.api.get("/users/@me/channels"):
            if only and ch["id"] not in only:
                continue
            self.try_dump_channel(ch)

    def saved_channels(self) -> list[dict[str, Any]]:
        """出力先に保存済みのチャンネル / スレッド / DM (フォーラム含む) の channel.json。"""
        found: list[dict[str, Any]] = []
        for cj in sorted(self.out.rglob("channel.json")):
            ch = read_json(cj)
            if not ch:
                continue
            if (cj.parent / "messages.jsonl").exists() or ch.get("type") in FORUM_TYPES:
                found.append(ch)
        return found

    def dump_saved(self) -> None:
        """保存済みのものだけを差分更新する (新しいサーバー / チャンネルは増やさない)。

        保存済みチャンネルの新しいスレッドは、通常のダンプと同じく取得する。
        """
        saved = self.saved_channels()
        if not saved:
            log(f"{self.out} に保存済みのチャンネルがありません")
            return
        tops: dict[str, set[str]] = {}  # guild_id -> 親チャンネル ID
        threads: dict[str, dict[str, Any]] = {}
        dms: set[str] = set()
        for ch in saved:
            if ch["type"] in (DM, GROUP_DM):
                dms.add(ch["id"])
            elif ch["type"] in THREAD_TYPES:
                threads[ch["id"]] = ch
            elif ch.get("guild_id"):
                tops.setdefault(ch["guild_id"], set()).add(ch["id"])
        log(f"保存済み: サーバー {len(tops)} / チャンネル {sum(map(len, tops.values()))}"
            f" / スレッド {len(threads)} / DM {len(dms)}")

        for gid, ids in tops.items():
            try:
                self.dump_guild(gid, ids)
            except ApiError as e:
                if e.status not in {403, 404}:
                    raise
                log(f"サーバー {gid} を取得できないためスキップ: {e}")
                continue
            for cid in sorted(ids - set(self._channel_cache)):
                log(f"# {cid}: サーバーに見つかりません (削除済み?)")

        # 親チャンネルを保存していないスレッド (dump channel <スレッド ID> で取ったもの)
        handled = set().union(*tops.values()) if tops else set()
        for tid, th in threads.items():
            if th.get("parent_id") in handled:
                continue
            self._update_by_id(tid)

        if dms:
            log("=== DM ===")
            listed = set()
            for ch in self.api.get("/users/@me/channels"):
                if ch["id"] in dms:
                    listed.add(ch["id"])
                    self.try_dump_channel(ch)
            # DM 一覧から閉じた DM も ID で直接取りにいく
            for cid in sorted(dms - listed):
                self._update_by_id(cid)

    def _update_by_id(self, channel_id: str) -> None:
        try:
            ch = self.channel(channel_id)
            if ch.get("guild_id") and ch["guild_id"] not in self._guild_cache:
                self._load_guild(ch["guild_id"])
        except ApiError as e:
            if e.status not in {403, 404}:
                raise
            log(f"# {channel_id}: 取得できないためスキップ: {e}")
            return
        self.try_dump_channel(ch)

    # ---- offline

    def rerender_all(self) -> None:
        """API を使わず、保存済みデータから txt / HTML を作り直す。"""
        for gj in self.out.glob("guilds/*/guild.json"):
            g = read_json(gj)
            if g:
                self._guild_cache[g["id"]] = g
        channel_files = sorted(self.out.rglob("channel.json"))
        for cj in channel_files:
            ch = read_json(cj)
            if ch:
                self._channel_cache[ch["id"]] = ch
        for cj in channel_files:
            d = cj.parent
            if not (d / "messages.jsonl").exists():
                continue
            ch = self._channel_cache[read_json(cj)["id"]]
            store = ChannelStore(d)
            msgs = store.load()
            self.render_channel(d, ch, store.sorted(msgs))
            log(f"# {render.channel_title(ch)}: {len(msgs)} 件")
        self.render_index()


# -------------------------------------------------------------------------- CLI


def cmd_guilds(api: DiscordApi, _: argparse.Namespace) -> None:
    for g in api.get("/users/@me/guilds"):
        print(f"{g['id']}\t{g['name']}")


def cmd_channels(api: DiscordApi, args: argparse.Namespace) -> None:
    for ch in sorted(api.get(f"/guilds/{args.guild_id}/channels"), key=lambda c: c.get("position", 0)):
        print(f"{ch['id']}\ttype={ch['type']}\t{ch.get('name')}")


def cmd_dms(api: DiscordApi, _: argparse.Namespace) -> None:
    for ch in api.get("/users/@me/channels"):
        print(f"{ch['id']}\t{'group' if ch['type'] == GROUP_DM else 'dm'}\t{dm_label(ch)}")


def parse_formats(value: str) -> set[str]:
    formats = {f.strip() for f in value.split(",") if f.strip()}
    if not formats or formats - set(FORMATS):
        raise argparse.ArgumentTypeError(f"{', '.join(FORMATS)} から選んでください")
    return formats


def cmd_dump(api: DiscordApi, args: argparse.Namespace) -> None:
    refresh_from = None
    if args.full:
        refresh_from = 0
    elif args.refresh_days:
        refresh_from = date_to_snowflake(datetime.now().astimezone() - timedelta(days=args.refresh_days))
    dumper = Dumper(
        api,
        Path(args.out),
        attachments=args.attachments,
        after=date_to_snowflake(args.after) if args.after else 0,
        before=date_to_snowflake(args.before) if args.before else None,
        refresh_from=refresh_from,
        threads=not args.no_threads,
        formats=args.format,
    )
    only = set(args.only) if args.only else None
    try:
        if args.target == "guild":
            for gid in args.ids:
                dumper.dump_guild(gid, only)
        elif args.target == "channel":
            for cid in args.ids:
                dumper.dump_any_channel(cid)
        elif args.target == "dms":
            dumper.dump_dms(only)
        elif args.target == "all":
            for g in api.get("/users/@me/guilds"):
                dumper.dump_guild(g["id"])
            dumper.dump_dms()
        elif args.target == "saved":
            dumper.dump_saved()
    finally:
        dumper.render_index()
    log("完了")


def cmd_render(_: None, args: argparse.Namespace) -> None:
    Dumper(None, Path(args.out), formats=args.format).rerender_all()


def cmd_gui(_: None, args: argparse.Namespace) -> None:
    import gui

    token = args.token or os.environ.get("DISCORD_TOKEN")
    gui.serve(Path(args.out), token, args.bot, args.delay, args.port, not args.no_browser)


def main() -> None:
    p = argparse.ArgumentParser(description="Discord のチャンネル / スレッド / DM をダンプする")
    p.add_argument("--token", help="トークン (省略時は環境変数 DISCORD_TOKEN)")
    p.add_argument("--bot", action="store_true", help="Bot トークンとして扱う")
    p.add_argument("--delay", type=float, default=0.3, help="リクエスト間の待機秒数 (既定 0.3)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("guilds", help="参加しているサーバー一覧").set_defaults(func=cmd_guilds)
    sp = sub.add_parser("channels", help="サーバーのチャンネル一覧")
    sp.add_argument("guild_id")
    sp.set_defaults(func=cmd_channels)
    sub.add_parser("dms", help="DM 一覧").set_defaults(func=cmd_dms)

    out_opts = argparse.ArgumentParser(add_help=False)
    out_opts.add_argument("-o", "--out", default="dump", help="出力先 (既定 ./dump)")
    out_opts.add_argument("--format", type=parse_formats, default=set(FORMATS),
                          help="出力形式 html,txt (既定は両方)")

    dp = sub.add_parser("dump", parents=[out_opts], help="メッセージをダンプ (2 回目以降は差分取得)")
    dp.add_argument("target", choices=["guild", "channel", "dms", "all", "saved"],
                    help="saved = 保存済みのチャンネル / スレッド / DM だけを更新")
    dp.add_argument("ids", nargs="*", help="guild / channel の ID")
    dp.add_argument("--only", nargs="+", metavar="CHANNEL_ID", help="guild / dms で対象チャンネルを限定")
    dp.add_argument("--attachments", action="store_true", help="添付ファイルもダウンロード")
    dp.add_argument("--no-threads", action="store_true", help="スレッドを取得しない")
    dp.add_argument("--after", help="この日時以降のみ (例 2025-01-01 / 2025-01-01T09:00+09:00)")
    dp.add_argument("--before", help="この日時より前のみ")
    dp.add_argument("--refresh-days", type=float, default=0, metavar="N",
                    help="直近 N 日分を取り直して編集・削除・リアクションの変化を反映")
    dp.add_argument("--full", action="store_true", help="全期間を取り直して編集・削除を反映")
    dp.set_defaults(func=cmd_dump)

    rp = sub.add_parser("render", parents=[out_opts], help="保存済みデータから HTML / txt を再生成 (API 不要)")
    rp.set_defaults(func=cmd_render)

    gp = sub.add_parser("gui", help="ブラウザでチャンネルを選んでダンプする GUI を起動")
    gp.add_argument("-o", "--out", default="dump", help="出力先の初期値 (既定 ./dump)")
    gp.add_argument("--port", type=int, default=0, help="待ち受けポート (既定は空きポート)")
    gp.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    gp.set_defaults(func=cmd_gui)

    args = p.parse_args()
    if args.command == "dump" and args.target in ("guild", "channel") and not args.ids:
        p.error(f"dump {args.target} には ID が必要です")

    api = None
    if args.command not in ("render", "gui"):
        token = args.token or os.environ.get("DISCORD_TOKEN")
        if not token:
            p.error("トークンを --token か環境変数 DISCORD_TOKEN で指定してください")
        api = DiscordApi(token.strip(), bot=args.bot, delay=args.delay)

    try:
        args.func(api, args)
    except ApiError as e:
        log(f"エラー: {e}")
        if e.status == 401:
            log("トークンが無効です")
        sys.exit(1)
    except KeyboardInterrupt:
        log("\n中断しました (再実行すると続きから取得します)")
        sys.exit(130)


if __name__ == "__main__":
    main()
