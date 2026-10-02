#!/usr/bin/env python3
"""Claude Code settings hook: stream a session's steps to Slack as formatted Block Kit
messages, so Projects threads can be followed live.

Two ways to send; set them only in the cloud environment (with neither set the hook does nothing):

- Slack bot, THREAD_LOG_BOT=1: each project posts to its own channel, #claude-<repo folder>
  (THREAD_LOG_CHANNEL overrides the name). When that channel is missing or the bot is not in it,
  messages go to THREAD_LOG_FALLBACK_CHANNEL (default claude-threads). Each session is one Slack
  thread: the parent message shows the first prompt and a status updated after every turn, and the
  steps are the replies (THREAD_LOG_THREADS=0 posts them straight in the channel instead). The bot
  token is added by the cloud environment's API credential (Bearer, for slack.com), so the session
  never sees it; for a local test THREAD_LOG_SLACK_TOKEN can carry it instead.
- Incoming webhook, THREAD_LOG_WEBHOOK=<url>: everything goes to the webhook's one channel.

What a message shows:
- one card per agent (main thread, or each subagent with its own colour), with a status
  (⏳ running / ✅ done / ⚠️ error) and its steps: commands in `code`, the last lines of
  output as a quote, edited files with +/- line counts
- at the end of each turn, a summary: duration, commands, files, subagents, errors

Steps are spooled and sent in batches every few seconds by one background flusher, so many
parallel subagents stay under Slack's ~1 message/second limit.

Other settings: THREAD_LOG_DRYRUN=1 prints the Slack payload for the event instead of sending it;
THREAD_LOG_FLUSH_SECONDS sets the batch interval (default 2); THREAD_LOG_TZ (e.g. Europe/Paris)
sets the time zone of the timestamps (default: the machine's).
"""
import fcntl
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime

WEBHOOK = os.environ.get("THREAD_LOG_WEBHOOK")
BOT = os.environ.get("THREAD_LOG_BOT") == "1"
TOKEN = os.environ.get("THREAD_LOG_SLACK_TOKEN")
API = os.environ.get("THREAD_LOG_API", "https://slack.com/api").rstrip("/")
THREADS = os.environ.get("THREAD_LOG_THREADS", "1") != "0"
DRYRUN = os.environ.get("THREAD_LOG_DRYRUN") == "1"
FLUSH = float(os.environ.get("THREAD_LOG_FLUSH_SECONDS", "2"))
if not (BOT or WEBHOOK or DRYRUN):
    sys.exit(0)

try:
    e = json.load(sys.stdin)
except (OSError, ValueError):
    sys.exit(0)

# Read-only tools are skipped to keep the channel readable; remove names to see everything.
SKIP = {"Read", "Glob", "Grep", "LS", "TodoWrite"}
EDITS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
AGENT_TOOLS = {"Agent", "Task"}
COLORS = ["🟦", "🟩", "🟨", "🟪", "🟧", "🟫", "🟥"]
SECRET = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]+|AKIA[0-9A-Z]{16}|Bearer\s+\S+)"
)


# ---------- text helpers (Slack mrkdwn) ----------

def clean(s, n):
    """Mask secrets, fold to one line, clip to n characters."""
    s = SECRET.sub("***", str(s or ""))
    s = " ⏎ ".join(p.strip() for p in s.splitlines() if p.strip())
    return s if len(s) <= n else s[: n - 1] + "…"


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def code(s, n):
    s = clean(s, n).replace("`", "ʼ")
    return f"`{esc(s)}`" if s else ""


def duration(sec):
    sec = int(max(sec, 0))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {sec % 3600 // 60:02d}m"


def lines_of(s):
    return len(str(s or "").splitlines()) or (1 if s else 0)


def clock():
    tz = os.environ.get("THREAD_LOG_TZ")
    if tz:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(tz)).strftime("%H:%M:%S")
        except (ImportError, KeyError, ValueError):  # unknown zone (ZoneInfoNotFoundError is a KeyError)
            pass
    return time.strftime("%H:%M:%S")


# ---------- one hook event -> one log entry ----------

def who(ev):
    aid = str(ev.get("agent_id") or "")
    if not aid:
        return "main", "⬜ *Thread chính*"
    color = COLORS[zlib.crc32(aid.encode()) % len(COLORS)]
    return aid, f"{color} *{esc(str(ev.get('agent_type') or 'subagent'))}* `{esc(aid[-4:])}`"


def output_tail(tr):
    out = f"{tr.get('stdout') or ''}\n{tr.get('stderr') or ''}" if isinstance(tr, dict) else str(tr or "")
    tail = [line for line in out.splitlines() if line.strip()][-3:]
    return [esc(clean(line, 150)) for line in tail]


