"""保存したメッセージを txt / HTML に整形する。"""
from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

CDN = "https://cdn.discordapp.com"

# 通常 / 返信 / スラッシュコマンド / コンテキストメニュー / 転送元など、本文として扱う種別
NORMAL_TYPES = {0, 19, 20, 21, 23}
SYSTEM_TEXT = {
    1: "がメンバーを追加しました",
    2: "がメンバーを削除しました",
    3: "が通話を開始しました",
    4: "がチャンネル名を変更しました",
    5: "がアイコンを変更しました",
    6: "がメッセージをピン留めしました",
    7: "がサーバーに参加しました",
    8: "がサーバーをブーストしました",
    9: "がサーバーをブーストしました (レベル 1 達成)",
    10: "がサーバーをブーストしました (レベル 2 達成)",
    11: "がサーバーをブーストしました (レベル 3 達成)",
    12: "がチャンネルをフォローしました",
    18: "がスレッドを作成しました",
    46: "の投票が終了しました",
}
CHANNEL_ICONS = {
    0: "#", 1: "@", 2: "🔊", 3: "👥", 5: "📢", 10: "🧵", 11: "🧵", 12: "🧵",
    13: "🎙", 15: "💬", 16: "🖼",
}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif"}
VIDEO_EXT = {".mp4", ".webm", ".mov"}
AUDIO_EXT = {".mp3", ".ogg", ".wav", ".m4a", ".flac"}


# ----------------------------------------------------------------------- helpers


def esc(s: Any) -> str:
    return html.escape(str(s), quote=True)


