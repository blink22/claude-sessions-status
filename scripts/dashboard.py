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
CLEAR = "\033[2J\033[H"


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


# Caches keyed on session file path. cwd/firstPrompt don't change once set,
# so we avoid re-scanning the full transcript on every refresh.
_meta_cache: dict[str, dict] = {}


def transcript_meta(transcript_path: str) -> dict:
    """Single-pass scan returning everything the dashboard needs.

    Key disambiguation: the LAST assistant entry can be (a) text only —
    Claude has finished its turn and is genuinely waiting on the user, or
    (b) tool_use only — Claude is mid-flight, waiting for its own tool
    result to come back. These look identical via `last_role` alone, so
    we expose `lastAssistantHasText` for downstream logic to tell them
    apart."""
    p = Path(transcript_path)
    cached = _meta_cache.get(transcript_path)
    blank = {
        "cwd": None, "firstPrompt": None,
        "lastRole": None, "lastAction": "",
        "lastAssistantHasText": False,
        "lastAssistantText": "", "recentTools": [],
        "lastAssistantTools": [],
    }
    if not p.exists():
        return cached or blank

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
    _meta_cache[transcript_path] = meta
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


def render(sessions: list[dict]) -> None:
    try:
        width = os.get_terminal_size().columns
    except OSError:
        width = 100
    now_ts = time.time()
    parts: list[str] = [CLEAR]
    header = f"{BOLD}Claude Code Sessions{RESET}"
    clock = f"{DIM}{datetime.now().strftime('%H:%M:%S')}{RESET}"
    parts.append(f"{header}   {clock}\n")
    parts.append(f"{DIM}{'─' * width}{RESET}\n\n")

    if not sessions:
        parts.append(
            f"{DIM}No sessions modified in the last {RECENT_HOURS:.0f} hours.{RESET}\n"
        )
    else:
        # Build the Desktop-title index once per render (cheap — ~100ms,
        # 23 files) so every row's title resolution sees user-set titles.
        desktop_idx = desktop_titles()
        for s in sessions:
            full_path = s.get("fullPath", "")
            meta = transcript_meta(full_path)
            # Use the most recent user/assistant timestamp as "last activity".
            # File mtime is unreliable — title metadata writes bump it.
            last_epoch = meta.get("lastTurnEpoch")
            if last_epoch is None:
                last_epoch = s.get("fileMtime", 0) / 1000
            ago_secs = now_ts - last_epoch
            ago = format_ago(ago_secs)

            phase_emoji, phase_label = infer_phase(meta)
            state, color = state_for(meta, ago_secs)
            hint = next_action(phase_label, meta.get("lastRole"), ago_secs)
            # If the assistant is still mid-tool, the user shouldn't act —
            # override any "Reply"/"Answer the question" hint.
            if state == "Working…" and meta.get("lastRole") == "assistant":
                hint = "Wait — Claude is mid-tool"

            title = resolve_title(s, meta, desktop_idx)
            # Reserve room for "▸ <emoji> <title>  [id]"
            prefix_len = 4 + (2 if phase_emoji else 0) + 12
            title = truncate(title, max(20, width - prefix_len))
            project = meta.get("cwd") or s.get("_originalPath") or ""
            project = truncate(project, width - 8)
            action = truncate(meta.get("lastAction") or "", width - 8)
            sid_short = (s.get("sessionId") or "")[:8]
            gist = session_gist(s, meta, bucket=None) or ""
            gist = truncate(gist, width - 8)

            emoji_part = f"{phase_emoji} " if phase_emoji else ""
            parts.append(
                f"{BOLD}▸ {emoji_part}{title}{RESET}  {DIM}[{sid_short}]{RESET}\n"
            )
            if project:
                parts.append(
                    f"  {CYAN}📁{RESET} {DIM}{project} · {ago}{RESET}\n"
                )
            # Status + next-action on one line so it's the obvious focal point.
            tail = f"  {DIM}— {hint}{RESET}" if hint else ""
            phase_tag = f" {DIM}· {phase_label}{RESET}" if phase_label else ""
            parts.append(
                f"  {color}⏵ {state}{RESET}{phase_tag}{tail}\n"
            )
            # Gist line — what Claude is concretely working on right now.
            if gist:
                parts.append(f"  📌 {BOLD}{gist}{RESET}\n")
            if action:
                parts.append(f"  {MAGENTA}↳{RESET} {DIM}{action}{RESET}\n")
            parts.append("\n")

    footer = (
        f"{DIM}refreshing every {REFRESH_SECS}s · "
        f"window {RECENT_HOURS:.0f}h · ctrl-c to exit{RESET}\n"
    )
    parts.append(footer)
    sys.stdout.write("".join(parts))
    sys.stdout.flush()


def main() -> int:
    # Clean exit on ctrl-c without a traceback.
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    # Hide the cursor while the dashboard runs; restore on exit.
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()
    try:
        while True:
            sessions = recent_sessions(find_sessions())
            render(sessions)
            time.sleep(REFRESH_SECS)
    finally:
        sys.stdout.write("\033[?25h")  # show cursor
        sys.stdout.write(RESET)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
