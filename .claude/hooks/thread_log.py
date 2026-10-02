#!/usr/bin/env python3
"""Claude Code settings hook: stream a session's steps to Slack as formatted Block Kit
messages, so Projects threads can be followed live.

What a Slack message shows:
- a header line: repo · thread id · time range of the batch
- one card per agent (main thread, or each subagent with its own colour), with a status
  (⏳ running / ✅ done / ⚠️ error) and its steps: commands in `code`, the last lines of
  output as a quote, edited files with +/- line counts
- at the end of each turn, a summary: duration, commands, files, subagents, errors

Steps are spooled and sent in batches every few seconds by one background flusher, so many
parallel subagents don't trip Slack's ~1 message/second webhook limit.

Does nothing unless THREAD_LOG_WEBHOOK is set (set it only in the cloud environment).
THREAD_LOG_DRYRUN=1 prints the Slack payload for the event instead of sending it.
THREAD_LOG_FLUSH_SECONDS sets the batch interval (default 2).
"""
import fcntl
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib

URL = os.environ.get("THREAD_LOG_WEBHOOK")
DRYRUN = os.environ.get("THREAD_LOG_DRYRUN") == "1"
FLUSH = float(os.environ.get("THREAD_LOG_FLUSH_SECONDS", "2"))
if not URL and not DRYRUN:
    sys.exit(0)

try:
    e = json.load(sys.stdin)
except Exception:
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


# ---------- one hook event -> one log entry ----------

def who(ev):
    aid = str(ev.get("agent_id") or "")
    if not aid:
        return "main", "⬜ *Thread chính*"
    color = COLORS[zlib.crc32(aid.encode()) % len(COLORS)]
    return aid, f"{color} *{esc(str(ev.get('agent_type') or 'subagent'))}* `{esc(aid[-4:])}`"


def output_tail(tr):
    out = f"{tr.get('stdout') or ''}\n{tr.get('stderr') or ''}" if isinstance(tr, dict) else str(tr or "")
    tail = [l for l in out.splitlines() if l.strip()][-3:]
    return [esc(clean(l, 150)) for l in tail]


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


def build_payloads(items, repo, sid):
    t0, t1 = items[0].get("t", ""), items[-1].get("t", "")
    span = t0 if t0 == t1 else f"{t0} → {t1}"
    short = re.sub(r"^(cse_|session_)", "", sid)[:8]
    head = {"type": "context", "elements": [
        {"type": "mrkdwn", "text": f"📂 *{esc(repo)}*  ·  thread `{esc(short)}`  ·  🕒 {span}"}]}

    groups, ends = {}, []
    for it in items:
        if it["kind"] == "turn_end":
            ends.append(it)
        else:
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

    for it in ends:
        s = it.get("stats") or {}
        if blocks:
            blocks.append({"type": "divider"})
        blocks.append(section(f"🏁 *Lượt xong* sau *{duration(s.get('secs', 0))}*"))
        parts = [f"⌨️ {s.get('cmds', 0)} lệnh", f"✏️ {s.get('files', 0)} file",
                 f"🤖 {s.get('subagents', 0)} subagent"]
        parts.append(f"❌ {s['errors']} lỗi" if s.get("errors") else "✓ không lỗi")
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": "   ·   ".join(parts)}]})

    steps = sum(len(v) for v in groups.values())
    fallback = f"{repo} · {short}: " + (f"{steps} bước" if steps else "") + (" · lượt xong" if ends else "")
    payloads, per_msg = [], 46  # Slack allows 50 blocks per message
    for i in range(0, max(len(blocks), 1), per_msg):
        payloads.append({"text": fallback, "blocks": [head] + blocks[i:i + per_msg]})
    return payloads


# ---------- record this event ----------

desc = describe(e)
if desc is None:
    sys.exit(0)
kind, text, out = desc
key, label = who(e)
entry = {"who": key, "label": label, "t": time.strftime("%H:%M:%S"), "kind": kind, "text": text, "out": out}

sid = re.sub(r"[^A-Za-z0-9_-]", "", str(e.get("session_id", "")))[:40] or "nosession"
repo = os.path.basename(str(e.get("cwd", "")).rstrip("/")) or "?"
base = os.path.join(tempfile.gettempdir(), "thread-log-dry" if DRYRUN else "thread-log")
os.makedirs(base, exist_ok=True)
spool = os.path.join(base, f"{sid}.spool")
state_path = os.path.join(base, f"{sid}.state")

with open(spool, "a") as f:
    fcntl.flock(f, fcntl.LOCK_EX)  # serialises spool and turn stats across parallel hooks
    try:
        with open(state_path) as sf:
            st = json.load(sf)
    except Exception:
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
    print(json.dumps(build_payloads([entry], repo, sid), ensure_ascii=False, indent=1))
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


def post(payload):
    body = json.dumps(payload).encode()
    for _ in range(4):
        try:
            req = urllib.request.Request(URL, body, {"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=8)
            return
        except urllib.error.HTTPError as err:
            if err.code != 429:
                return
            time.sleep(float(err.headers.get("Retry-After") or 2))
        except Exception:
            return


while True:
    time.sleep(FLUSH)
    items = take()
    if items:
        for i, payload in enumerate(build_payloads(items, repo, sid)):
            if i:
                time.sleep(1.1)
            post(payload)
        continue
    # Nothing left: step down, then catch an entry that arrived while we still held the lock.
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    if not os.path.exists(spool) or os.path.getsize(spool) == 0:
        break
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        break