def local_time(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone()


def display_name(user: dict[str, Any] | None) -> str:
    if not user:
        return "unknown"
    return user.get("global_name") or user.get("username") or "unknown"


def user_label(user: dict[str, Any] | None) -> str:
    if not user:
        return "unknown"
    name = user.get("username", "unknown")
    if user.get("discriminator") not in (None, "0", "0000"):
        name += "#" + user["discriminator"]
    gname = user.get("global_name")
    return f"{gname} ({name})" if gname and gname != name else name


def dm_label(channel: dict[str, Any]) -> str:
    if channel.get("name"):
        return channel["name"]
    names = [display_name(r) for r in channel.get("recipients", [])]
    return ", ".join(names) or "dm"


def channel_title(ch: dict[str, Any]) -> str:
    icon = CHANNEL_ICONS.get(ch.get("type"), "#")
    name = dm_label(ch) if ch.get("type") in (1, 3) else ch.get("name") or ch["id"]
    return f"{icon} {name}"


def avatar_url(user: dict[str, Any]) -> str:
    if user.get("avatar"):
        return f"{CDN}/avatars/{user['id']}/{user['avatar']}.png?size=64"
    disc = user.get("discriminator") or "0"
    idx = int(disc) % 5 if disc not in ("0", "0000") else (int(user.get("id", 0)) >> 22) % 6
    return f"{CDN}/embed/avatars/{idx}.png"


def human_size(n: int | None) -> str:
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def file_src(msg: dict[str, Any], att: dict[str, Any]) -> str:
    """ダウンロード済みならローカルパス、なければ CDN の URL。"""
    return msg.get("_files", {}).get(att.get("id", "")) or att.get("url", "")


def system_text(msg: dict[str, Any]) -> str:
    t = msg.get("type")
    return display_name(msg.get("author")) + SYSTEM_TEXT.get(t, f" (システムメッセージ type {t})")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------- txt


def plain_content(msg: dict[str, Any]) -> str:
    content = msg.get("content") or ""
    for u in msg.get("mentions", []):
        content = re.sub(rf"<@!?{u['id']}>", "@" + display_name(u), content)
    return content


def text_message(msg: dict[str, Any]) -> str:
    ts = local_time(msg["timestamp"])
    if msg.get("type") not in NORMAL_TYPES:
        head = f"[{ts:%Y-%m-%d %H:%M:%S}] * {system_text(msg)}"
    else:
        head = f"[{ts:%Y-%m-%d %H:%M:%S}] {user_label(msg.get('author'))}:"
    if msg.get("edited_timestamp"):
        head += " (編集済み)"
    if msg.get("_deleted"):
        head += " (削除済み)"
    lines = [head]

    ref = msg.get("referenced_message")
    if ref:
        snippet = plain_content(ref).replace("\n", " ")[:80]
        lines.append(f"    ↪ 返信先 {user_label(ref.get('author'))}: {snippet}")
    lines.extend("    " + line for line in plain_content(msg).splitlines())
    for edit in msg.get("_edits", []):
        lines.append("    [編集前] " + (edit.get("content") or "").replace("\n", " "))
    for snap in msg.get("message_snapshots", []):
        lines.append("    ⤷ 転送されたメッセージ:")
        lines.extend("      " + line for line in (snap.get("message", {}).get("content") or "").splitlines())
    for att in msg.get("attachments", []):
        lines.append(f"    📎 {att.get('filename')} ({file_src(msg, att)})")
    for emb in msg.get("embeds", []):
        desc = emb.get("title") or emb.get("url") or (emb.get("description") or "")[:80]
        lines.append(f"    [埋め込み] {desc}")
    for st in msg.get("sticker_items", []):
        lines.append(f"    [スタンプ] {st.get('name')}")
    if msg.get("poll"):
        lines.append(f"    [投票] {msg['poll'].get('question', {}).get('text', '')}")
    if msg.get("reactions"):
        lines.append("    " + " ".join(
            f"{r['emoji'].get('name')}×{r.get('count', 0)}" for r in msg["reactions"]
        ))
    if msg.get("thread"):
        lines.append(f"    🧵 スレッド: {msg['thread'].get('name')} ({msg['thread']['id']})")
    return "\n".join(lines)


def render_text(messages: list[dict[str, Any]], path: Path) -> None:
    write_atomic(path, "".join(text_message(m) + "\n\n" for m in messages))


# ------------------------------------------------------------------ markdown


class Ctx:
    """HTML 整形中に参照する情報。"""

    def __init__(self, channel_dir: Path, names: dict[str, str], ids: set[str]):
        self.channel_dir = channel_dir
        self.names = names  # チャンネル / ロール ID → 名前
        self.ids = ids  # このページに存在するメッセージ ID

    def thread_link(self, thread_id: str) -> str | None:
        for p in (self.channel_dir / "threads").glob(f"*_{thread_id}"):
            if (p / "messages.html").exists():
                return f"threads/{p.name}/messages.html"
        return None


def _discord_ts(m: re.Match[str]) -> str:
    try:
        dt = datetime.fromtimestamp(int(m[1])).astimezone()
    except (ValueError, OverflowError, OSError):
        # datetime で扱えない範囲 (西暦 1〜9999 年の外など) はそのまま表示する
        return f'<code>{esc(m[0])}</code>'
    fmt = {"t": "%H:%M", "T": "%H:%M:%S", "d": "%Y/%m/%d", "D": "%Y年%m月%d日"}.get(
        m[2] or "f", "%Y年%m月%d日 %H:%M"
    )
    return f'<time class="ts" title="{dt:%Y-%m-%d %H:%M:%S}">{dt.strftime(fmt)}</time>'


def _inline(s: str) -> str:
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"__(.+?)__", r"<u>\1</u>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<em>\1</em>", s)
    s = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"<em>\1</em>", s)
    s = re.sub(r"~~(.+?)~~", r"<s>\1</s>", s)
    s = re.sub(r"\|\|(.+?)\|\|", r'<span class="spoiler" onclick="this.classList.add(\'shown\')">\1</span>', s)
    return s