def edit_text(tool, ti):
    path = ti.get("file_path") or ti.get("notebook_path") or ""
    if tool == "Edit":
        delta = f"+{lines_of(ti.get('new_string'))} −{lines_of(ti.get('old_string'))}"
    elif tool == "MultiEdit":
        edits = ti.get("edits") or []
        delta = (f"+{sum(lines_of(x.get('new_string')) for x in edits)} "
                 f"−{sum(lines_of(x.get('old_string')) for x in edits)}")
    elif tool == "Write":
        delta = f"+{lines_of(ti.get('content'))} (ghi file)"
    else:
        delta = ""
    return f"✏️ {code(path, 120)}  {delta}".rstrip()


def tool_text(tool, ti):
    arg = ti.get("url") or ti.get("query") or json.dumps(ti, ensure_ascii=False)
    return f"🔧 *{esc(tool)}* {code(arg, 120)}"


def describe(ev):
    """Return (kind, mrkdwn text, output lines) for this event, or None to skip it."""
    name = ev.get("hook_event_name")
    if name == "UserPromptSubmit":
        return "prompt", f"💬 {esc(clean(ev.get('prompt'), 220))}", []
    if name == "Stop":
        return "turn_end", "", []
    if name == "SubagentStart":
        return "start", "▶︎ Bắt đầu", []
    if name == "SubagentStop":
        msg = clean(ev.get("last_assistant_message"), 200)
        return "stop", "✅ Xong" + (f" — {esc(msg)}" if msg else ""), []
    tool = ev.get("tool_name", "?")
    ti = ev.get("tool_input") or {}
    if name == "PreToolUse":  # wired only for Agent/Task: shows the hand-off before it runs
        if tool not in AGENT_TOOLS:
            return None
        kind = esc(str(ti.get("subagent_type") or "subagent"))
        return "launch", f"🤖 Giao việc cho *{kind}*: {esc(clean(ti.get('description') or ti.get('prompt'), 160))}", []
    if tool in SKIP or tool in AGENT_TOOLS:  # Agent results are covered by SubagentStop
        return None
    if name == "PostToolUseFailure":
        what = code(ti.get("command"), 200) if tool == "Bash" else f"*{esc(tool)}*"
        return "fail", f"❌ {what}", [esc(clean(ev.get("error") or ev.get("tool_response"), 200))]
    if tool == "Bash":
        return "cmd", f"▸ {code(ti.get('command'), 200)}", output_tail(ev.get("tool_response"))
    if tool in EDITS:
        return "edit", edit_text(tool, ti), []
    return "tool", tool_text(tool, ti), []


# ---------- a batch of entries -> Slack Block Kit payloads ----------

