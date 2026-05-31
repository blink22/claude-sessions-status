#!/usr/bin/env python3
"""Refreshing terminal dashboard showing all recently-active Claude Code
sessions and their current state. Run it in a small terminal window
pinned to your desktop for an at-a-glance view of what every session is
doing and what's pending.

Reads from ~/.claude/projects/<encoded>/sessions-index.json and the
transcript JSONL files. No external dependencies. Ctrl-C to exit.

Environment:
  CLAUDE_SESSIONS_REFRESH   refresh interval seconds (default 5)
  CLAUDE_SESSIONS_HOURS     show sessions modified within this window (default 24)
  CLAUDE_SESSIONS_LIMIT     max sessions to show (default 12)
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path


# Sibling module — env-file loader. SwiftBar runs this script as
# `<plugins>/claude-sessions-status.5s.py` (a symlink to this file). The
# import below uses a relative sibling import via sys.path insertion so
# the loader works whether we're run directly, via the symlink, or
# imported by menubar.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _env import load_env_file  # noqa: E402

# Load user config / API keys from the OSS env file.
ENV_FILE = Path(os.path.expanduser("~/.claude-sessions-status.env"))
load_env_file(ENV_FILE)


def _parse_iso_epoch(s: object) -> float | None:
    """Parse an ISO-8601 timestamp like '2026-05-16T02:05:35.728Z' into
    epoch seconds. Returns None for empty, missing, or malformed input.
    Used to read 'real conversation activity' time from JSONL entries
    rather than relying on the file's mtime — which gets bumped by
    metadata-only writes like ai-title / custom-title."""
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None

PROJECTS_DIR = Path(os.path.expanduser("~/.claude/projects"))
REFRESH_SECS = int(os.environ.get("CLAUDE_SESSIONS_REFRESH", "5"))
RECENT_HOURS = float(os.environ.get("CLAUDE_SESSIONS_HOURS", "24"))
MAX_ROWS = int(os.environ.get("CLAUDE_SESSIONS_LIMIT", "12"))

# ANSI helpers
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
RED = "\033[31m"
BLUE = "\033[34m"
CLEAR = "\033[2J\033[H"

# Where the dashboard remembers its last-chosen view (list vs kanban).
# Sibling to ~/.claude-sessions-status-popover-mode and -panel-mode used
# by floating.py — terminal keeps its own preference so kanban can be on
# in the popover while the terminal stays in list mode (or vice-versa).
HOME = Path(os.path.expanduser("~"))
DASHBOARD_MODE_FILE = HOME / ".claude-sessions-status-dashboard-mode"

# Quick-resume hotkey state (digit keys 1-9 in the main loop).
# `_NUMBERED_SESSIONS` is rebuilt by render() every frame and indexed by
# the keypress handler. `_LAST_ACTION_MSG` is a one-shot footer string set
# by the keypress handler (e.g. "→ resumed session 3 …" or "no session
# at slot 3"); render() prints it once, then clears it.
QUICK_RESUME_MAX = 9
_NUMBERED_SESSIONS: list[dict] = []
_LAST_ACTION_MSG: str | None = None


# Common emojis we emit. Most render as 2 terminal cells; everything else
# is 1. Lets us pad/truncate columns without pulling in `wcwidth`.
_WIDE_CHARS: frozenset[str] = frozenset(
    "🎨🛠🔍🌀📋❓💬✅📁📌🔔⚙️📥💤🪲🎯⚠️🤔🪛📝🧠🪛🧪"
)
# Pre-compiled ANSI stripper for visible-width calculations.
import re as _re  # local alias; we only need it in helpers
_ANSI_RE = _re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _term_width(s: str) -> int:
    """Approximate the number of terminal cells `s` occupies. Strips ANSI
    escapes and counts wide emojis as 2. Good enough for our card layout
    — the alternative (`wcwidth`) is a hard dependency."""
    plain = _ANSI_RE.sub("", s)
    w = 0
    for ch in plain:
        w += 2 if ch in _WIDE_CHARS else 1
    return w


def _clip_w(s: str, max_width: int) -> str:
    """Truncate `s` to `max_width` terminal cells, appending '…' if cut.
    Assumes `s` is plain text (no ANSI). Callers that pass pre-styled
    strings should style AFTER clipping."""
    s = s.replace("\n", " ").replace("\r", " ")
    if _term_width(s) <= max_width:
        return s
    out: list[str] = []
    w = 0
    for ch in s:
        cw = 2 if ch in _WIDE_CHARS else 1
        if w + cw > max_width - 1:
            out.append("…")
            return "".join(out)
        out.append(ch)
        w += cw
    return "".join(out)


def _pad_w(s: str, width: int) -> str:
    """Right-pad `s` with spaces so it occupies `width` terminal cells.
    Tolerates ANSI escapes — pad count is based on visible width."""
    n = _term_width(s)
    if n >= width:
        return s
    return s + " " * (width - n)


DASHBOARD_MODES = ("list", "kanban", "tasks", "ai")


def _read_dashboard_mode() -> str:
    try:
        v = DASHBOARD_MODE_FILE.read_text(encoding="utf-8").strip()
        if v in DASHBOARD_MODES:
            return v
    except OSError:
        pass
    return "list"


def _write_dashboard_mode(mode: str) -> None:
    if mode not in DASHBOARD_MODES:
        return
    try:
        DASHBOARD_MODE_FILE.write_text(mode, encoding="utf-8")
    except OSError:
        pass


def _load_index(proj_dir: Path) -> tuple[dict[str, dict], str]:
    """Return ({sessionId or fullPath -> entry}, originalProjectPath)
    parsed from this project's sessions-index.json. Used only to look up
    titles — mtimes come from the transcript files themselves because the
    index isn't always updated on every turn."""
    idx_path = proj_dir / "sessions-index.json"
    if not idx_path.exists():
        return {}, ""
    try:
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, ""
    by_key: dict[str, dict] = {}
    entries = idx.get("entries") if isinstance(idx, dict) else None
    original_path = idx.get("originalPath", "") if isinstance(idx, dict) else ""
    if isinstance(entries, list):
        for e in entries:
            if not isinstance(e, dict):
                continue
            sid = e.get("sessionId")
            fp = e.get("fullPath")
            if sid:
                by_key[sid] = e
            if fp:
                by_key[fp] = e
    return by_key, original_path


def find_sessions() -> list[dict]:
    """Enumerate top-level transcript files across all projects, ground
    truth from filesystem mtimes. Returns one dict per session, merged
    with any title/metadata from the project's sessions-index.json."""
    if not PROJECTS_DIR.exists():
        return []
    out: list[dict] = []
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        index_lookup, original_path = _load_index(proj_dir)
        # Only top-level .jsonl files — skip subagent transcripts that
        # live in subdirectories, they're internal to a parent session.
        for jsonl in proj_dir.glob("*.jsonl"):
            try:
                st = jsonl.stat()
            except OSError:
                continue
            session_id = jsonl.stem
            indexed = index_lookup.get(session_id) or index_lookup.get(str(jsonl)) or {}
            entry = {
                "sessionId": session_id,
                "fullPath": str(jsonl),
                "fileMtime": st.st_mtime * 1000,  # ms, parity with index format
                "_originalPath": (
                    original_path
                    or indexed.get("projectPath", "")
                    or proj_dir.name.lstrip("-").replace("-", "/")
                ),
                "summary": indexed.get("summary"),
                "firstPrompt": indexed.get("firstPrompt"),
                "messageCount": indexed.get("messageCount"),
            }
            out.append(entry)
    return out


def recent_sessions(sessions: list[dict]) -> list[dict]:
    cutoff_ms = (time.time() - RECENT_HOURS * 3600) * 1000
    fresh = [s for s in sessions if s.get("fileMtime", 0) > cutoff_ms]
    fresh.sort(key=lambda s: s.get("fileMtime", 0), reverse=True)
    return fresh[:MAX_ROWS]