def markdown(text: str, mentions: list[dict[str, Any]], ctx: Ctx) -> str:
    """Discord 風 Markdown の簡易レンダラー。"""
    if not text:
        return ""
    tokens: list[str] = []

    def keep(fragment: str) -> str:
        tokens.append(fragment)
        return f"\x00{len(tokens) - 1}\x00"

    users = {u["id"]: display_name(u) for u in mentions}

    def emoji(m: re.Match[str]) -> str:
        ext = "gif" if m[1] else "webp"
        return keep(f'<img class="emoji" src="{CDN}/emojis/{m[3]}.{ext}?size=48" alt=":{esc(m[2])}:" title=":{esc(m[2])}:">')

    text = re.sub(
        r"```(?:([\w+#.-]+)\n)?(.*?)```",
        lambda m: keep(f"<pre><code>{esc(m[2].strip(chr(10)))}</code></pre>"),
        text, flags=re.S,
    )
    text = re.sub(r"``(.+?)``|`([^`\n]+)`", lambda m: keep(f"<code>{esc(m[1] or m[2])}</code>"), text)
    text = re.sub(r"<@!?(\d+)>", lambda m: keep(f'<span class="mention">@{esc(users.get(m[1], m[1]))}</span>'), text)
    text = re.sub(r"<@&(\d+)>", lambda m: keep(f'<span class="mention">@{esc(ctx.names.get(m[1], "ロール"))}</span>'), text)
    text = re.sub(r"<#(\d+)>", lambda m: keep(f'<span class="mention">#{esc(ctx.names.get(m[1], m[1]))}</span>'), text)
    text = re.sub(r"<(a?):(\w+):(\d+)>", emoji, text)
    text = re.sub(r"<t:(-?\d+)(?::([tTdDfFR]))?>", lambda m: keep(_discord_ts(m)), text)
    text = re.sub(
        r"\[([^\]\n]+)\]\(<?(https?://[^\s)>]+)>?\)",
        lambda m: keep(f'<a href="{esc(m[2])}" target="_blank" rel="noopener">{esc(m[1])}</a>'),
        text,
    )
    text = re.sub(
        r"<?(https?://[^\s<>]*[^\s<>.,:;\"')\]!?])>?",
        lambda m: keep(f'<a href="{esc(m[1])}" target="_blank" rel="noopener">{esc(m[1])}</a>'),
        text,
    )

    out: list[str] = []
    quote: list[str] = []
    lines = esc(text).split("\n")
    for i, line in enumerate(lines):
        if line.startswith("&gt;&gt;&gt; "):
            quote.extend([line[13:], *lines[i + 1:]])
            break
        if line.startswith("&gt; ") or line == "&gt;":
            quote.append(line[5:])
            continue
        if quote:
            out.append('<div class="quote">' + "<br>".join(map(_inline, quote)) + "</div>")
            quote = []
        if m := re.match(r"(#{1,3}) (.+)", line):
            out.append(f'<div class="h{len(m[1])}">{_inline(m[2])}</div>')
        elif line.startswith("-# "):
            out.append(f'<div class="subtext">{_inline(line[3:])}</div>')
        elif m := re.match(r"(\s*)[-*] (.+)", line):
            out.append(f'<div class="li" style="margin-left:{len(m[1]) * 8}px">• {_inline(m[2])}</div>')
        else:
            out.append(_inline(line) + "<br>")
    if quote:
        out.append('<div class="quote">' + "<br>".join(map(_inline, quote)) + "</div>")

    rendered = "".join(out)
    rendered = re.sub(r"(<br>)+$", "", rendered)
    return re.sub(r"\x00(\d+)\x00", lambda m: tokens[int(m[1])], rendered)


# ---------------------------------------------------------------------- HTML


def _attachment(msg: dict[str, Any], att: dict[str, Any]) -> str:
    src = esc(file_src(msg, att))
    name = att.get("filename", "file")
    ext = os.path.splitext(name)[1].lower()
    ctype = att.get("content_type", "")
    spoiler = " spoiler-media" if name.startswith("SPOILER_") else ""
    if ctype.startswith("image/") or ext in IMAGE_EXT:
        return (f'<a class="media{spoiler}" href="{src}" target="_blank">'
                f'<img src="{src}" alt="{esc(name)}" loading="lazy"></a>')
    if ctype.startswith("video/") or ext in VIDEO_EXT:
        return f'<video class="media{spoiler}" controls preload="metadata" src="{src}"></video>'
    if ctype.startswith("audio/") or ext in AUDIO_EXT:
        return (f'<div class="file">🎵 {esc(name)}<br>'
                f'<audio controls preload="none" src="{src}"></audio></div>')
    return (f'<a class="file" href="{src}" target="_blank">📎 {esc(name)} '
            f'<small>{human_size(att.get("size"))}</small></a>')


