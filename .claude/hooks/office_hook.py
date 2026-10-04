#!/usr/bin/env python3
"""Flowitup Office hook — reports Claude Code session activity to the office server.

Lives in each project repo at .claude/hooks/office_hook.py (install with install-hook.py from
github.com/flowitup/office). The project is the repo's name, so every repo shows up as its own room.

Runs on every hook event configured in .claude/settings.json. It:
  1. posts a small event (who, what tool, which branch) to   POST $OFFICE_URL/api/event
  2. ships any new transcript lines since the last call to   POST $OFFICE_URL/api/log/<session_id>

Design rules: stdlib only, never blocks Claude for long, never fails the hook.
Settings (cloud environment; with OFFICE_URL unset the hook does nothing):
  OFFICE_URL      e.g. https://office.flowitup.com
  OFFICE_TOKEN    optional. Leave it unset in the cloud environment and add an API credential
                  (Bearer <token>, for office.flowitup.com) instead, so the session never sees it.
                  Set it for local runs (Work locally / a terminal session).
  OFFICE_PROJECT  optional; default is the GitHub repo name (origin remote), else the folder name.
"""
import json, os, subprocess, sys, tempfile, urllib.request

URL = os.environ.get("OFFICE_URL", "").rstrip("/")
TOKEN = os.environ.get("OFFICE_TOKEN", "")
PROJECT = os.environ.get("OFFICE_PROJECT", "")
TIMEOUT = 4  # seconds per request
MAX_CHUNK = 2 * 1024 * 1024  # don't ship more than 2 MB per call


def post(path, body, ctype):
    # Cloudflare (in front of office.flowitup.com) rejects urllib's default User-Agent with error 1010.
    headers = {"Content-Type": ctype, "User-Agent": "motio-office-hook/1.0"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(URL + path, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        r.read()


def project_name(cwd):
    """The repo name from the origin remote (…/flowitup/motio.git → motio), else the project folder's name."""
    if PROJECT:
        return PROJECT
    root = os.environ.get("CLAUDE_PROJECT_DIR") or cwd or "."
    try:
        url = subprocess.run(["git", "-C", root, "config", "--get", "remote.origin.url"],
                             capture_output=True, text=True, timeout=2).stdout.strip()
        if url:
            name = url.rstrip("/").split("/")[-1].split(":")[-1]
            return name[:-4] if name.endswith(".git") else name
    except Exception:
        pass
    return os.path.basename(os.path.abspath(root)) or "unknown"


def branch(cwd):
    try:
        return subprocess.run(["git", "-C", cwd or ".", "branch", "--show-current"],
                              capture_output=True, text=True, timeout=2).stdout.strip() or None
    except Exception:
        return None


def summarize_input(tool, ti):
    """A short, non-sensitive-ish label for what the tool is doing."""
    if not isinstance(ti, dict):
        return None
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt"):
        v = ti.get(key)
        if isinstance(v, str) and v:
            return v[:160]
    return None


def ship_transcript(sid, tp):
    if not tp or not os.path.exists(tp):
        return
    state = os.path.join(tempfile.gettempdir(), f"motio-office-{sid}.offset")
    try:
        offset = int(open(state).read().strip())
    except Exception:
        offset = 0
    size = os.path.getsize(tp)
    reset = ""
    if size < offset:  # transcript rewritten — start over
        offset, reset = 0, "&reset=1"
    if size == offset:
        return
    with open(tp, "rb") as f:
        f.seek(offset)
        chunk = f.read(MAX_CHUNK)
    cut = chunk.rfind(b"\n")  # only ship complete lines
    if cut < 0:
        return
    chunk = chunk[: cut + 1]
    post(f"/api/log/{sid}?offset={offset}{reset}", chunk, "application/x-ndjson")
    with open(state, "w") as f:
        f.write(str(offset + len(chunk)))


def main():
    if not URL:
        return
    p = json.load(sys.stdin)
    ev = p.get("hook_event_name")
    sid = p.get("session_id")
    if not sid:
        return
    tool = p.get("tool_name")
    event = {
        "project": project_name(p.get("cwd")),
        "session_id": sid,
        "event": ev,
        "tool": tool,
        "detail": summarize_input(tool, p.get("tool_input")),
        "prompt": (p.get("prompt") or "")[:4000] or None,         # UserPromptSubmit
        "message": (p.get("message") or "")[:300] or None,       # Notification
        "notification_type": p.get("notification_type"),
        "source": p.get("source"),                               # SessionStart: startup/resume/compact
        # Subagents: SubagentStart/SubagentStop name the agent, and every event fired inside one carries its id.
        "agent_id": p.get("agent_id"),
        "agent_type": p.get("agent_type"),
        "subagent_type": (p.get("tool_input") or {}).get("subagent_type") if tool in ("Task", "Agent") else None,
        "stop_reason": p.get("stop_reason"),                     # SubagentStop
        "last_message": (p.get("last_assistant_message") or "")[:300] or None if ev == "SubagentStop" else None,
        "v": 2,  # this hook reports SubagentStart; the server then tracks subagents by id
        "reason": p.get("reason"),                               # SessionEnd
        "branch": branch(p.get("cwd")),
        "where": "cloud" if os.environ.get("CLAUDE_CODE_REMOTE") == "true" else "local",
        "host": os.uname().nodename if hasattr(os, "uname") else None,
    }
    try:
        post("/api/event", json.dumps(event).encode(), "application/json")
    except Exception:
        pass
    # Shipping the log on every tool call would be noisy; do it when something settled.
    if ev in ("PostToolUse", "PostToolUseFailure", "UserPromptSubmit", "Stop", "SubagentStop", "Notification", "SessionEnd", "PreCompact"):
        try:
            ship_transcript(sid, p.get("transcript_path"))
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)  # never block Claude