def _text_from_content(content) -> str:
    """Pull a single readable text snippet from a message.content field."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                return b["text"].strip()
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                return f"running tool: {b.get('name', '?')}"
    return ""


# ---------- sub-agents (Task-tool-spawned children) ----------
# When a parent agent calls the `Task` tool, Claude Code writes the
# sub-agent's transcript to `<projects>/<proj>/<parent-sess>/subagents/
# agent-<id>.jsonl`, paired with a tiny `agent-<id>.meta.json` containing
# {agentType, description, toolUseId}. We surface a count + breakdown on
# each parent's row so the user can see "3 agents · 2 done · 1 working"
# without opening the session. Pure presentation — does not affect
# bucket/state classification of the parent itself.
SUBAGENT_MAX_DISPLAY = 5           # cards/popover/menubar show at most N
SUBAGENT_RUNNING_GRACE_SECS = 60   # mtime newer than this ⇒ still running

# state strings (kept as bare ASCII so menubar/SwiftBar pass them
# through untouched):
SUBAGENT_RUNNING = "running"
SUBAGENT_DONE = "done"
SUBAGENT_INTERRUPTED = "interrupted"

# Cache keyed on agent JSONL path. Stores (jsonl_mtime, state, last_epoch).
# Most sub-agents finish quickly and never change again — once we see a
# terminal state on a stable file we never re-read it. Running agents are
# the only ones we re-stat each tick.
_subagent_state_cache: dict[str, tuple[float, str, float]] = {}


def _peek_last_line(p: Path) -> dict | None:
    """Read just the final JSONL line and decode it. Returns None on
    any failure (empty file, mid-write, malformed JSON)."""
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            tail = deque(f, maxlen=1)
    except OSError:
        return None
    if not tail:
        return None
    try:
        return json.loads(tail[-1])
    except (ValueError, TypeError):
        return None


def _derive_subagent_state(jsonl_path: Path, now_ts: float) -> str:
    """Three-state classifier: running / done / interrupted. Cheap path
    is mtime ⇒ running; for stale files we peek the last JSONL line."""
    try:
        mtime = jsonl_path.stat().st_mtime
    except OSError:
        return SUBAGENT_DONE  # treat unreadable as terminal
    if now_ts - mtime < SUBAGENT_RUNNING_GRACE_SECS:
        return SUBAGENT_RUNNING
    last = _peek_last_line(jsonl_path)
    if last is None:
        return SUBAGENT_DONE
    msg = last.get("message") if isinstance(last, dict) else None
    if not isinstance(msg, dict):
        return SUBAGENT_DONE
    # Interrupt marker is always a user-role entry with literal text.
    if last.get("type") == "user":
        content = msg.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict) and first.get("type") == "text":
                text = first.get("text", "") or ""
        if isinstance(text, str) and text.startswith("[Request interrupted"):
            return SUBAGENT_INTERRUPTED
    return SUBAGENT_DONE


def subagents_for_session(parent_jsonl_path: str, now_ts: float) -> list[dict]:
    """Return one dict per sub-agent under this parent session, sorted
    by mtime descending (most recently active first). Empty list if the
    session has no `subagents/` subdir.

    Each dict: {id, name, agent_type, state, last_epoch}.
    """
    # Sub-agents live at `<proj>/<sess-uuid>/subagents/agent-*.jsonl` —
    # a sibling DIR next to the parent's `<sess-uuid>.jsonl`, NOT inside
    # the same JSONL file.
    p = Path(parent_jsonl_path)
    subdir = p.parent / p.stem / "subagents"
    if not subdir.is_dir():
        return []

    results: list[dict] = []
    try:
        entries = list(subdir.glob("agent-*.meta.json"))
    except OSError:
        return []
    for meta_p in entries:
        try:
            with meta_p.open("r", encoding="utf-8", errors="ignore") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        agent_id = meta_p.stem[:-5] if meta_p.stem.endswith(".meta") else meta_p.stem
        # `.meta.json` → stem is `agent-<id>.meta`. Strip the trailing `.meta`.
        jsonl_p = subdir / (agent_id + ".jsonl")
        # Stat jsonl up-front so we can both cache-key on its mtime and use
        # it as `last_epoch` for sorting.
        try:
            mtime = jsonl_p.stat().st_mtime
        except OSError:
            mtime = 0.0
        # Cache lookup: stable terminal states never change.
        cached = _subagent_state_cache.get(str(jsonl_p))
        if cached and cached[0] == mtime and cached[1] != SUBAGENT_RUNNING:
            state = cached[1]
        else:
            state = _derive_subagent_state(jsonl_p, now_ts)
            _subagent_state_cache[str(jsonl_p)] = (mtime, state, mtime)
        results.append({
            "id": agent_id,
            "name": (meta.get("description") or "").strip() or "agent",
            "agent_type": meta.get("agentType") or "",
            "state": state,
            "last_epoch": mtime,
        })

    # Most-recently-active first. Within same mtime, stable.
    results.sort(key=lambda r: r["last_epoch"], reverse=True)
    return results


# ---------- TodoWrite-derived tasks ----------
# Claude Code's built-in `TodoWrite` tool lets the model lay out its plan
# as a list of {content, activeForm, status} entries. Each subsequent
# TodoWrite call is a FULL SNAPSHOT — so the last one in the JSONL is
# the ground truth for "what is this session working on right now."
#
# We surface that as a Tasks view: per-session vertical list, running
# sub-agents nested under the (typically unique) in_progress todo.
#
# Status values seen in the wild: "pending" | "in_progress" | "completed".
TODO_PENDING = "pending"
TODO_IN_PROGRESS = "in_progress"
TODO_COMPLETED = "completed"

# Cache: parent_jsonl_path → (mtime, todos list). Stable terminal states
# never invalidate; running sessions re-scan only when the file grows.
_todos_cache: dict[str, tuple[float, list[dict]]] = {}


def todos_for_session(parent_jsonl_path: str) -> list[dict]:
    """Return the most-recent TodoWrite snapshot from this session's
    parent transcript. Empty list when the session never called the
    tool (which is fine — most short sessions don't).

    Each todo is a dict: {content, activeForm, status, index}.
    `index` preserves array position from the snapshot — the only
    stable identity TodoWrite gives us (no `id` field exists)."""
    p = Path(parent_jsonl_path)
    try:
        current_mtime = p.stat().st_mtime
    except OSError:
        return []
    cached = _todos_cache.get(parent_jsonl_path)
    if cached and cached[0] == current_mtime:
        return cached[1]

    last_todos: list[dict] = []
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                # Cheap pre-filter: TodoWrite is a tool name string, so
                # only fully-parse lines that mention it.
                if "TodoWrite" not in line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                msg = obj.get("message") if isinstance(obj, dict) else None
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and block.get("name") == "TodoWrite"
                    ):
                        todos_input = (
                            block.get("input", {}).get("todos")
                            if isinstance(block.get("input"), dict) else None
                        )
                        if isinstance(todos_input, list):
                            last_todos = todos_input
    except OSError:
        return []

    # Normalize: keep only the fields we care about, add stable index.
    out: list[dict] = []
    for i, t in enumerate(last_todos):
        if not isinstance(t, dict):
            continue
        out.append({
            "index": i,
            "content": t.get("content") or "",
            "activeForm": t.get("activeForm") or "",
            "status": t.get("status") or TODO_PENDING,
        })

    _todos_cache[parent_jsonl_path] = (current_mtime, out)
    return out


def todo_summary(todos: list[dict]) -> dict:
    """Aggregate counts by status — useful for chip / header rendering."""
    counts = {TODO_PENDING: 0, TODO_IN_PROGRESS: 0, TODO_COMPLETED: 0}
    for t in todos:
        st = t.get("status") or TODO_PENDING
        if st in counts:
            counts[st] += 1
        else:
            counts[TODO_PENDING] += 1
    return {
        "total": len(todos),
        "pending": counts[TODO_PENDING],
        "in_progress": counts[TODO_IN_PROGRESS],
        "completed": counts[TODO_COMPLETED],
    }


def find_in_progress_todo(todos: list[dict]) -> dict | None:
    """Return the first in_progress todo, or None. Claude Code's
    convention is to keep exactly one todo in_progress at a time, so
    "first" is almost always "only" — but we don't enforce that."""
    for t in todos:
        if t.get("status") == TODO_IN_PROGRESS:
            return t
    return None


def subagent_summary(subs: list[dict]) -> dict:
    """Aggregate counts by state. Used by the renderers."""
    counts = {SUBAGENT_RUNNING: 0, SUBAGENT_DONE: 0, SUBAGENT_INTERRUPTED: 0}
    for s in subs:
        st = s.get("state") or SUBAGENT_DONE
        if st in counts:
            counts[st] += 1
        else:
            counts[SUBAGENT_DONE] += 1
    return {
        "total": len(subs),
        "running": counts[SUBAGENT_RUNNING],
        "done": counts[SUBAGENT_DONE],
        "interrupted": counts[SUBAGENT_INTERRUPTED],
    }


# Caches keyed on session file path. Stores (mtime, meta) tuples so
# we can skip the full transcript scan whenever the file hasn't grown
# since last read. Combined with `cwd` / `firstPrompt` being immutable
# once seen, this turns a 20-session refresh from ~20 disk reads into
# ~0 in the steady state (only re-scanning sessions that actually had
# a new turn).
_meta_cache: dict[str, tuple[float, dict]] = {}


def transcript_meta(transcript_path: str) -> dict:
    """Single-pass scan returning everything the dashboard needs.

    Key disambiguation: the LAST assistant entry can be (a) text only —
    Claude has finished its turn and is genuinely waiting on the user, or
    (b) tool_use only — Claude is mid-flight, waiting for its own tool
    result to come back. These look identical via `last_role` alone, so
    we expose `lastAssistantHasText` for downstream logic to tell them
    apart."""
    p = Path(transcript_path)
    blank = {
        "cwd": None, "firstPrompt": None,
        "lastRole": None, "lastAction": "",
        "lastAssistantHasText": False,
        "lastAssistantText": "", "recentTools": [],
        "lastAssistantTools": [],
    }
    if not p.exists():
        # Return the cached value if we have one — better than blank.
        prev = _meta_cache.get(transcript_path)
        return (prev[1] if prev else blank)

    # mtime-based cache hit: skip the JSONL scan entirely if the file
    # hasn't changed since the last computation.
    try:
        current_mtime = p.stat().st_mtime
    except OSError:
        prev = _meta_cache.get(transcript_path)
        return (prev[1] if prev else blank)
    prev = _meta_cache.get(transcript_path)
    if prev is not None and prev[0] == current_mtime:
        return prev[1]
    cached = prev[1] if prev else None

    try:
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            tail = list(deque(f, maxlen=80))
    except OSError:
        return cached or blank

    last_role = None
    last_action = ""
    last_assistant_text = ""
    last_assistant_has_text = False
    recent_tools: list[str] = []
    # Tools called in the MOST RECENT assistant entry only (subset of
    # recent_tools). Used to detect interactive tools — AskUserQuestion
    # and ExitPlanMode — that block on user input, so they need to
    # surface as "needs you" rather than "working".
    last_assistant_tools: list[str] = []
    custom_title: str | None = None
    ai_title: str | None = None
    # Real-conversation timestamp — captured from the most recent
    # user-or-assistant entry. File mtime can lag reality by hours
    # because the GUI keeps appending metadata-only entries.
    last_turn_epoch: float | None = None
    # The latest "substantive" user prompt (>= 10 chars, not a one-word
    # affirmation). Drives the gist line / Haiku context for "what is
    # Claude actually working on right now".
    latest_user_prompt: str | None = None

    # Walk the tail in reverse to find: most recent entry's role, the most
    # recent assistant entry's content, and tool calls from the last few
    # assistant entries (window of recent activity). Also picks up the
    # most recent title entries (user-set or AI-generated).
    found_last_role = False
    found_last_assistant = False
    assistant_entries_seen = 0
    for raw in reversed(tail):
        try:
            e = json.loads(raw)
        except json.JSONDecodeError:
            continue
        t = e.get("type")
        # Title entries can appear anywhere in the file; capture the most
        # recent of each type. User overrides take precedence later.
        if t == "custom-title" and custom_title is None:
            ct = e.get("customTitle") or e.get("title")
            if isinstance(ct, str) and ct.strip():
                custom_title = ct.strip()
            continue
        if t == "ai-title" and ai_title is None:
            at = e.get("aiTitle") or e.get("title")
            if isinstance(at, str) and at.strip():
                ai_title = at.strip()
            continue
        if t not in ("user", "assistant"):
            continue
        msg = e.get("message") or {}
        content = msg.get("content")
        # Capture the timestamp of the most recent user/assistant entry —
        # NOT the file's mtime, which gets bumped by metadata writes.
        if last_turn_epoch is None:
            ts = _parse_iso_epoch(e.get("timestamp"))
            if ts is not None:
                last_turn_epoch = ts
        # Capture the latest substantive user prompt (skip short replies
        # like "yes", "ok"). Used by the gist feature.
        if t == "user" and latest_user_prompt is None:
            text = _text_from_content(content)
            if (
                text
                and len(text.strip()) >= 10
                and not text.startswith("<local-command-caveat")
            ):
                latest_user_prompt = text.strip()
        if not found_last_role:
            last_role = t
            last_action = _text_from_content(content)[:90]
            found_last_role = True
        if t == "assistant":
            assistant_entries_seen += 1
            if not found_last_assistant:
                # Capture text + tool_use from the MOST RECENT assistant entry.
                if isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "text" and b.get("text"):
                            last_assistant_text = b["text"]
                            last_assistant_has_text = True
                        elif b.get("type") == "tool_use":
                            last_assistant_tools.append(b.get("name", ""))
                elif isinstance(content, str) and content.strip():
                    last_assistant_text = content
                    last_assistant_has_text = True
                found_last_assistant = True
            # Collect tool names from the LAST 3 assistant entries only
            # (phase trend detection). Beyond that, stop accumulating
            # tools but KEEP walking the tail — title entries (custom-title
            # / ai-title) typically sit just past recent activity, and a
            # premature break would miss them, leaving titles undetected.
            if assistant_entries_seen <= 3 and isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        recent_tools.append(b.get("name", ""))

    cwd = cached.get("cwd") if cached else None
    first_prompt = cached.get("firstPrompt") if cached else None
    if not cwd or not first_prompt:
        try:
            with p.open("r", encoding="utf-8", errors="ignore") as f:
                for raw in f:
                    try:
                        e = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not cwd and e.get("cwd"):
                        cwd = e["cwd"]
                    if not first_prompt and e.get("type") == "user":
                        txt = _text_from_content((e.get("message") or {}).get("content"))
                        if txt and not txt.startswith("<local-command-caveat"):
                            first_prompt = txt[:80]
                    if cwd and first_prompt:
                        break
        except OSError:
            pass

    meta = {
        "cwd": cwd,
        "firstPrompt": first_prompt,
        "lastRole": last_role,
        "lastAction": last_action,
        "lastAssistantText": last_assistant_text,
        "lastAssistantHasText": last_assistant_has_text,
        "recentTools": recent_tools,
        "lastAssistantTools": last_assistant_tools,
        "customTitle": custom_title,
        "aiTitle": ai_title,
        "lastTurnEpoch": last_turn_epoch,
        "latestUserPrompt": latest_user_prompt,
    }
    _meta_cache[transcript_path] = (current_mtime, meta)
    return meta


# ---------- gist: "what Claude is actually working on" ----------
GIST_CACHE_FILE = Path(os.path.expanduser("~/.claude-sessions-status-cache.json"))
GIST_MAX_WORDS = 8
GIST_MAX_CHARS = 60
HAIKU_MODEL = "claude-haiku-4-5-20251001"
_AI_KEY_WARNED = False   # one-shot guard for the "AI mode on but no key" log


def _load_gist_cache() -> dict:
    if not GIST_CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(GIST_CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_gist_cache(cache: dict) -> None:
    try:
        GIST_CACHE_FILE.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass


def _transcript_line_count(path: str) -> int | None:
    """Cheap proxy for 'has the transcript grown'. Reads file size as a
    fast invalidation key — much cheaper than counting newlines, and
    works because JSONL only grows by append."""
    try:
        return os.stat(path).st_size
    except OSError:
        return None


def _free_gist(meta: dict) -> str | None:
    """Free heuristic — latest substantive user prompt, truncated."""
    prompt = meta.get("latestUserPrompt") or meta.get("firstPrompt")
    if not isinstance(prompt, str):
        return None
    p = prompt.strip().replace("\n", " ")
    if len(p) <= GIST_MAX_CHARS:
        return p
    # Trim at a word boundary near the limit.
    cut = p[:GIST_MAX_CHARS].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:!?") + "…"


def _ai_gist_log(msg: str) -> None:
    """Best-effort log to ~/.claude-sessions-status.log so debugging is
    possible without changing menu output."""
    try:
        Path(os.path.expanduser("~/.claude-sessions-status.log")).open("a").write(
            f"[gist] {msg}\n"
        )
    except OSError:
        pass


def _ai_gist_call_haiku(meta: dict) -> str | None:
    """One Haiku call. Returns the short phrase, or None on any failure."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    user_prompt = (meta.get("latestUserPrompt") or meta.get("firstPrompt") or "")[:1500]
    assistant_text = (meta.get("lastAssistantText") or "")[:1500]
    last_action = (meta.get("lastAction") or "")[:300]
    recent_tools = meta.get("recentTools") or []
    if not user_prompt and not assistant_text and not last_action:
        return None

    system_prompt = (
        "You label a Claude Code session for a dashboard. Read the recent "
        "context and produce ONE short phrase (max 8 words, max 60 chars) "
        "describing what's currently being worked on. Examples:\n"
        "  'Fixing bottom sheet padding bug'\n"
        "  'Planning suhoor alarm UX redesign'\n"
        "  'Refactoring auth middleware to use tokens'\n"
        "  'Asking which database to use'\n"
        "Output ONLY the phrase. No quotes, no preamble, no period."
    )
    body_obj = {
        "model": HAIKU_MODEL,
        "max_tokens": 40,
        "system": system_prompt,
        "messages": [
            {
                "role": "user",
                "content": (
                    f"User's latest message:\n{user_prompt}\n\n"
                    f"Assistant's recent text:\n{assistant_text}\n\n"
                    f"Assistant's last action: {last_action}\n"
                    f"Recent tool calls: {', '.join(recent_tools[:5])}\n"
                ),
            }
        ],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body_obj).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        _ai_gist_log(f"haiku call failed: {e!r}")
        return None
    blocks = data.get("content") or []
    out = " ".join(
        b.get("text", "") for b in blocks if b.get("type") == "text"
    ).strip()
    if not out:
        return None
    # Strip stray quotes / period the model sometimes adds.
    if len(out) >= 2 and out[0] in '"“' and out[-1] in '"”':
        out = out[1:-1].strip()
    out = out.rstrip(".")
    if len(out) > GIST_MAX_CHARS:
        out = out[: GIST_MAX_CHARS - 1].rstrip() + "…"
    return out


def _ai_gist(session_id: str, transcript_path: str, meta: dict) -> str | None:
    """Cached Haiku gist. Re-generates only when the transcript file size
    has changed since the last cached gist — i.e., one Haiku call per
    real turn per session, not one per SwiftBar refresh."""
    global _AI_KEY_WARNED
    if not os.environ.get("ANTHROPIC_API_KEY"):
        if not _AI_KEY_WARNED:
            _ai_gist_log("CLAUDE_SESSIONS_AI is on but ANTHROPIC_API_KEY is empty; falling back to free heuristic")
            _AI_KEY_WARNED = True
        return None
    if not session_id or not transcript_path:
        return None

    size = _transcript_line_count(transcript_path)
    cache = _load_gist_cache()
    entry = cache.get(session_id)
    if (
        isinstance(entry, dict)
        and isinstance(entry.get("size"), int)
        and entry["size"] == size
        and isinstance(entry.get("gist"), str)
        and entry["gist"]
    ):
        return entry["gist"]

    gist = _ai_gist_call_haiku(meta)
    if gist:
        cache[session_id] = {"gist": gist, "size": size, "ts": time.time()}
        _save_gist_cache(cache)
    return gist


def session_gist(session: dict, meta: dict, bucket: str | None = None) -> str | None:
    """Top-level entry point. Returns a short phrase describing what
    Claude is currently working on for this session, or None if nothing
    useful can be inferred.

    AI mode (CLAUDE_SESSIONS_AI=1) calls Haiku with caching. If the call
    fails OR the key is missing, falls back to the free heuristic. The
    free heuristic uses the latest substantive user prompt verbatim.

    DORMANT sessions skip the AI call to save money (the user has
    already moved past them — no need for a fresh phrase)."""
    ai_on = os.environ.get("CLAUDE_SESSIONS_AI", "").strip() in ("1", "true", "yes")
    if ai_on and bucket != "dormant":
        ai = _ai_gist(
            session.get("sessionId") or "",
            session.get("fullPath") or "",
            meta,
        )
        if ai:
            return ai
    return _free_gist(meta)


# ---------- AI Tasks (LLM-derived task list per session) ----------
# A bounded slice of each session's transcript goes to Haiku; the model
# returns a structured list of "what's this session actually working on"
# tasks. Separate from the TodoWrite mirror (which can be stale or
# missing) — this tab is "intelligence layered on top".
#
# Cost: ~$0.004/call. Gated by (real-turn-occurred AND size-grew-≥2KB AND
# ≥60s-since-last-call). Worker thread keeps the LLM out of the UI tick.
AI_TASKS_CACHE_FILE = Path(os.path.expanduser("~/.claude-sessions-status-ai-tasks.json"))
AI_TASK_MAX = 6
AI_TASK_RECLASSIFY_COOLDOWN_S = 60
AI_TASK_SIZE_DELTA_BYTES = 2048
AI_TASK_INPUT_USER_PROMPTS = 20
AI_TASK_INPUT_ASSISTANT_SNIPPETS = 5
AI_TASK_HTTP_TIMEOUT = 12
AI_TASK_FAIL_BACKOFF_S = 300  # after a hard error, don't retry for 5 min

# Statuses — kept identical to TodoWrite (UX agent's call). Haiku returns
# these strings; we validate on parse.
AI_TASK_PENDING = "pending"
AI_TASK_IN_PROGRESS = "in_progress"
AI_TASK_COMPLETED = "completed"
AI_TASK_STATUSES = (AI_TASK_PENDING, AI_TASK_IN_PROGRESS, AI_TASK_COMPLETED)


def _ai_tasks_log(msg: str) -> None:
    """Append a line to ~/.claude-sessions-status.log. Reuses gist log
    file so users have one place to look for AI-feature diagnostics."""
    try:
        log_p = Path(os.path.expanduser("~/.claude-sessions-status.log"))
        with log_p.open("a", encoding="utf-8") as f:
            f.write(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ai-tasks] {msg}\n"
            )
    except OSError:
        pass


# Cache shape: {session_id: {tasks, session_intent, classified_size,
#                            classified_ts, last_turn_epoch, model,
#                            failure_count, last_failure_ts}}
_ai_tasks_cache_lock = None  # set lazily on first use
_ai_tasks_cache_mem: dict | None = None


def _ai_tasks_lock():
    """Thread-safety primitive for the on-disk + in-memory cache."""
    global _ai_tasks_cache_lock
    if _ai_tasks_cache_lock is None:
        import threading
        _ai_tasks_cache_lock = threading.RLock()
    return _ai_tasks_cache_lock


def _load_ai_tasks_cache() -> dict:
    """Read the on-disk cache once into memory. Subsequent reads use the
    in-memory copy — writers mutate it under the lock and flush."""
    global _ai_tasks_cache_mem
    with _ai_tasks_lock():
        if _ai_tasks_cache_mem is not None:
            return _ai_tasks_cache_mem
        if not AI_TASKS_CACHE_FILE.exists():
            _ai_tasks_cache_mem = {}
            return _ai_tasks_cache_mem
        try:
            data = json.loads(AI_TASKS_CACHE_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        _ai_tasks_cache_mem = data
        return _ai_tasks_cache_mem


def _save_ai_tasks_cache_entry(session_id: str, entry: dict) -> None:
    """Merge one session's entry into the cache and atomically flush."""
    with _ai_tasks_lock():
        cache = _load_ai_tasks_cache()
        cache[session_id] = entry
        try:
            tmp = AI_TASKS_CACHE_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(cache, indent=0), encoding="utf-8")
            os.replace(tmp, AI_TASKS_CACHE_FILE)
        except OSError as e:
            _ai_tasks_log(f"cache write failed: {e!r}")


def _build_ai_task_payload(jsonl_path: str, todos: list[dict]) -> dict:
    """Construct the bounded input slice we send to Haiku.

    Returns: {first_prompt, user_prompts, assistant_snippets,
              tool_histogram, todo_snapshot}. The slice size is bounded
              regardless of transcript length — that's the whole point.
    """
    p = Path(jsonl_path)
    if not p.exists():
        return {}

    user_prompts: list[str] = []
    assistant_snippets: list[str] = []
    tool_counts: dict[str, int] = {}
    first_prompt = ""

    try:
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                t = obj.get("type")
                msg = obj.get("message") if isinstance(obj, dict) else None
                if not isinstance(msg, dict):
                    continue
                if t == "user":
                    text = _text_from_content(msg.get("content"))
                    if not text:
                        continue
                    # Skip metadata-looking entries (task notifications,
                    # interrupts, tool-result wrappers) — they bloat the
                    # slice without informing intent.
                    if text.startswith("<") or text.startswith("[Request"):
                        continue
                    text = text[:500]
                    if not first_prompt:
                        first_prompt = text
                    if len(text) >= 10:
                        user_prompts.append(text)
                elif t == "assistant":
                    content = msg.get("content")
                    if isinstance(content, list):
                        for b in content:
                            if not isinstance(b, dict):
                                continue
                            if b.get("type") == "text":
                                txt = (b.get("text") or "").strip()
                                if txt:
                                    assistant_snippets.append(txt[:400])
                            elif b.get("type") == "tool_use":
                                name = b.get("name") or "?"
                                tool_counts[name] = tool_counts.get(name, 0) + 1
    except OSError:
        return {}

    # Keep only the last N of each, to bound the payload.
    user_prompts = user_prompts[-AI_TASK_INPUT_USER_PROMPTS:]
    assistant_snippets = assistant_snippets[-AI_TASK_INPUT_ASSISTANT_SNIPPETS:]
    # Top 10 tools by count (the histogram is the *summary* of activity,
    # so a full enumeration would just be noise).
    top_tools = sorted(tool_counts.items(), key=lambda kv: -kv[1])[:10]

    todo_snapshot = [
        {"content": t.get("content", "")[:160], "status": t.get("status", "")}
        for t in (todos or [])
    ][:30]

    return {
        "first_prompt": first_prompt[:400],
        "user_prompts": user_prompts,
        "assistant_snippets": assistant_snippets,
        "tool_histogram": top_tools,
        "todo_snapshot": todo_snapshot,
    }


_AI_TASKS_SYSTEM_PROMPT = (
    "You extract a current task list from a Claude Code session transcript "
    "for a developer dashboard. You will receive: the session's first user "
    "prompt, the last 20 user prompts, the last 5 assistant text snippets, "
    "a tool-use histogram, and the session's most recent TodoWrite snapshot "
    "(may be empty or stale).\n\n"
    "Return STRICT JSON matching this schema:\n"
    "{\n"
    "  \"session_intent\": \"<one short phrase—what this session is fundamentally about>\",\n"
    "  \"tasks\": [\n"
    "    {\"title\": str, \"status\": str, \"summary\": str, \"evidence\": str, \"confidence\": float}\n"
    "  ]\n"
    "}\n\n"
    "Rules:\n"
    "- 1–6 tasks max. Group related TodoWrite items into ONE task when they share intent—\n"
    "  TodoWrite's granularity is often too fine; collapse aggressively.\n"
    "- status ∈ {\"pending\",\"in_progress\",\"completed\"}.\n"
    "  \"in_progress\" = work clearly underway in the last few user/assistant turns.\n"
    "  \"completed\" = explicitly finished AND not contradicted later.\n"
    "  \"pending\" = upcoming or proposed but not started.\n"
    "- title: ≤ 60 chars, imperative phrasing (\"Fix X\", not \"Fixing X\").\n"
    "- summary: 1–2 plain-text sentences. No markdown. Describe the WHAT and WHY.\n"
    "- evidence: one verbatim quote (≤ 120 chars) from user or assistant message.\n"
    "- confidence: float in [0,1]. Use ≤ 0.5 when signals are sparse or contradictory.\n"
    "- If the transcript is too short to identify any task, return tasks: [].\n"
    "Output JSON only. No prose, no markdown fences."
)


def _ai_task_call_haiku(payload: dict) -> dict | None:
    """One Haiku call. Returns parsed JSON dict or None on any failure."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    user_content = (
        f"First user prompt:\n{payload.get('first_prompt') or '(empty)'}\n\n"
        f"Recent user prompts (most recent last):\n"
        + "\n---\n".join(payload.get("user_prompts") or []) + "\n\n"
        f"Recent assistant snippets:\n"
        + "\n---\n".join(payload.get("assistant_snippets") or []) + "\n\n"
        f"Tool usage (name: count):\n"
        + "\n".join(f"  {n}: {c}" for n, c in payload.get("tool_histogram") or [])
        + "\n\n"
        + "Most recent TodoWrite snapshot (may be empty):\n"
        + json.dumps(payload.get("todo_snapshot") or [], indent=0)
    )
    body_obj = {
        "model": HAIKU_MODEL,
        "max_tokens": 900,
        "system": _AI_TASKS_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_content}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body_obj).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=AI_TASK_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except Exception as e:  # noqa: BLE001
        _ai_tasks_log(f"haiku call failed: {e!r}")
        return None
    blocks = data.get("content") or []
    raw = " ".join(
        b.get("text", "") for b in blocks if b.get("type") == "text"
    ).strip()
    if not raw:
        return None
    # Strip markdown fences the model sometimes adds despite instructions.
    if raw.startswith("```"):
        raw = raw.strip("`")
        # Drop a leading "json\n" if present.
        if raw.lower().startswith("json"):
            raw = raw[4:].lstrip()
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as e:
        _ai_tasks_log(f"haiku response parse failed: {e!r}; raw[:300]={raw[:300]!r}")
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _normalize_ai_task_response(parsed: dict) -> dict:
    """Validate + clamp the LLM output. Drops malformed tasks, enforces
    cap, normalizes statuses."""
    intent = parsed.get("session_intent") or ""
    if not isinstance(intent, str):
        intent = ""
    raw_tasks = parsed.get("tasks") or []
    if not isinstance(raw_tasks, list):
        raw_tasks = []
    clean: list[dict] = []
    for t in raw_tasks:
        if not isinstance(t, dict):
            continue
        title = (t.get("title") or "").strip()
        if not title:
            continue
        status = (t.get("status") or "").strip()
        if status not in AI_TASK_STATUSES:
            status = AI_TASK_PENDING
        summary = (t.get("summary") or "").strip()
        evidence = (t.get("evidence") or "").strip()
        confidence = t.get("confidence")
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))
        clean.append({
            "title": title[:80],
            "status": status,
            "summary": summary[:300],
            "evidence": evidence[:160],
            "confidence": round(confidence, 2),
        })
        if len(clean) >= AI_TASK_MAX:
            break
    return {"session_intent": intent[:120], "tasks": clean}


# Worker thread + queue. The refresh tick enqueues jobs; the worker pops
# them serially and writes results to the cache. Single worker keeps
# concurrency bounded — heavy bursts queue up but won't fan out into
# many parallel HTTP requests.
_ai_task_queue = None
_ai_task_worker = None
_ai_task_inflight: set = set()
_ai_task_inflight_lock = None


def _ai_task_inflight_lock_get():
    global _ai_task_inflight_lock
    if _ai_task_inflight_lock is None:
        import threading
        _ai_task_inflight_lock = threading.Lock()
    return _ai_task_inflight_lock


def _start_ai_task_worker() -> None:
    global _ai_task_queue, _ai_task_worker
    import threading, queue as _q
    if _ai_task_worker is not None and _ai_task_worker.is_alive():
        return
    if _ai_task_queue is None:
        _ai_task_queue = _q.Queue()
    _ai_task_worker = threading.Thread(
        target=_ai_task_worker_loop, daemon=True, name="ai-tasks-worker",
    )
    _ai_task_worker.start()


def _ai_task_worker_loop() -> None:
    """Single-threaded consumer. Pops jobs serially and classifies."""
    while True:
        job = _ai_task_queue.get()  # blocks
        if job is None:
            return  # shutdown sentinel
        try:
            _classify_session(job)
        except Exception as e:  # noqa: BLE001
            _ai_tasks_log(f"worker exception: {e!r}")
        finally:
            sid = job.get("session_id")
            with _ai_task_inflight_lock_get():
                _ai_task_inflight.discard(sid)


def _classify_session(job: dict) -> None:
    """Classify ONE session. Reads its current state, calls Haiku,
    writes the result to the cache. Best-effort — failures degrade
    silently (failure_count + last_failure_ts get logged for backoff)."""
    session_id = job.get("session_id")
    jsonl_path = job.get("jsonl_path")
    todos = job.get("todos") or []
    size = job.get("size", 0)
    last_turn_epoch = job.get("last_turn_epoch", 0.0)
    if not session_id or not jsonl_path:
        return

    payload = _build_ai_task_payload(jsonl_path, todos)
    if not payload or not payload.get("user_prompts"):
        # Nothing meaningful to classify yet — write an empty entry so
        # the gate logic doesn't keep retrying immediately.
        _save_ai_tasks_cache_entry(session_id, {
            "tasks": [],
            "session_intent": "",
            "classified_size": size,
            "classified_ts": time.time(),
            "last_turn_epoch": last_turn_epoch,
            "model": HAIKU_MODEL,
            "failure_count": 0,
            "last_failure_ts": 0,
        })
        return

    parsed = _ai_task_call_haiku(payload)
    if parsed is None:
        # Hard failure — bump the backoff counter so we don't hammer.
        with _ai_tasks_lock():
            cache = _load_ai_tasks_cache()
            prev = cache.get(session_id) or {}
            prev["failure_count"] = (prev.get("failure_count") or 0) + 1
            prev["last_failure_ts"] = time.time()
            cache[session_id] = prev
            try:
                AI_TASKS_CACHE_FILE.write_text(json.dumps(cache, indent=0), encoding="utf-8")
            except OSError:
                pass
        return

    norm = _normalize_ai_task_response(parsed)
    _save_ai_tasks_cache_entry(session_id, {
        "tasks": norm["tasks"],
        "session_intent": norm["session_intent"],
        "classified_size": size,
        "classified_ts": time.time(),
        "last_turn_epoch": last_turn_epoch,
        "model": HAIKU_MODEL,
        "failure_count": 0,
        "last_failure_ts": 0,
    })


def _ai_tasks_enabled() -> bool:
    """Independent toggle from CLAUDE_SESSIONS_AI (gist mode). A user
    can have one feature on and not the other."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    return os.environ.get("CLAUDE_SESSIONS_AI_TASKS", "").strip() in (
        "1", "true", "yes", "on"
    )