def _embed(e: dict[str, Any], ctx: Ctx) -> str:
    t = e.get("type")
    if t == "gifv" and e.get("video"):
        return f'<video class="media" autoplay loop muted playsinline src="{esc(e["video"].get("url", ""))}"></video>'
    if t == "image" and e.get("thumbnail") and not e.get("title"):
        u = esc(e["thumbnail"].get("url", ""))
        return f'<a class="media" href="{u}" target="_blank"><img src="{u}" loading="lazy" alt=""></a>'

    color = f"#{e['color']:06x}" if isinstance(e.get("color"), int) else "var(--line)"
    parts = [f'<div class="embed" style="border-left-color:{color}">']
    if e.get("provider", {}).get("name"):
        parts.append(f'<div class="e-provider">{esc(e["provider"]["name"])}</div>')
    if e.get("author", {}).get("name"):
        parts.append(f'<div class="e-author">{esc(e["author"]["name"])}</div>')
    if e.get("title"):
        title = esc(e["title"])
        if e.get("url"):
            title = f'<a href="{esc(e["url"])}" target="_blank" rel="noopener">{title}</a>'
        parts.append(f'<div class="e-title">{title}</div>')
    if e.get("description"):
        parts.append(f'<div class="e-desc">{markdown(e["description"], [], ctx)}</div>')
    if e.get("fields"):
        parts.append('<div class="e-fields">')
        for f in e["fields"]:
            cls = "e-field inline" if f.get("inline") else "e-field"
            parts.append(f'<div class="{cls}"><b>{esc(f.get("name", ""))}</b>'
                         f'<div>{markdown(f.get("value", ""), [], ctx)}</div></div>')
        parts.append("</div>")
    img = (e.get("image") or {}).get("url") or (e.get("thumbnail") or {}).get("url")
    if img:
        parts.append(f'<img class="e-img" src="{esc(img)}" loading="lazy" alt="">')
    if e.get("footer", {}).get("text"):
        parts.append(f'<div class="e-footer">{esc(e["footer"]["text"])}</div>')
    parts.append("</div>")
    return "".join(parts)


def _poll(poll: dict[str, Any]) -> str:
    counts = {c["id"]: c.get("count", 0) for c in (poll.get("results") or {}).get("answer_counts", [])}
    rows = []
    for a in poll.get("answers", []):
        media = a.get("poll_media", {})
        emoji = (media.get("emoji") or {}).get("name") or ""
        rows.append(f'<li>{esc(emoji)} {esc(media.get("text", ""))} <small>{counts.get(a.get("answer_id"), 0)} 票</small></li>')
    return (f'<div class="poll"><b>📊 {esc(poll.get("question", {}).get("text", ""))}</b>'
            f'<ul>{"".join(rows)}</ul></div>')


def _reactions(reactions: list[dict[str, Any]]) -> str:
    items = []
    for r in reactions:
        em = r.get("emoji", {})
        if em.get("id"):
            ext = "gif" if em.get("animated") else "webp"
            icon = f'<img class="emoji" src="{CDN}/emojis/{em["id"]}.{ext}?size=32" alt=":{esc(em.get("name"))}:">'
        else:
            icon = esc(em.get("name", ""))
        items.append(f'<span class="reaction">{icon} {r.get("count", 0)}</span>')
    return f'<div class="reactions">{"".join(items)}</div>'


def _stickers(stickers: list[dict[str, Any]]) -> str:
    out = []
    for s in stickers:
        if s.get("format_type") in (1, 2, 4):
            ext = "gif" if s.get("format_type") == 4 else "png"
            out.append(f'<img class="sticker" src="https://media.discordapp.net/stickers/{s["id"]}.{ext}?size=160" '
                       f'alt="{esc(s.get("name"))}" title="{esc(s.get("name"))}" loading="lazy">')
        else:
            out.append(f'<div class="file">[スタンプ] {esc(s.get("name"))}</div>')
    return "".join(out)


def _extras(msg: dict[str, Any], ctx: Ctx) -> str:
    """添付・埋め込み・スタンプ・投票など本文以外の部分。"""
    parts = [_attachment(msg, a) for a in msg.get("attachments", [])]
    parts += [_embed(e, ctx) for e in msg.get("embeds", [])]
    if msg.get("sticker_items"):
        parts.append(_stickers(msg["sticker_items"]))
    if msg.get("poll"):
        parts.append(_poll(msg["poll"]))
    return "".join(parts)