def section(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def context(text):
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def short_id(sid):
    return re.sub(r"^(cse_|session_)", "", sid)[:8]


def step_blocks(items):
    """One card per agent with its steps, in order of first appearance."""
    groups = {}
    for it in items:
        groups.setdefault(it["who"], []).append(it)
    blocks = []
    for key, its in groups.items():
        kinds = {it["kind"] for it in its}
        errors = sum(it["kind"] == "fail" for it in its)
        err = f"{errors} lỗi"
        if key == "main":
            status = f"  ·  ⚠️ {err}" if errors else ""
        elif "stop" in kinds:
            status = "  ·  ✅ xong" + (f" ({err})" if errors else "")
        elif errors:
            status = f"  ·  ⚠️ {err}"
        else:
            status = "  ·  ⏳ đang chạy"
        if blocks:
            blocks.append({"type": "divider"})
        text = f"{its[0]['label']}{status}"
        for it in its:
            row = f"`{it['t']}`  {it['text']}" + "".join(f"\n>{o}" for o in it.get("out") or [])
            if len(text) + len(row) > 2800:  # Slack section limit is 3000 chars
                blocks.append(section(text))
                text = row
            else:
                text += "\n" + row
        blocks.append(section(text))
    return blocks


def summary_blocks(it):
    s = it.get("stats") or {}
    parts = [f"⌨️ {s.get('cmds', 0)} lệnh", f"✏️ {s.get('files', 0)} file", f"🤖 {s.get('subagents', 0)} subagent"]
    parts.append(f"❌ {s['errors']} lỗi" if s.get("errors") else "✓ không lỗi")
    return [section(f"🏁 *Lượt xong* sau *{duration(s.get('secs', 0))}*"), context("   ·   ".join(parts))]


def build_payloads(items, repo, sid, in_thread=False):
    """Slack messages for a batch. In a session thread the repo/thread header is left out,
    since the parent message already shows it."""
    steps = [it for it in items if it["kind"] != "turn_end"]
    ends = [it for it in items if it["kind"] == "turn_end"]
    blocks = step_blocks(steps)
    for it in ends:
        if blocks:
            blocks.append({"type": "divider"})
        blocks.extend(summary_blocks(it))
    if not blocks:
        return []
    short = short_id(sid)
    head = []
    if not in_thread:
        t0, t1 = items[0].get("t", ""), items[-1].get("t", "")
        span = t0 if t0 == t1 else f"{t0} → {t1}"
        head = [context(f"📂 *{esc(repo)}*  ·  thread `{esc(short)}`  ·  🕒 {span}")]
    fallback = (f"{repo} · {short}: " + (f"{len(steps)} bước" if steps else "")
                + (" · lượt xong" if ends else ""))
    per_msg = 49 - len(head)  # Slack allows 50 blocks per message
    return [{"text": fallback, "blocks": head + blocks[i:i + per_msg]} for i in range(0, len(blocks), per_msg)]


# ---------- record this event ----------

desc = describe(e)
if desc is None:
    sys.exit(0)
kind, text, out = desc
key, label = who(e)
entry = {"who": key, "label": label, "t": clock(), "kind": kind, "text": text, "out": out}

sid = re.sub(r"[^A-Za-z0-9_-]", "", str(e.get("session_id", "")))[:40] or "nosession"
repo = os.path.basename(str(e.get("cwd", "")).rstrip("/")) or "?"
CHANNEL = (os.environ.get("THREAD_LOG_CHANNEL") or f"claude-{repo}").lstrip("#")
FALLBACK = os.environ.get("THREAD_LOG_FALLBACK_CHANNEL", "claude-threads").lstrip("#")
base = os.path.join(tempfile.gettempdir(), "thread-log-dry" if DRYRUN else "thread-log")
os.makedirs(base, exist_ok=True)
spool = os.path.join(base, f"{sid}.spool")
state_path = os.path.join(base, f"{sid}.state")

with open(spool, "a") as f:
    fcntl.flock(f, fcntl.LOCK_EX)  # serialises spool and turn stats across parallel hooks
    try:
        with open(state_path) as sf:
            st = json.load(sf)
    except (OSError, ValueError):
        st = {}
    now = time.time()
    if kind == "prompt" or not st:
        st = {"start": now, "cmds": 0, "files": [], "subagents": 0, "errors": 0}
    if kind == "cmd":
        st["cmds"] += 1
    elif kind == "edit":
        path = (e.get("tool_input") or {}).get("file_path") or ""
        if path not in st["files"]:
            st["files"].append(path)
    elif kind == "start":
        st["subagents"] += 1
    elif kind == "fail":
        st["errors"] += 1
    if kind == "turn_end":
        entry["stats"] = {"secs": now - st["start"], "cmds": st["cmds"], "files": len(st["files"]),
                          "subagents": st["subagents"], "errors": st["errors"]}
        try:
            os.remove(state_path)
        except OSError:
            pass
    else:
        with open(state_path, "w") as sf:
            json.dump(st, sf)
    if not DRYRUN:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

if DRYRUN:
    payloads = build_payloads([entry], repo, sid)
    if BOT:
        payloads = [dict(p, channel=CHANNEL) for p in payloads]
    print(json.dumps(payloads, ensure_ascii=False, indent=1))
    sys.exit(0)

# ---------- become the flusher unless one is already running ----------

lock_fd = os.open(os.path.join(base, f"{sid}.flusher"), os.O_CREAT | os.O_RDWR)
try:
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(0)  # the running flusher will pick this entry up

if os.fork():  # return to Claude immediately; the child keeps the lock and does the sending
    sys.exit(0)
os.setsid()
devnull = os.open(os.devnull, os.O_RDWR)
for fd in (0, 1, 2):
    os.dup2(devnull, fd)


def take():
    with open(spool, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        items = []
        for raw in f:
            try:
                items.append(json.loads(raw))
            except ValueError:
                pass
        f.seek(0)
        f.truncate()
    return items


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_json(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


# ---------- incoming webhook ----------

def post_webhook(payload):
    body = json.dumps(payload).encode()
    for _ in range(4):
        try:
            req = urllib.request.Request(WEBHOOK, body, {"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=8)
            return
        except urllib.error.HTTPError as err:
            if err.code != 429:
                return
            time.sleep(float(err.headers.get("Retry-After") or 2))
        except (OSError, ValueError):  # network errors (URLError is an OSError) or a malformed URL
            return


def flush_webhook(items):
    for i, payload in enumerate(build_payloads(items, repo, sid)):
        if i:
            time.sleep(1.1)
        post_webhook(payload)


# ---------- Slack bot (Web API) ----------

CHANNELS = os.path.join(base, "channels.json")  # channel name -> id, shared by the sessions on this machine
THREAD = os.path.join(base, f"{sid}.slack")  # this session's channel, parent message and turn count
MOVED = {"channel_not_found", "not_in_channel", "is_archived"}
last_post = [0.0]


def slack(method, payload=None, query=None):
    """Call a Slack Web API method; return its JSON reply, or None when it could not be reached."""
    url = f"{API}/{method}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    body = json.dumps(payload).encode() if payload is not None else None
    for _ in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, body, headers), timeout=8) as r:
                return json.load(r)
        except urllib.error.HTTPError as err:
            if err.code != 429:
                return None
            time.sleep(float(err.headers.get("Retry-After") or 2))
        except (OSError, ValueError):  # network errors, or a reply that is not JSON
            return None
    return None


def find_channel(name):
    """Channel id for a name the bot can see (public, or private with the bot in it)."""
    if re.fullmatch(r"[CG][A-Z0-9]{8,}", name):
        return name
    cache = read_json(CHANNELS)
    if name in cache:
        return cache[name]
    cursor = ""
    for _ in range(20):
        r = slack("conversations.list", query={"types": "public_channel,private_channel",
                                               "exclude_archived": "true", "limit": 200, "cursor": cursor})
        if not r or not r.get("ok"):
            return None
        for c in r.get("channels") or []:
            if c.get("name") == name:
                cache[name] = c["id"]
                write_json(CHANNELS, cache)
                return c["id"]
        cursor = (r.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return None
    return None


def forget_channel(cid):
    cache = read_json(CHANNELS)
    write_json(CHANNELS, {k: v for k, v in cache.items() if v != cid})


def post(th, payload, reply=True):
    """Post a message in the session's channel, as a reply in its thread when it has one.
    Returns the message ts, or None."""
    for _ in range(2):
        if not th.get("channel"):
            th["channel"] = find_channel(CHANNEL) or find_channel(FALLBACK)
            if not th["channel"]:
                return None
        msg = dict(payload, channel=th["channel"], unfurl_links=False, unfurl_media=False)
        if reply and th.get("ts"):
            msg["thread_ts"] = th["ts"]
        wait = last_post[0] + 1.1 - time.time()  # chat.postMessage: about 1 message/second per channel
        if wait > 0:
            time.sleep(wait)
        r = slack("chat.postMessage", msg)
        last_post[0] = time.time()
        if r and r.get("ok"):
            return r.get("ts")
        if not r or r.get("error") not in MOVED:
            return None
        # The channel is gone, archived or the bot was removed: carry on in the fallback channel.
        forget_channel(th["channel"])
        fallback = find_channel(FALLBACK)
        th["channel"] = fallback if fallback != th["channel"] else None
        th["ts"] = None  # the thread stayed in the old channel; the next batch starts a new one
        if not th["channel"]:
            return None
    return None


def parent_payload(th, status):
    short = short_id(sid)
    prompt = th.get("prompt") or "💬 _(phiên đã chạy trước khi bật log)_"
    head = f"📂 *{esc(repo)}*  ·  thread `{esc(short)}`  ·  🕒 {th.get('start', '')}  ·  {status}"
    return {"text": f"{repo} · {short}: {status}", "blocks": [section(prompt), context(head)]}


def set_status(th, status):
    if th.get("ts") and th.get("channel"):
        slack("chat.update", dict(parent_payload(th, status), channel=th["channel"], ts=th["ts"]))


def flush_bot(items):
    th = read_json(THREAD)
    if not THREADS:
        for payload in build_payloads(items, repo, sid):
            post(th, payload, reply=False)
        write_json(THREAD, th)
        return
    steps = [it for it in items if it["kind"] != "turn_end"]
    ends = [it for it in items if it["kind"] == "turn_end"]
    if not th.get("ts"):
        first = next((it for it in steps if it["kind"] == "prompt"), None)
        if first and "prompt" not in th:  # the first prompt becomes the parent message
            th["prompt"] = first["text"]
            steps.remove(first)
        th.setdefault("start", (first or items[0])["t"])
        th["ts"] = post(th, parent_payload(th, "⏳ đang chạy"), reply=False)
        th["running"] = True
    elif not th.get("running") and any(it["kind"] == "prompt" for it in steps):
        set_status(th, "⏳ đang chạy")
        th["running"] = True
    for payload in build_payloads(steps + ends, repo, sid, in_thread=bool(th.get("ts"))):
        post(th, payload)
    for it in ends:
        s = it.get("stats") or {}
        th["turns"] = th.get("turns", 0) + 1
        icon = "⚠️" if s.get("errors") else "✅"
        set_status(th, f"{icon} xong lượt {th['turns']} lúc {it['t']} ({duration(s.get('secs', 0))})")
        th["running"] = False
    write_json(THREAD, th)


while True:
    time.sleep(FLUSH)
    items = take()
    if items:
        if BOT:
            flush_bot(items)
        else:
            flush_webhook(items)
        continue
    # Nothing left: step down, then catch an entry that arrived while we still held the lock.
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    if not os.path.exists(spool) or os.path.getsize(spool) == 0:
        break
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        break