def ai_tasks_for_session(
    session_id: str,
    jsonl_path: str,
    meta: dict,
    todos: list[dict],
    bucket: str | None = None,
) -> dict:
    """Return the cached AI task result for a session. Enqueues a
    re-classification if the cache is stale and all gates pass.

    Returns a dict shaped like the cache entry:
        {tasks, session_intent, classified_ts, last_turn_epoch,
         failure_count, last_failure_ts, status} where status is one of:
         "ok" | "computing" | "stale" | "errored" | "disabled" | "empty".
    """
    # Fast path: AI tasks disabled.
    if not _ai_tasks_enabled():
        return {"status": "disabled", "tasks": []}

    # Don't classify dormant sessions — they're not changing, and the
    # cost would be wasted. Existing cached result (if any) is fine to
    # show though.
    cache = _load_ai_tasks_cache()
    entry = cache.get(session_id) or {}
    if bucket == "dormant":
        return {
            **entry,
            "status": "ok" if entry.get("tasks") else "empty",
        }

    # Compute gate values.
    try:
        size = Path(jsonl_path).stat().st_size
    except OSError:
        size = 0
    last_size = entry.get("classified_size", 0)
    last_ts = entry.get("classified_ts", 0)
    last_turn_epoch = meta.get("lastTurnEpoch") or 0
    cached_turn_epoch = entry.get("last_turn_epoch", 0)
    now = time.time()
    failures = entry.get("failure_count", 0) or 0
    last_failure_ts = entry.get("last_failure_ts", 0) or 0

    # Backoff after repeated failures.
    in_backoff = (
        failures > 0
        and (now - last_failure_ts) < AI_TASK_FAIL_BACKOFF_S
    )

    # Three gates must all pass to fire a new classification.
    has_data = size > 0
    size_grew = (size - last_size) >= AI_TASK_SIZE_DELTA_BYTES
    new_turn = last_turn_epoch and last_turn_epoch > cached_turn_epoch
    cooled = (now - last_ts) >= AI_TASK_RECLASSIFY_COOLDOWN_S
    # First-time classification is allowed even without size delta /
    # cooldown — we just need data.
    first_time = not entry and has_data and last_turn_epoch

    should_classify = (
        not in_backoff
        and has_data
        and (first_time or (size_grew and new_turn and cooled))
    )

    if should_classify:
        with _ai_task_inflight_lock_get():
            if session_id not in _ai_task_inflight:
                _ai_task_inflight.add(session_id)
                _start_ai_task_worker()
                _ai_task_queue.put({
                    "session_id": session_id,
                    "jsonl_path": jsonl_path,
                    "size": size,
                    "last_turn_epoch": last_turn_epoch,
                    "todos": todos,
                })
                computing = True
            else:
                computing = True
    else:
        with _ai_task_inflight_lock_get():
            computing = session_id in _ai_task_inflight

    # Status string for the renderer.
    if computing and not entry.get("tasks"):
        status = "computing"
    elif in_backoff:
        status = "errored"
    elif entry.get("tasks"):
        # Stale = data exists but session has grown a lot since we last
        # classified. UI uses this to dim slightly.
        age = now - last_ts
        status = "stale" if age > 300 and size_grew else "ok"
    elif entry:
        status = "empty"
    else:
        status = "computing" if computing else "empty"

    return {
        **entry,
        "status": status,
    }