def html_message(msg: dict[str, Any], ctx: Ctx, cont: bool) -> str:
    author = msg.get("author") or {}
    ts = local_time(msg["timestamp"])
    full = f"{ts:%Y-%m-%d %H:%M:%S}"
    system = msg.get("type") not in NORMAL_TYPES
    classes = ["msg"]
    if cont:
        classes.append("cont")
    if system:
        classes.append("system")
    if msg.get("_deleted"):
        classes.append("deleted")
    search = f"{display_name(author)} {author.get('username', '')} {msg.get('content') or ''}".lower()
    out = [f'<div class="{" ".join(classes)}" id="m{msg["id"]}" data-s="{esc(search)}">']

    ref = msg.get("referenced_message")
    if ref:
        snippet = esc(plain_content(ref).replace("\n", " ")[:120]) or "<i>添付ファイル</i>"
        target = f'href="#m{ref["id"]}"' if ref.get("id") in ctx.ids else ""
        out.append(f'<div class="reply"><img src="{esc(avatar_url(ref.get("author") or {}))}" alt="" loading="lazy">'
                   f'<b>{esc(display_name(ref.get("author")))}</b> <a {target}>{snippet}</a></div>')
    elif msg.get("type") == 19:
        out.append('<div class="reply"><i>元のメッセージは削除されました</i></div>')

    if system:
        out.append(f'<div class="gutter">→</div><div class="body"><span class="sys">{esc(system_text(msg))}</span>'
                   f' <time title="{full}">{ts:%Y/%m/%d %H:%M}</time>')
        if msg.get("content"):
            out.append(f'<div class="content">{markdown(msg["content"], msg.get("mentions", []), ctx)}</div>')
        out.append("</div></div>")
        return "".join(out)

    badges = ""
    if author.get("bot"):
        badges += '<span class="badge">BOT</span>'
    if msg.get("_deleted"):
        badges += '<span class="badge del">削除済み</span>'
    out.append(f'<img class="avatar" src="{esc(avatar_url(author))}" alt="" loading="lazy">')
    out.append(f'<time class="side" title="{full}">{ts:%H:%M}</time>')
    out.append('<div class="body">')
    out.append(f'<div class="head"><span class="name" title="{esc(user_label(author))}">'
               f'{esc(display_name(author))}</span>{badges}<time title="{full}">{ts:%Y/%m/%d %H:%M}</time></div>')
    if cont and msg.get("_deleted"):
        out.append('<span class="badge del">削除済み</span>')

    content = markdown(msg.get("content") or "", msg.get("mentions", []), ctx)
    if msg.get("edited_timestamp"):
        et = local_time(msg["edited_timestamp"])
        content += f' <span class="edited" title="{et:%Y-%m-%d %H:%M:%S}">(編集済み)</span>'
    if content:
        out.append(f'<div class="content">{content}</div>')

    if msg.get("_edits"):
        out.append(f'<details class="edits"><summary>編集履歴 ({len(msg["_edits"])})</summary>')
        for e in msg["_edits"]:
            when = local_time(e["edited_timestamp"]) if e.get("edited_timestamp") else None
            label = f"{when:%Y/%m/%d %H:%M}" if when else ""
            out.append(f'<div class="edit"><time>{label}</time>{markdown(e.get("content") or "", msg.get("mentions", []), ctx)}</div>')
        out.append("</details>")

    for snap in msg.get("message_snapshots", []):
        inner = snap.get("message", {})
        out.append('<div class="forward"><div class="fwd-label">↪ 転送されたメッセージ</div>')
        out.append(markdown(inner.get("content") or "", inner.get("mentions", []), ctx))
        out.append(_extras(inner, ctx) + "</div>")

    out.append(_extras(msg, ctx))
    if msg.get("reactions"):
        out.append(_reactions(msg["reactions"]))
    if msg.get("thread"):
        th = msg["thread"]
        link = ctx.thread_link(th["id"])
        label = f'🧵 {esc(th.get("name", ""))}'
        if th.get("message_count") is not None:
            label += f' <small>{th["message_count"]} 件</small>'
        out.append(f'<a class="thread" href="{link}">{label}</a>' if link else f'<div class="thread">{label}</div>')
    out.append("</div></div>")
    return "".join(out)


CSS = """
:root{--bg:#fff;--bg2:#f2f3f5;--fg:#2e3338;--muted:#5c5e66;--line:#e3e5e8;--link:#006ce7;
--mention-bg:rgba(88,101,242,.15);--mention-fg:#4752c4;--code:#ebedef;--hover:#f6f6f7;--spoiler:#b9bbbe;--del:#d83c3e;--del-bg:rgba(216,60,62,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#313338;--bg2:#2b2d31;--fg:#dbdee1;--muted:#949ba4;--line:#3f4147;
--link:#00a8fc;--mention-fg:#c9cdfb;--code:#2b2d31;--hover:#2e3035;--spoiler:#1e1f22;--del:#f23f43;--del-bg:rgba(242,63,67,.08)}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.375 system-ui,-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
header{position:sticky;top:0;z-index:2;display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:10px 16px;
background:var(--bg);border-bottom:1px solid var(--line)}
header h1{font-size:16px;margin:0}header .sub{color:var(--muted);font-size:13px}
header input{margin-left:auto;padding:6px 10px;border-radius:4px;border:1px solid var(--line);background:var(--bg2);color:var(--fg);min-width:200px}
#hits{color:var(--muted);font-size:13px}
main{padding:8px 0 40px}
.day{display:flex;align-items:center;margin:20px 16px 8px;color:var(--muted);font-size:12px;font-weight:600}
.day::before,.day::after{content:"";flex:1;height:1px;background:var(--line)}.day span{padding:0 8px}
.msg{display:grid;grid-template-columns:72px minmax(0,1fr);padding:2px 16px 2px 0;margin-top:16px}
.msg.cont{margin-top:0}.msg:hover{background:var(--hover)}.msg:target{background:var(--mention-bg)}
.msg.deleted{background:var(--del-bg)}
.reply,.body{grid-column:2}.avatar,.side,.gutter{grid-column:1;justify-self:center}
.avatar{width:40px;height:40px;border-radius:50%;margin-top:2px}
.side{display:none;font-size:11px;color:var(--muted);padding-top:3px}
.msg.cont .avatar,.msg.cont .head{display:none}.msg.cont .side{display:block;visibility:hidden}.msg.cont:hover .side{visibility:visible}
.searching .msg.cont .avatar{display:block}.searching .msg.cont .head{display:flex}.searching .msg.cont .side{display:none}
.searching .msg{margin-top:8px}.searching .day{display:none}
.head{display:flex;gap:8px;align-items:baseline}.name{font-weight:600}
.head time,.msg.system time{font-size:12px;color:var(--muted)}
.badge{font-size:10px;font-weight:700;padding:1px 4px;border-radius:3px;background:#5865f2;color:#fff}
.badge.del{background:var(--del)}
.content{white-space:normal;overflow-wrap:anywhere}
.edited{font-size:11px;color:var(--muted)}
.system .gutter{color:var(--muted)}.sys{color:var(--muted)}
.reply{display:flex;align-items:center;gap:4px;font-size:13px;color:var(--muted);min-width:0;white-space:nowrap;overflow:hidden}
.reply img{width:16px;height:16px;border-radius:50%}.reply a{color:var(--muted);overflow:hidden;text-overflow:ellipsis}
.reply b{color:var(--fg);font-weight:600}
code{background:var(--code);padding:1px 4px;border-radius:3px;font:13px ui-monospace,Menlo,monospace}
pre{background:var(--code);border:1px solid var(--line);border-radius:4px;padding:8px;overflow-x:auto;margin:4px 0;max-width:720px}
pre code{background:none;padding:0}
.quote{border-left:4px solid var(--line);padding-left:10px;margin:2px 0}
.h1{font-size:1.5em;font-weight:700}.h2{font-size:1.25em;font-weight:700}.h3{font-size:1.1em;font-weight:700}
.subtext{font-size:12px;color:var(--muted)}
.mention{background:var(--mention-bg);color:var(--mention-fg);border-radius:3px;padding:0 2px;font-weight:500}
.spoiler{background:var(--spoiler);color:transparent;border-radius:3px;cursor:pointer}.spoiler.shown{background:var(--code);color:inherit}
.spoiler-media{filter:blur(24px)}.spoiler-media:hover{filter:none}
.emoji{width:22px;height:22px;vertical-align:-5px}
.media{display:block;margin-top:4px;max-width:min(400px,100%)}
.media img,video.media{max-width:100%;max-height:350px;border-radius:6px;display:block}
.file{display:inline-block;margin-top:4px;padding:8px 12px;border:1px solid var(--line);border-radius:6px;background:var(--bg2)}
.file small{color:var(--muted)}
.embed{margin-top:4px;max-width:520px;padding:8px 12px;border-left:4px solid;border-radius:4px;background:var(--bg2);font-size:14px}
.e-provider,.e-footer{font-size:12px;color:var(--muted)}.e-author{font-weight:600;font-size:13px}.e-title{font-weight:700}
.e-fields{display:flex;flex-wrap:wrap;gap:8px;margin-top:4px}.e-field{flex:1 1 100%}.e-field.inline{flex:1 1 30%}
.e-img{max-width:100%;max-height:300px;border-radius:4px;margin-top:8px;display:block}
.sticker{width:160px;height:160px;margin-top:4px}
.poll{margin-top:4px;padding:8px 12px;border:1px solid var(--line);border-radius:6px;max-width:440px}
.poll ul{margin:4px 0 0;padding-left:20px}.poll small{color:var(--muted)}
.reactions{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px}
.reaction{display:inline-flex;align-items:center;gap:4px;padding:1px 6px;border-radius:8px;background:var(--bg2);border:1px solid var(--line);font-size:13px}
.reaction .emoji{width:16px;height:16px;vertical-align:0}
.thread{display:inline-block;margin-top:4px;padding:6px 10px;border-radius:6px;background:var(--bg2);font-size:13px}
.forward{border-left:3px solid var(--line);padding-left:10px;margin-top:4px}.fwd-label{font-size:12px;color:var(--muted)}
.edits{font-size:13px;color:var(--muted);margin-top:2px}.edits summary{cursor:pointer}
.edit{padding:2px 0 2px 8px;border-left:2px solid var(--line);margin:2px 0}.edit time{margin-right:8px;font-size:11px}
nav.index{padding:16px;max-width:900px}nav.index section{margin-bottom:24px}nav.index h2{font-size:18px;margin:0 0 8px}
nav.index ul{list-style:none;margin:0;padding-left:16px}nav.index>section>ul{padding-left:0}nav.index li{padding:2px 0}
nav.index small{color:var(--muted);margin-left:6px}nav.index .cat{font-size:12px;font-weight:700;color:var(--muted);text-transform:uppercase;margin-top:10px}
@media (max-width:600px){.msg{grid-template-columns:52px minmax(0,1fr);padding-right:8px}.avatar{width:34px;height:34px}
header input{margin-left:0;flex:1}}
"""