def resolve_title(
    session: dict, meta: dict, desktop_index: dict[str, dict] | None = None
) -> str:
    """Pick the best title for display. Priority (most authoritative first):
      1. Title set in Claude for Desktop's app data — this is the title
         the user sees in the GUI, so it always wins.
      2. customTitle in the JSONL transcript (CLI-only sessions).
      3. aiTitle in the JSONL transcript.
      4. Indexed summary from sessions-index.json (often stale).
      5. First user prompt from the transcript.
      6. '(untitled)' as last resort."""
    sid = session.get("sessionId")
    if desktop_index and isinstance(sid, str):
        entry = desktop_index.get(sid)
        if entry and isinstance(entry.get("title"), str) and entry["title"].strip():
            return entry["title"].strip()
    for candidate in (
        meta.get("customTitle"),
        meta.get("aiTitle"),
        session.get("summary"),
        meta.get("firstPrompt"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return "(untitled)"


STUCK_AFTER_SECS = 5 * 60        # working with no progress this long → "Maybe stuck"
STALE_FINISHED_SECS = 30 * 60    # FINISHED sessions older than this → DORMANT
DORMANT_AFTER_SECS = 4 * 60 * 60  # any bucket this old → DORMANT (abandoned)

CLAUDE_SESSIONS_DIR = Path(os.path.expanduser("~/.claude/sessions"))

# Claude for Desktop stores session metadata — INCLUDING user-set titles —
# in a parallel tree under ~/Library/Application Support/Claude. Titles
# set via the GUI are NOT written to the JSONL transcript, so we have to
# read them from this app-data folder.
DESKTOP_SESSIONS_DIR = Path(
    os.path.expanduser("~/Library/Application Support/Claude/claude-code-sessions")
)


def desktop_titles() -> dict[str, dict]:
    """Return {cliSessionId: {'title': str, 'source': str}} parsed from
    Claude for Desktop's local session-metadata files. These contain the
    canonical user-visible title — exactly what's shown in the GUI — so
    they take priority over anything in the JSONL or sessions-index."""
    out: dict[str, dict] = {}
    if not DESKTOP_SESSIONS_DIR.exists():
        return out
    try:
        files = list(DESKTOP_SESSIONS_DIR.rglob("local_*.json"))
    except OSError:
        return out
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(d, dict):
            continue
        cli_id = d.get("cliSessionId")
        title = d.get("title")
        if isinstance(cli_id, str) and isinstance(title, str) and title.strip():
            out[cli_id] = {
                "title": title.strip(),
                "source": d.get("titleSource") or "",
            }
    return out


def live_session_ids() -> set[str]:
    """Return the set of sessionIds that have a Claude process running
    right now. Reads ~/.claude/sessions/<pid>.json — each is written by
    a live Claude CLI process and contains its pid + sessionId. A pid
    that no longer responds to signal 0 is treated as dead (stale file)."""
    live: set[str] = set()
    if not CLAUDE_SESSIONS_DIR.exists():
        return live
    try:
        files = list(CLAUDE_SESSIONS_DIR.glob("*.json"))
    except OSError:
        return live
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        pid = data.get("pid")
        sid = data.get("sessionId")
        if not isinstance(pid, int) or not isinstance(sid, str):
            continue
        try:
            os.kill(pid, 0)   # 0 = "is this pid alive?" without affecting it
        except (ProcessLookupError, PermissionError, OSError):
            continue
        live.add(sid)
    return live


def is_dormant(
    session_id: str,
    ago_seconds: float,
    live: set[str],
    bucket: str,
) -> bool:
    """A session is dormant — push to the bottom of the menu — if:
      (a) the Claude process isn't running anymore (window closed), OR
      (b) it's in the 'finished' bucket and you haven't touched it for
          STALE_FINISHED_SECS (probably moved past it), OR
      (c) it's been silent for DORMANT_AFTER_SECS regardless of bucket
          (abandoned). NEEDS YOU and WORKING are kept visible up to that
          ceiling because they're inherently demanding attention."""
    if session_id and session_id not in live:
        return True
    if ago_seconds > DORMANT_AFTER_SECS:
        return True
    if bucket == "ready" and ago_seconds > STALE_FINISHED_SECS:
        return True
    return False


# Canonical bucket identifiers. Re-used by `classify` and by every view
# (menubar, floating, terminal). Single source of truth so menubar and
# floating can't drift apart.
BUCKET_NEEDS = "needs"
BUCKET_WORKING = "working"
BUCKET_READY = "ready"
BUCKET_DORMANT = "dormant"
BUCKET_ORDER = (BUCKET_NEEDS, BUCKET_WORKING, BUCKET_READY, BUCKET_DORMANT)

# Header glyph + label + color for each bucket. Kanban column headers
# read from here; the menubar/floating use parallel constants in their
# own files — keeping these here so the terminal isn't a snowflake.
BUCKET_DISPLAY: dict[str, tuple[str, str, str]] = {
    BUCKET_NEEDS:   ("🔔", "NEEDS YOU", RED),
    BUCKET_WORKING: ("⚙️", "WORKING",  YELLOW),
    BUCKET_READY:   ("📥", "FINISHED", GREEN),
    BUCKET_DORMANT: ("💤", "DORMANT",  DIM),
}


def classify(state: str, phase_label: str) -> str:
    """Map (state, phase_label) → one of {needs, working, ready}.
    Dormant is NOT decided here — `is_dormant` overrides this once
    activity-age + process-liveness are known."""
    if state == "Maybe stuck":
        return BUCKET_NEEDS
    if phase_label in ("Asking you", "Proposing a plan"):
        return BUCKET_NEEDS
    if state == "Working…":
        return BUCKET_WORKING
    if state == "Waiting on you":
        return BUCKET_READY
    return BUCKET_READY


def state_for(meta: dict, ago_seconds: float) -> tuple[str, str]:
    """Distinguish four real states:
      - User just spoke (Working): Claude is processing.
      - Assistant called an interactive tool (Waiting): blocked on user input
        (AskUserQuestion / ExitPlanMode).
      - Assistant left other tool calls dangling (Working): mid-flight.
      - Assistant ended with text (Waiting): user's turn.
    The interactive-tool case has to be separated because a tool_use entry
    normally signals 'mid-flight' (Working) — but for AskUserQuestion /
    ExitPlanMode the assistant is genuinely paused on the user."""
    role = meta.get("lastRole")
    has_text = meta.get("lastAssistantHasText")
    if role == "user":
        if ago_seconds > STUCK_AFTER_SECS:
            return ("Maybe stuck", RED)
        return ("Working…", YELLOW)
    if role == "assistant":
        # Pending interactive tool overrides everything else for state —
        # the user is blocking Claude even though the entry looks tool-y.
        if _pending_interactive_tool(meta):
            return ("Waiting on you", GREEN)
        if has_text:
            return ("Waiting on you", GREEN)
        if ago_seconds > STUCK_AFTER_SECS:
            return ("Maybe stuck", RED)
        return ("Working…", YELLOW)
    return ("Idle", DIM)


# ---------- phase inference ----------
# Recent assistant turns scanned to infer what the agent is doing now.
PHASE_SCAN_TAIL = 30

# Tool-name buckets used for phase classification.
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
EXPLORE_TOOLS = {"Read", "Grep", "Glob", "Bash", "WebFetch", "WebSearch"}
# Tools that *pause* Claude until the user responds. When the last
# assistant entry contains one of these (with no following user reply
# yet), the session is genuinely blocked on user input — surface it as
# NEEDS YOU rather than the generic "Working / mid-tool".
INTERACTIVE_TOOLS = {"AskUserQuestion", "ExitPlanMode"}


def _pending_interactive_tool(meta: dict) -> bool:
    """True if the most recent assistant entry called an interactive tool
    (AskUserQuestion / ExitPlanMode) and the user hasn't replied yet."""
    if meta.get("lastRole") != "assistant":
        return False
    last_tools = meta.get("lastAssistantTools") or []
    return any(t in INTERACTIVE_TOOLS for t in last_tools)


def _is_design_tool(name: str) -> bool:
    n = name.lower()
    return n.startswith("mcp__") and ("figma" in n or "preview" in n or "design" in n)


_QUESTION_PHRASES = (
    "should i", "want me to", "would you like", "do you want",
    "let me know", "any preference", "which would you prefer",
    "shall i", "ok to proceed", "is that what you", "does that work",
)


def _looks_like_plan(text: str) -> bool:
    """Heuristic: long structured response with numbered steps or a plan
    heading. Used to detect 'proposing a plan' phase."""
    if len(text) < 200:
        return False
    lower = text.lower()
    if any(h in lower for h in ("## plan", "### plan", "## summary", "## proposal")):
        return True
    # Numbered lists with at least three items.
    numbered = sum(1 for n in "1234567" if f"\n{n}. " in text)
    return numbered >= 3


def infer_phase(meta: dict) -> tuple[str, str]:
    """Return (emoji, short_label) describing what the agent is doing right
    now, based purely on the transcript meta. No API calls.

    Branching rule: the "user's turn" is ONLY when the most recent entry
    is the assistant AND that entry contains text. If the user has spoken
    after a prior assistant question, the question is moot — it's
    Claude's turn again, so we classify by current tool activity instead
    of the stale text. Previously this code paired e.g. 'Asking you' with
    'Wait — Claude is on it' because it ignored late user replies."""
    last_role = meta.get("lastRole")
    last_text: str = meta.get("lastAssistantText") or ""
    has_text: bool = bool(meta.get("lastAssistantHasText"))
    recent_tools: list[str] = list(meta.get("recentTools") or [])
    tool_set = set(recent_tools)
    text_lower = last_text.lower()

    # Pending AskUserQuestion / ExitPlanMode beats all other classifiers —
    # the assistant is genuinely waiting on the user, not mid-flight.
    if _pending_interactive_tool(meta):
        last_tools = meta.get("lastAssistantTools") or []
        if "ExitPlanMode" in last_tools:
            return ("📋", "Proposing a plan")
        return ("❓", "Asking you")

    user_turn = (last_role == "assistant" and has_text)

    if not user_turn:
        # ---- Claude is working: classify by activity ----
        if last_role == "user" and not recent_tools:
            return ("🌀", "Spinning up")
        if any(_is_design_tool(t) for t in recent_tools):
            return ("🎨", "Designing")
        if tool_set & EDIT_TOOLS:
            return ("🛠", "Coding")
        if tool_set & EXPLORE_TOOLS:
            return ("🔍", "Exploring")
        if recent_tools:
            return ("🛠", f"Using {recent_tools[0] or 'tool'}")
        return ("", "")

    # ---- User's turn: classify by the assistant's final text ----
    stripped = last_text.rstrip().rstrip("\"'*])")
    if stripped.endswith("?") or any(
        p in text_lower[-300:] for p in _QUESTION_PHRASES
    ):
        return ("❓", "Asking you")
    if _looks_like_plan(last_text):
        return ("📋", "Proposing a plan")
    if len(last_text) > 500:
        return ("💬", "Explaining")
    return ("✅", "Reported back")


def next_action(phase_label: str, role: str | None, ago_seconds: float) -> str:
    """A short, imperative line telling the user what to actually DO."""
    if role == "user":
        if ago_seconds > STUCK_AFTER_SECS:
            return "May be stuck — consider /resume or send a nudge"
        return "Wait — Claude is on it"
    if phase_label == "Asking you":
        return "Answer the question"
    if phase_label == "Proposing a plan":
        return "Read the plan and approve or adjust"
    if phase_label == "Designing":
        return "Review the design output"
    if phase_label == "Coding":
        return "Review the changes, then reply"
    if phase_label == "Explaining":
        return "Read the response, then reply"
    if phase_label == "Reported back":
        return "Skim the result, then reply"
    if phase_label.startswith("Using"):
        return "Watch — Claude is mid-tool"
    if phase_label == "Exploring":
        return "Watch — Claude is gathering context"
    if role == "assistant":
        return "Reply to continue"
    return ""


def format_ago(seconds_ago: float) -> str:
    s = int(seconds_ago)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def truncate(s: str, width: int) -> str:
    s = s.replace("\n", " ").replace("\r", " ").strip()
    if len(s) <= width:
        return s
    return s[: max(1, width - 1)] + "…"


def _prepare_row(s: dict, now_ts: float, desktop_idx: dict, live: set[str]) -> dict:
    """Resolve everything we need to render one session, in one pass.
    Both list and kanban renderers call this so the two views can never
    disagree about a session's bucket, state, or gist."""
    full_path = s.get("fullPath", "")
    meta = transcript_meta(full_path)
    last_epoch = meta.get("lastTurnEpoch")
    if last_epoch is None:
        last_epoch = s.get("fileMtime", 0) / 1000
    ago_secs = now_ts - last_epoch

    phase_emoji, phase_label = infer_phase(meta)
    state, color = state_for(meta, ago_secs)
    hint = next_action(phase_label, meta.get("lastRole"), ago_secs)
    if state == "Working…" and meta.get("lastRole") == "assistant":
        hint = "Wait — Claude is mid-tool"

    bucket = classify(state, phase_label)
    sid = s.get("sessionId") or ""
    if is_dormant(sid, ago_secs, live, bucket):
        bucket = BUCKET_DORMANT

    subs = subagents_for_session(full_path, now_ts)
    sub_sum = subagent_summary(subs)
    todos = todos_for_session(full_path)
    todo_sum = todo_summary(todos)

    # If any sub-agent is actively running, the session as a whole is
    # in-progress regardless of what the parent transcript's tail says.
    # A parent often goes quiet waiting on its children; from the user's
    # POV that's still WORKING. Override after the dormant check so this
    # also rescues sessions whose parent file mtime is stale.
    if sub_sum.get("running"):
        bucket = BUCKET_WORKING
        state = "Working…"
        color = YELLOW
        hint = ""  # the agent listing carries the "what's happening" signal

    return {
        "session": s,
        "meta": meta,
        "ago_secs": ago_secs,
        "ago": format_ago(ago_secs),
        "phase_emoji": phase_emoji,
        "phase_label": phase_label,
        "state": state,
        "color": color,
        "hint": hint,
        "bucket": bucket,
        "title": resolve_title(s, meta, desktop_idx),
        "project": meta.get("cwd") or s.get("_originalPath") or "",
        "action": meta.get("lastAction") or "",
        "sid_short": sid[:8],
        "gist": session_gist(s, meta, bucket=bucket) or "",
        "subagents": subs,
        "subagent_summary": sub_sum,
        "todos": todos,
        "todo_summary": todo_sum,
        "ai_tasks": ai_tasks_for_session(
            sid, full_path, meta, todos, bucket=bucket,
        ),
    }


def _subagent_chip_lines(row: dict) -> list[str]:
    """One plain-text line per actively-running sub-agent, summarizing
    what it's doing (agent_type + meta.json description).

    Returns [] when no sub-agents are running — done/interrupted children
    are intentionally invisible so the chip is purely a "right now"
    signal. Capped at SUBAGENT_MAX_DISPLAY visible rows with a "+N more"
    suffix line when exceeded.

    Format per line: "◐ <agent_type> · <description>" — the leading
    half-circle glyph mirrors the running state icon used in popover
    Detail density, so the terminal/menubar/popover share a vocabulary."""
    subs = row.get("subagents") or []
    running = [s for s in subs if s.get("state") == SUBAGENT_RUNNING]
    if not running:
        return []
    lines: list[str] = []
    shown = running[:SUBAGENT_MAX_DISPLAY]
    for sub in shown:
        atype = (sub.get("agent_type") or "").strip()
        desc = (sub.get("name") or "agent").strip()
        if atype:
            lines.append(f"◐ {atype} · {desc}")
        else:
            lines.append(f"◐ {desc}")
    extra = len(running) - len(shown)
    if extra > 0:
        lines.append(f"+ {extra} more working")
    return lines


# ---------- list view (original layout) ----------
def render_list(rows: list[dict], width: int) -> str:
    parts: list[str] = []
    if not rows:
        parts.append(
            f"{DIM}No sessions modified in the last {RECENT_HOURS:.0f} hours.{RESET}\n"
        )
        return "".join(parts)
    for idx, r in enumerate(rows):
        # First QUICK_RESUME_MAX rows get a dim "[N] " prefix on the title
        # line so the user knows which digit hotkey resumes which session.
        slot_num = idx + 1 if idx < QUICK_RESUME_MAX else None
        slot_prefix = f"{DIM}[{slot_num}]{RESET} " if slot_num else ""
        # Reserve room for "[N] ▸ <emoji> <title>  [id]"
        slot_len = 4 if slot_num else 0
        prefix_len = 4 + slot_len + (2 if r["phase_emoji"] else 0) + 12
        title = truncate(r["title"], max(20, width - prefix_len))
        project = truncate(r["project"], width - 8)
        action = truncate(r["action"], width - 8)
        gist = truncate(r["gist"], width - 8)
        emoji_part = f"{r['phase_emoji']} " if r["phase_emoji"] else ""
        parts.append(
            f"{slot_prefix}{BOLD}▸ {emoji_part}{title}{RESET}  {DIM}[{r['sid_short']}]{RESET}\n"
        )
        if project:
            parts.append(
                f"  {CYAN}📁{RESET} {DIM}{project} · {r['ago']}{RESET}\n"
            )
        tail = f"  {DIM}— {r['hint']}{RESET}" if r["hint"] else ""
        phase_tag = f" {DIM}· {r['phase_label']}{RESET}" if r["phase_label"] else ""
        parts.append(
            f"  {r['color']}⏵ {r['state']}{RESET}{phase_tag}{tail}\n"
        )
        if gist:
            parts.append(f"  📌 {BOLD}{gist}{RESET}\n")
        if action:
            parts.append(f"  {MAGENTA}↳{RESET} {DIM}{action}{RESET}\n")
        # One line per actively-running sub-agent, in cyan. Sessions with
        # no running children render no chip at all — done/interrupted
        # are intentionally hidden.
        for chip_line in _subagent_chip_lines(r):
            parts.append(f"  {CYAN}{chip_line}{RESET}\n")
        parts.append("\n")
    return "".join(parts)


# ---------- kanban view ----------
KANBAN_MIN_WIDTH = 60        # below this, fall back to list view with a banner
KANBAN_MIN_COL_WIDTH = 28    # narrower than this per column → too cramped

def _kanban_card_lines(
    r: dict, col_inner_w: int, slot_num: int | None = None
) -> list[str]:
    """Build the styled lines for one card. 4–5 lines; trailing blank
    line is added by the caller, not here, so column-zipping knows where
    cards begin and end.

    `slot_num`, if set (1-9), is prefixed to the title line as a dim
    "[N] " marker matching the digit hotkey that resumes this session."""
    lines: list[str] = []

    # Line 1 — title (with optional slot-number + phase emoji prefix).
    emoji_part = f"{r['phase_emoji']} " if r["phase_emoji"] else ""
    slot_prefix_plain = f"[{slot_num}] " if slot_num else ""
    t1_plain = f"{slot_prefix_plain}▸ {emoji_part}{r['title']}"
    t1 = _clip_w(t1_plain, col_inner_w)
    if slot_num:
        # Style the "[N] " part dim, the rest bold, all inside the clipped
        # string. Since _clip_w can truncate, only apply the dim wrapper
        # when the slot prefix actually survived clipping.
        prefix_str = f"[{slot_num}] "
        if t1.startswith(prefix_str):
            rest = t1[len(prefix_str):]
            lines.append(f"{DIM}{prefix_str}{RESET}{BOLD}{rest}{RESET}")
        else:
            lines.append(f"{BOLD}{t1}{RESET}")
    else:
        lines.append(f"{BOLD}{t1}{RESET}")

    # Line 2 — gist (optional).
    if r["gist"]:
        g_plain = f"📌 {r['gist']}"
        lines.append(_clip_w(g_plain, col_inner_w))

    # Line 3 — state · phase.
    if r["phase_label"]:
        s3_plain = f"⏵ {r['state']} · {r['phase_label']}"
    else:
        s3_plain = f"⏵ {r['state']}"
    s3 = _clip_w(s3_plain, col_inner_w)
    lines.append(f"{r['color']}{s3}{RESET}")

    # Line 4 — folder · age (dim).
    folder_short = r["project"].replace(str(HOME), "~") if r["project"] else ""
    if folder_short:
        f4_plain = f"📁 {folder_short} · {r['ago']}"
    else:
        f4_plain = f"📁 · {r['ago']}"
    f4 = _clip_w(f4_plain, col_inner_w)
    lines.append(f"{DIM}{f4}{RESET}")

    # Lines 5+ (optional) — one cyan row per actively-running sub-agent
    # ("◐ <type> · <description>"), plus a "+N more working" suffix if
    # capped. Skipped entirely when zero sub-agents are running.
    for chip_line in _subagent_chip_lines(r):
        lines.append(f"{CYAN}{_clip_w(chip_line, col_inner_w)}{RESET}")

    return lines


def render_kanban(rows: list[dict], width: int, show_dormant: bool) -> str:
    cols = [BUCKET_NEEDS, BUCKET_WORKING, BUCKET_READY]
    if show_dormant:
        cols.append(BUCKET_DORMANT)
    n_cols = len(cols)

    # Outer gutters: 1 char left, 1 char right, 2 chars between columns.
    gutter_total = 2 + (n_cols - 1) * 2
    col_w = max(1, (width - gutter_total) // n_cols)
    if col_w < KANBAN_MIN_COL_WIDTH:
        # Too cramped — let the caller fall back.
        return ""
    col_inner_w = col_w  # we already accounted for gutters, content fills col_w

    # Group rows by bucket.
    buckets: dict[str, list[dict]] = {k: [] for k in cols}
    for r in rows:
        b = r["bucket"]
        if b in buckets:
            buckets[b].append(r)
        # Sessions in BUCKET_DORMANT when show_dormant is off are dropped
        # (consistent with the floating's "Show older" toggle off).

    # Assign quick-resume slot numbers in column-major order: walk each
    # visible column in display order, and within each column walk cards
    # top-to-bottom. Cap at QUICK_RESUME_MAX. The mapping (id(r) → slot)
    # is also exported via _NUMBERED_SESSIONS in render(), so the
    # keypress handler resolves digit hits to the same session the user
    # sees labeled "[N]".
    slot_by_id: dict[int, int] = {}
    next_slot = 1
    for key in cols:
        for r in buckets[key]:
            if next_slot > QUICK_RESUME_MAX:
                break
            slot_by_id[id(r)] = next_slot
            next_slot += 1
        if next_slot > QUICK_RESUME_MAX:
            break

    # Build a list of styled lines per column.
    per_col_lines: list[list[str]] = []
    for key in cols:
        emoji, label, color = BUCKET_DISPLAY[key]
        col_lines: list[str] = []
        bucket_rows = buckets[key]
        # Header row: "🔔 NEEDS YOU (2)".
        header_plain = f"{emoji} {label} ({len(bucket_rows)})"
        header_styled = f"{BOLD}{color}{_clip_w(header_plain, col_inner_w)}{RESET}"
        col_lines.append(header_styled)
        # Separator rule under the header.
        rule = "─" * col_inner_w
        col_lines.append(f"{DIM}{rule}{RESET}")
        col_lines.append("")  # blank between rule and first card
        for i, r in enumerate(bucket_rows):
            slot = slot_by_id.get(id(r))
            col_lines.extend(_kanban_card_lines(r, col_inner_w, slot_num=slot))
            # Blank separator between cards, but not after the last one.
            if i < len(bucket_rows) - 1:
                col_lines.append("")
        per_col_lines.append(col_lines)

    # Pad each column to the same vertical height by appending empty lines.
    max_h = max(len(c) for c in per_col_lines) if per_col_lines else 0
    for c in per_col_lines:
        while len(c) < max_h:
            c.append("")

    # Zip rows together, padding each cell to col_w and joining with a
    # 2-space gutter. 1-space outer indent.
    out: list[str] = []
    for row_idx in range(max_h):
        cells = []
        for c in per_col_lines:
            cells.append(_pad_w(c[row_idx], col_w))
        out.append(" " + "  ".join(cells).rstrip() + "\n")

    # Empty-state banner if literally everything is empty.
    if not any(buckets[k] for k in cols):
        out.append(
            f"\n{DIM}No sessions modified in the last "
            f"{RECENT_HOURS:.0f} hours.{RESET}\n"
        )

    return "".join(out)


def _compute_numbered_sessions(
    rows: list[dict], view: str, show_dormant: bool
) -> list[dict]:
    """Build the ordered list of sessions whose slot numbers (1..9) match
    the "[N]" labels the user sees. Index 0 in the returned list is the
    "[1]" session, etc. Both renderers and the keypress handler consult
    this so numbering and lookup can never disagree.

    Order rules (mirroring the renderers):
      - list view: same order as `rows` (already sorted by recency).
      - kanban view: column-major. Columns are NEEDS, WORKING, FINISHED
        (and DORMANT if show_dormant). Within each column, recency order
        (which is `rows` order, since `_prepare_row` doesn't reshuffle).
    """
    if view != "kanban":
        return [r["session"] for r in rows[:QUICK_RESUME_MAX]]
    cols = [BUCKET_NEEDS, BUCKET_WORKING, BUCKET_READY]
    if show_dormant:
        cols.append(BUCKET_DORMANT)
    buckets: dict[str, list[dict]] = {k: [] for k in cols}
    for r in rows:
        b = r["bucket"]
        if b in buckets:
            buckets[b].append(r)
    out: list[dict] = []
    for key in cols:
        for r in buckets[key]:
            if len(out) >= QUICK_RESUME_MAX:
                return out
            out.append(r["session"])
    return out


def render_tasks(rows: list[dict], width: int) -> str:
    """Per-session vertical list of TodoWrite todos with running sub-agents
    nested under the in_progress todo. Sessions with no todos are skipped.

    Layout — matches the popover Tasks tab vocabulary:

      ▾ <title>                  3/5 ◐   2m ago
        ✓ Completed todo content
        ◐ In-progress todo               [2 agents]
            ├ ◐ general-purpose · brief
            └ ◐ code-reviewer · brief
        ○ Pending todo content
    """
    sessions_with_todos = [r for r in rows if r.get("todos")]
    if not sessions_with_todos:
        return (
            f"\n{DIM}No tasks yet. Claude writes todos when it plans "
            f"multi-step work — open a session and watch them appear here.{RESET}\n"
        )

    icons = {
        TODO_PENDING: "○",
        TODO_IN_PROGRESS: "◐",
        TODO_COMPLETED: "✓",
    }
    colors = {
        TODO_PENDING: DIM,
        TODO_IN_PROGRESS: CYAN,
        TODO_COMPLETED: DIM,
    }

    parts: list[str] = []
    for r in sessions_with_todos:
        todos = r.get("todos") or []
        summ = r.get("todo_summary") or {}
        running_subs = [
            s for s in (r.get("subagents") or [])
            if s.get("state") == SUBAGENT_RUNNING
        ]
        title = truncate(r.get("title") or "(untitled)", max(20, width - 40))
        done_n = summ.get("completed", 0)
        total_n = summ.get("total", 0) or 1
        in_progress_n = summ.get("in_progress", 0)
        progress_glyph = "◐" if in_progress_n else "○"
        progress_color = CYAN if in_progress_n else DIM
        # Header — title + progress + age.
        parts.append(
            f"{BOLD}▸ {title}{RESET}   "
            f"{progress_color}{done_n}/{total_n} {progress_glyph}{RESET}"
            f"   {DIM}{r.get('ago', '')}{RESET}\n"
        )
        for todo in todos:
            status = todo.get("status") or TODO_PENDING
            icon = icons.get(status, "·")
            color = colors.get(status, RESET)
            # Pick the right verbalization for the status.
            text = (
                todo.get("activeForm") if status == TODO_IN_PROGRESS
                else todo.get("content")
            ) or todo.get("content") or ""
            text = truncate(text, max(20, width - 12))
            # Strikethrough on completed (ANSI 9) so finished todos visibly
            # fall back — terminal-equivalent of the popover's strikethrough.
            if status == TODO_COMPLETED:
                line_body = f"\033[9m{text}\033[29m"
            else:
                line_body = text
            badge = ""
            if status == TODO_IN_PROGRESS and running_subs:
                badge = (
                    f"   {CYAN}[{len(running_subs)} agent"
                    f"{'s' if len(running_subs) != 1 else ''}]{RESET}"
                )
            parts.append(f"  {color}{icon}{RESET}  {line_body}{badge}\n")

            # Nest running sub-agents under the in_progress todo only.
            if status == TODO_IN_PROGRESS and running_subs:
                shown_subs = running_subs[:SUBAGENT_MAX_DISPLAY]
                for i, sub in enumerate(shown_subs):
                    is_last = (
                        i == len(shown_subs) - 1
                        and len(running_subs) <= SUBAGENT_MAX_DISPLAY
                    )
                    connector = "└" if is_last else "├"
                    atype = (sub.get("agent_type") or "").strip()
                    desc = (sub.get("name") or "agent").strip()
                    line = (
                        f"{connector} ◐ {atype} · {desc}"
                        if atype else f"{connector} ◐ {desc}"
                    )
                    parts.append(
                        f"      {CYAN}{truncate(line, max(20, width - 8))}{RESET}\n"
                    )
                overflow = len(running_subs) - len(shown_subs)
                if overflow > 0:
                    parts.append(
                        f"      {DIM}└ + {overflow} more working{RESET}\n"
                    )
        parts.append("\n")
    return "".join(parts)


def render_ai_tasks(rows: list[dict], width: int) -> str:
    """Terminal companion for the AI tab — same data, plain-ANSI layout.

    Empty states mirror the popover (no key / classifying / nothing yet).
    Sessions with cached AI tasks render with the same status icons
    (○ ◐ ✓) plus optional ·NN% confidence suffix on uncertain ones."""
    ai_enabled = bool(os.environ.get("ANTHROPIC_API_KEY")) and (
        os.environ.get("CLAUDE_SESSIONS_AI_TASKS", "").strip()
        in ("1", "true", "yes", "on")
    )

    parts: list[str] = []
    # Header chip line.
    any_classified = any(
        (r.get("ai_tasks") or {}).get("classified_ts") for r in rows
    )
    any_computing = any(
        (r.get("ai_tasks") or {}).get("status") == "computing" for r in rows
    )
    chip_status = "(no sessions classified yet)" if not any_classified else "synced"
    suffix = "  ·  classifying…" if any_computing else ""
    parts.append(
        f"{MAGENTA}✨ AI-detected{RESET}   {DIM}{chip_status}{suffix}{RESET}\n\n"
    )

    # Empty state: no key / not enabled.
    if not ai_enabled:
        parts.append(
            f"{BOLD}Enable AI Tasks{RESET}\n"
            f"Add an Anthropic API key + toggle to {DIM}~/.claude-sessions-status.env{RESET}:\n\n"
            f"  ANTHROPIC_API_KEY=sk-…\n"
            f"  CLAUDE_SESSIONS_AI_TASKS=1\n\n"
            f"{DIM}First classification appears within ~1 min.{RESET}\n"
        )
        return "".join(parts)

    # Sessions with usable AI task data.
    sessions_with_ai: list[dict] = []
    for r in rows:
        ai = r.get("ai_tasks") or {}
        status = ai.get("status") or "empty"
        if status in ("ok", "stale", "computing", "errored") and (
            ai.get("tasks") or status == "computing"
        ):
            sessions_with_ai.append(r)

    if not sessions_with_ai:
        if any_computing:
            parts.append(
                f"{DIM}Classifying your sessions… first results in a few seconds.{RESET}\n"
            )
        else:
            parts.append(
                f"{DIM}No AI tasks yet. Sessions will appear here after their "
                f"next user/assistant turn.{RESET}\n"
            )
        return "".join(parts)

    icons = {"pending": "○", "in_progress": "◐", "completed": "✓"}
    colors = {"pending": DIM, "in_progress": MAGENTA, "completed": DIM}

    for r in sessions_with_ai:
        ai = r.get("ai_tasks") or {}
        tasks = ai.get("tasks") or []
        intent = (ai.get("session_intent") or "").strip()
        classified_ts = ai.get("classified_ts") or 0
        title = truncate(r.get("title") or "(untitled)", max(20, width - 40))

        synced = ""
        if classified_ts:
            ago_secs = max(0.0, time.time() - classified_ts)
            synced = f"   {DIM}synced {format_ago(ago_secs)}{RESET}"
        parts.append(
            f"{BOLD}▸ {title}{RESET}{synced}   {DIM}{r.get('ago', '')}{RESET}\n"
        )

        if ai.get("status") == "errored":
            fail_n = ai.get("failure_count", 0)
            parts.append(
                f"  {DIM}⚠ last classification failed "
                f"({fail_n} time{'s' if fail_n != 1 else ''}) — retrying after backoff{RESET}\n"
            )

        if intent:
            parts.append(
                f"  {DIM}{truncate(intent, max(20, width - 6))}{RESET}\n"
            )

        for task in tasks:
            status = task.get("status") or "pending"
            icon = icons.get(status, "·")
            icon_color = colors.get(status, RESET)
            title_text = (task.get("title") or "").strip()
            summary = (task.get("summary") or "").strip()
            confidence = task.get("confidence") or 1.0
            evidence = (task.get("evidence") or "").strip()

            # Strikethrough completed (ANSI 9).
            if status == "completed":
                title_styled = f"\033[9m{title_text}\033[29m"
            elif status == "in_progress":
                title_styled = f"{BOLD}{title_text}{RESET}"
            else:
                title_styled = title_text
            conf_suffix = ""
            if confidence < 0.9:
                pct = int(round(confidence * 100))
                conf_suffix = f"   {DIM}·{pct}%{RESET}"
            parts.append(
                f"  {icon_color}{icon}{RESET}  {title_styled}{conf_suffix}\n"
            )
            if summary and status != "completed":
                parts.append(
                    f"      {DIM}{truncate(summary, max(20, width - 8))}{RESET}\n"
                )
            if evidence and status == "in_progress":
                quoted = evidence if len(evidence) < 100 else evidence[:99] + "…"
                parts.append(
                    f"      {DIM}“{truncate(quoted, max(20, width - 10))}”{RESET}\n"
                )
        parts.append("\n")
    return "".join(parts)


def render(
    sessions: list[dict],
    view: str = "list",
    show_dormant: bool = False,
) -> None:
    global _NUMBERED_SESSIONS, _LAST_ACTION_MSG
    try:
        width = os.get_terminal_size().columns
    except OSError:
        width = 100
    now_ts = time.time()
    parts: list[str] = [CLEAR]
    header = f"{BOLD}Claude Code Sessions{RESET}"
    clock = f"{DIM}{datetime.now().strftime('%H:%M:%S')}{RESET}"
    view_tag = f"{DIM}· {view}" + (" · +dormant" if show_dormant else "") + f"{RESET}"
    parts.append(f"{header}   {clock}   {view_tag}\n")
    parts.append(f"{DIM}{'─' * width}{RESET}\n\n")

    # Build the Desktop-title index once per render (cheap — ~100ms,
    # 23 files) so every row's title resolution sees user-set titles.
    desktop_idx = desktop_titles()
    live = live_session_ids()
    rows = [_prepare_row(s, now_ts, desktop_idx, live) for s in sessions]

    # Resolve which view the body will actually render in (kanban may fall
    # back to list when the terminal is too narrow). The numbering must
    # match the displayed view, not the requested one.
    body: str
    if view == "ai":
        parts.append(render_ai_tasks(rows, width))
        effective_view = "ai"
    elif view == "tasks":
        parts.append(render_tasks(rows, width))
        effective_view = "tasks"
    elif view == "kanban" and width >= KANBAN_MIN_WIDTH:
        body = render_kanban(rows, width, show_dormant)
        if not body:
            parts.append(
                f"{DIM}(terminal too narrow for kanban — showing list){RESET}\n\n"
            )
            parts.append(render_list(rows, width))
            effective_view = "list"
        else:
            parts.append(body)
            effective_view = "kanban"
    elif view == "kanban":
        parts.append(
            f"{DIM}(terminal width {width} < {KANBAN_MIN_WIDTH} — showing list){RESET}\n\n"
        )
        parts.append(render_list(rows, width))
        effective_view = "list"
    else:
        parts.append(render_list(rows, width))
        effective_view = "list"

    # Publish the numbering so the keypress handler can resolve digits.
    _NUMBERED_SESSIONS = _compute_numbered_sessions(
        rows, effective_view, show_dormant
    )

    # One-shot status line (e.g. "→ resumed session 3 …" or "no session
    # at slot 3"), printed once and cleared so the next frame is clean.
    if _LAST_ACTION_MSG:
        parts.append(f"{DIM}{_LAST_ACTION_MSG}{RESET}\n")
        _LAST_ACTION_MSG = None

    footer = (
        f"{DIM}refreshing every {REFRESH_SECS}s · "
        f"window {RECENT_HOURS:.0f}h · "
        f"k=kanban  l=list  t=tasks  a=ai  d=±dormant  1-9=resume  q=quit{RESET}\n"
    )
    parts.append(footer)
    sys.stdout.write("".join(parts))
    sys.stdout.flush()


def _setup_cbreak_stdin():
    """Put stdin into cbreak mode for single-character reads. Returns
    the saved termios attrs to restore on exit, or None if stdin isn't
    a tty (e.g. piped output). Caller is responsible for restoring."""
    if not sys.stdin.isatty():
        return None
    try:
        import termios
        import tty
    except ImportError:
        return None
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
    except Exception:
        return None
    return saved


def _restore_stdin(saved) -> None:
    if saved is None:
        return
    try:
        import termios
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)
    except Exception:
        pass


def _poll_key() -> str | None:
    """Non-blocking single-char read from stdin. Returns None if nothing
    pending. Safe to call even when stdin isn't a tty (returns None)."""
    if not sys.stdin.isatty():
        return None
    try:
        import select
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if r:
            return sys.stdin.read(1)
    except Exception:
        return None
    return None


def _resume_session_in_terminal(session_id: str) -> bool:
    """Open a fresh Terminal.app window and run `claude --resume <sid>`.
    Returns True on success (osascript launched without an exit error),
    False otherwise. Never raises — the dashboard must keep running.

    Shell-escaping note: the sessionId is the JSONL filename stem, so it's
    a UUID-like token. We still defensively reject anything outside the
    safe character set before composing the AppleScript so a hostile
    transcript filename can't smuggle quotes / commands into `do script`.
    """
    if not session_id:
        return False
    # Defensive allow-list: UUIDs use [0-9a-fA-F-]. Anything else and we
    # bail rather than risk command injection via `do script`.
    safe_chars = set("0123456789abcdefABCDEF-_")
    if any(c not in safe_chars for c in session_id):
        return False
    cmd = f"claude --resume {session_id}"
    try:
        subprocess.run(
            [
                "osascript",
                "-e", 'tell application "Terminal" to activate',
                "-e", f'tell application "Terminal" to do script "{cmd}"',
            ],
            check=False,
        )
    except OSError:
        return False
    return True


def main() -> int:
    global _LAST_ACTION_MSG
    import argparse
    parser = argparse.ArgumentParser(
        prog="claude-sessions-status-dashboard",
        description="Live terminal view of recently-active Claude Code sessions.",
    )
    parser.add_argument("--kanban", action="store_true",
                        help="Start in kanban (3-column) view")
    parser.add_argument("--list", dest="list_view", action="store_true",
                        help="Start in list view (default if no flag and no saved pref)")
    parser.add_argument("--tasks", action="store_true",
                        help="Start in tasks view (TodoWrite-derived per-session todos)")
    parser.add_argument("--ai-tasks", dest="ai_tasks", action="store_true",
                        help="Start in AI tasks view (Haiku-derived per-session synthesis)")
    parser.add_argument("--show-dormant", action="store_true",
                        help="Show the DORMANT column / dormant sessions")
    parser.add_argument("--save", action="store_true",
                        help="Persist the chosen view to "
                             "~/.claude-sessions-status-dashboard-mode")
    args = parser.parse_args()

    # Resolve initial view: explicit flag > saved preference > default.
    if args.ai_tasks:
        view = "ai"
    elif args.tasks:
        view = "tasks"
    elif args.kanban:
        view = "kanban"
    elif args.list_view:
        view = "list"
    else:
        view = _read_dashboard_mode()
    if args.save:
        _write_dashboard_mode(view)
    show_dormant = bool(args.show_dormant)

    # Clean exit on ctrl-c without a traceback.
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    # Hide cursor + put stdin into cbreak for interactive keys.
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()
    saved_termios = _setup_cbreak_stdin()
    try:
        while True:
            sessions = recent_sessions(find_sessions())
            render(sessions, view=view, show_dormant=show_dormant)
            # Sleep in 100ms increments so keypresses feel responsive.
            elapsed = 0.0
            tick = 0.1
            next_refresh = float(REFRESH_SECS)
            while elapsed < next_refresh:
                time.sleep(tick)
                elapsed += tick
                key = _poll_key()
                if key is None:
                    continue
                ch = key.lower()
                if ch == "q":
                    return 0
                if ch == "k" and view != "kanban":
                    view = "kanban"
                    _write_dashboard_mode(view)
                    break  # re-render now
                if ch == "l" and view != "list":
                    view = "list"
                    _write_dashboard_mode(view)
                    break
                if ch == "t" and view != "tasks":
                    view = "tasks"
                    _write_dashboard_mode(view)
                    break
                if ch == "a" and view != "ai":
                    view = "ai"
                    _write_dashboard_mode(view)
                    break
                if ch == "d":
                    show_dormant = not show_dormant
                    break
                if ch == "r":  # manual refresh
                    break
                # Digit hotkeys 1-9: resume the Nth visible session in a
                # new Terminal window. We set _LAST_ACTION_MSG and break
                # so the next frame re-renders with the footer status.
                if ch.isdigit() and ch != "0":
                    slot = int(ch)
                    if slot - 1 < len(_NUMBERED_SESSIONS):
                        target = _NUMBERED_SESSIONS[slot - 1]
                        sid = target.get("sessionId") or ""
                        if sid and _resume_session_in_terminal(sid):
                            _LAST_ACTION_MSG = (
                                f"→ resumed session {slot} in new Terminal window"
                            )
                        else:
                            _LAST_ACTION_MSG = f"could not resume session {slot}"
                    else:
                        _LAST_ACTION_MSG = f"no session at slot {slot}"
                    break
                # Any other key is ignored — no help overlay yet.
    finally:
        _restore_stdin(saved_termios)
        sys.stdout.write("\033[?25h")  # show cursor
        sys.stdout.write(RESET)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