SEARCH_JS = """
const q=document.getElementById('q'),hits=document.getElementById('hits'),msgs=[...document.querySelectorAll('.msg')];
let t;q.addEventListener('input',()=>{clearTimeout(t);t=setTimeout(()=>{const v=q.value.trim().toLowerCase();let n=0;
for(const m of msgs){const ok=!v||m.dataset.s.includes(v);m.hidden=!ok;if(ok)n++}
document.body.classList.toggle('searching',!!v);hits.textContent=v?n+' 件':'';},150)});
"""


def _page(title: str, header: str, body: str, script: str = "") -> str:
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{esc(title)}</title><style>{CSS}</style></head><body>"
        f"<header>{header}</header><main>{body}</main>"
        + (f"<script>{script}</script>" if script else "")
        + "</body></html>"
    )


def render_html(
    messages: list[dict[str, Any]],
    path: Path,
    ch: dict[str, Any],
    subtitle: str,
    index_href: str,
    names: dict[str, str],
) -> None:
    ctx = Ctx(path.parent, names, {m["id"] for m in messages})
    body: list[str] = []
    prev: dict[str, Any] | None = None
    prev_day = None
    for msg in messages:
        ts = local_time(msg["timestamp"])
        day = ts.date()
        if day != prev_day:
            body.append(f'<div class="day"><span>{ts:%Y年%m月%d日}</span></div>')
            prev_day, prev = day, None
        cont = bool(
            prev
            and msg.get("type") in (0, 20)
            and prev.get("type") in NORMAL_TYPES
            and (prev.get("author") or {}).get("id") == (msg.get("author") or {}).get("id")
            and (ts - local_time(prev["timestamp"])).total_seconds() < 420
        )
        body.append(html_message(msg, ctx, cont))
        prev = msg
    if not messages:
        body.append('<p style="padding:16px;color:var(--muted)">メッセージはありません</p>')

    title = channel_title(ch)
    header = (
        f'<a href="{esc(index_href)}">← 一覧</a><h1>{esc(title)}</h1>'
        f'<span class="sub">{esc(subtitle)} · {len(messages)} 件</span>'
        '<input id="q" type="search" placeholder="メッセージを検索"><span id="hits"></span>'
    )
    write_atomic(path, _page(f"{title} - {subtitle}" if subtitle else title, header, "".join(body), SEARCH_JS))


# --------------------------------------------------------------------- index


def _channel_dirs(parent: Path) -> list[tuple[Path, dict[str, Any]]]:
    if not parent.is_dir():
        return []
    out = []
    for d in parent.iterdir():
        ch = read_json(d / "channel.json") if d.is_dir() else None
        if ch:
            out.append((d, ch))
    return out


def _last_id(d: Path) -> int:
    return int((read_json(d / "state.json") or {}).get("last_id") or 0)


def _item(d: Path, ch: dict[str, Any], root: Path) -> str:
    st = read_json(d / "state.json") or {}
    title = esc(channel_title(ch))
    page = d / "messages.html"
    label = f'<a href="{esc(os.path.relpath(page, root))}">{title}</a>' if page.exists() else title
    if "count" in st:
        label += f'<small>{st["count"]} 件</small>'
    threads = sorted(_channel_dirs(d / "threads"), key=lambda x: -_last_id(x[0]))
    sub = "".join(_item(td, tch, root) for td, tch in threads)
    return f"<li>{label}{f'<ul>{sub}</ul>' if sub else ''}</li>"


def render_index(root: Path) -> None:
    sections = []
    gdir = root / "guilds"
    guilds = sorted(gdir.iterdir(), key=lambda p: p.name.lower()) if gdir.is_dir() else []
    for g in guilds:
        guild = read_json(g / "guild.json")
        if not guild:
            continue
        all_channels = {c["id"]: c for c in read_json(g / "channels.json") or []}

        def sort_key(item: tuple[Path, dict[str, Any]]) -> tuple[int, int, int, int]:
            ch = item[1]
            cat = all_channels.get(ch.get("parent_id") or "", {})
            voice = 1 if ch.get("type") in (2, 13) else 0
            return (cat.get("position", -1), int(cat.get("id", 0)), voice, ch.get("position", 0))

        items, current_cat = [], None
        for d, ch in sorted(_channel_dirs(g), key=sort_key):
            cat = ch.get("parent_id")
            if cat != current_cat and cat in all_channels:
                items.append(f'<li class="cat">{esc(all_channels[cat].get("name", ""))}</li>')
            current_cat = cat
            items.append(_item(d, ch, root))
        sections.append(f'<section><h2>{esc(guild["name"])}</h2><ul>{"".join(items)}</ul></section>')

    dms = sorted(_channel_dirs(root / "dms"), key=lambda x: -_last_id(x[0]))
    if dms:
        items = "".join(_item(d, ch, root) for d, ch in dms)
        sections.append(f"<section><h2>ダイレクトメッセージ</h2><ul>{items}</ul></section>")

    body = f'<nav class="index">{"".join(sections) or "<p>まだ何もダンプされていません</p>"}</nav>'
    updated = datetime.now().astimezone()
    header = f'<h1>Discord ダンプ</h1><span class="sub">更新: {updated:%Y/%m/%d %H:%M}</span>'
    write_atomic(root / "index.html", _page("Discord ダンプ", header, body))
