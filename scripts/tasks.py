"""Per-session user-curated tasks persistence + CRUD.

The task list is the user's own — they author tasks themselves and check
them off when done. Tasks persist across turns and across sessions in a
single JSON sidecar at ``~/.claude-sessions-status-tasks.json``, keyed
by sessionId.

The schema is forward-compatible with v1.1 AI-suggested tasks: every
task carries a ``source`` ("user" | "suggested") and ``approved`` flag.
In v1 we only ever emit ``source="user"`` and ``approved=True``; v1.1
adds the Haiku suggestion path that emits ``approved=False`` items the
user later ratifies via taskApprove / taskReject.

File shape:

    {
      "version": 1,
      "sessions": {
        "<session-uuid>": {
          "tasks": [
            {
              "id": "t_<base32>",
              "content": "Ship v0.5",
              "status": "open",        # open | done | dismissed
              "source": "user",        # user | suggested
              "approved": True,        # False only for un-ratified suggestions
              "createdAt": <epoch>,
              "completedAt": null,
              "suggestedBy": null,
              "suggestionContext": null
            }
          ],
          "lastSweepAt": null,         # epoch of last Haiku sweep (v1.1)
          "dismissedSuggestions": []   # content strings — don't re-suggest
        }
      }
    }

Concurrency: writes are serialized via an in-process ``threading.Lock``
since the only writer is the badge process (clicks → WKScript handler →
Python). The lock + ``_atomic_write_text``-style replace prevents a
mid-write read-modify-write race when two clicks land within the same
poll interval. Cross-process safety is not a goal — only one badge runs
at a time per PID file.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Optional

HOME = Path(os.path.expanduser("~"))
TASKS_FILE = HOME / ".claude-sessions-status-tasks.json"
SCHEMA_VERSION = 1

# Tasks older than this in "done" or "dismissed" state get pruned on
# load. Keeps the file from growing unboundedly.
_PRUNE_TTL_S = 30 * 86400.0  # 30 days

# Per-session safety cap. Keeps Haiku suggestions or runaway clicks from
# bloating the file.
_TASKS_PER_SESSION_MAX = 50

# Single in-process lock around read-modify-write. Cheap and sufficient
# given there's exactly one writer process.
_LOCK = threading.Lock()

# In-memory cache keyed on file mtime, mirroring the existing
# _load_seen() pattern in floating.py. Avoids re-reading + re-parsing
# the JSON on every 5s poll when nothing has changed.
_cache: Optional[tuple] = None  # (mtime, data)

# One-shot orphan-tmp sweep: runs on the first load_state() call per
# process so we don't sweep on every refresh tick.
_swept = False


# ---------------- IO helpers ----------------

def _atomic_write_json(path: Path, data: dict) -> bool:
    """Same shape as floating.py's ``_atomic_write_text`` — tempfile +
    os.replace. POSIX-atomic on the same filesystem.

    Returns True if the write hit disk, False on failure (disk full,
    permission denied, etc.). Callers MUST honor the return value:
    a False return means in-memory state is now ahead of disk, and
    the mtime-keyed cache must be invalidated so the next load
    re-reads the (older) on-disk truth.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(
            json.dumps(data, indent=2, sort_keys=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
        return True
    except OSError as e:
        sys.stderr.write(f"[tasks] write failed: {e!r}\n")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _sweep_orphan_tmps() -> None:
    """Clean up ``.tasks.json.tmp`` files that survived a SIGKILL mid-
    write. POSIX-atomic os.replace guarantees the real file is never
    half-written, but the .tmp source can be orphaned. We sweep on
    load_state() the first time a session boots, which is cheap and
    keeps the user's $HOME tidy."""
    try:
        for stale in TASKS_FILE.parent.glob(TASKS_FILE.name + ".tmp"):
            try:
                stale.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _empty_state() -> dict:
    return {"version": SCHEMA_VERSION, "sessions": {}}


def _empty_session_entry() -> dict:
    return {
        "tasks": [],
        "lastSweepAt": None,
        "dismissedSuggestions": [],
    }


def _new_task_id() -> str:
    """Short, URL-safe, sortable-enough id. ``secrets.token_urlsafe(9)``
    yields ~12 base64url chars; we prefix with ``t_`` so future log
    lines are greppable."""
    return "t_" + secrets.token_urlsafe(9)


def _prune(state: dict) -> dict:
    """Drop done/dismissed tasks older than _PRUNE_TTL_S from each
    session entry. Also enforces _TASKS_PER_SESSION_MAX (FIFO eviction
    of oldest done tasks, never evicting open ones). Returns the same
    dict instance (in-place mutation) for clarity at call sites."""
    cutoff = time.time() - _PRUNE_TTL_S
    sessions = state.get("sessions") or {}
    for sid, entry in list(sessions.items()):
        if not isinstance(entry, dict):
            sessions.pop(sid, None)
            continue
        tasks = entry.get("tasks") or []
        kept: list = []
        for t in tasks:
            if not isinstance(t, dict):
                continue
            status = t.get("status") or "open"
            if status in ("done", "dismissed"):
                completed = t.get("completedAt") or t.get("createdAt") or 0
                if isinstance(completed, (int, float)) and completed < cutoff:
                    continue
            kept.append(t)
        # Enforce per-session cap: if we still exceed the limit, drop
        # oldest done tasks first, then oldest dismissed. Open tasks
        # are never evicted automatically.
        if len(kept) > _TASKS_PER_SESSION_MAX:
            done = [t for t in kept if t.get("status") in ("done", "dismissed")]
            done.sort(key=lambda t: t.get("completedAt") or t.get("createdAt") or 0)
            to_drop = len(kept) - _TASKS_PER_SESSION_MAX
            drop_ids = {id(t) for t in done[:to_drop]}
            kept = [t for t in kept if id(t) not in drop_ids]
        entry["tasks"] = kept
        # Drop session entries that are now completely empty AND have
        # no dismissed-suggestion history. Keeps the file small.
        if not kept and not entry.get("dismissedSuggestions"):
            sessions.pop(sid, None)
    return state


def load_state() -> dict:
    """Load the tasks file, returning the in-memory state dict. Cached
    on mtime so the 5s refresh poll is cheap.

    On corrupt JSON (rare — can happen after a crash mid-write that
    somehow defeated the atomic replace), back the corrupt file up to
    ``.corrupt-<ms-ts>`` and start fresh rather than wedging the
    badge. Millisecond-resolution timestamp so two corruptions in the
    same wall-clock second don't clobber each other.

    First call per process also sweeps orphaned ``.tmp`` files from a
    previous mid-write SIGKILL. Cheap and self-healing.

    NOTE: ``_prune`` runs under ``_LOCK`` to keep the read path safe
    against a concurrent mutating call that's holding the lock — both
    would mutate ``data`` in place otherwise."""
    global _cache, _swept
    if not _swept:
        _sweep_orphan_tmps()
        _swept = True
    if not TASKS_FILE.exists():
        _cache = None
        return _empty_state()
    try:
        mtime = TASKS_FILE.stat().st_mtime
    except OSError:
        return _cache[1] if _cache else _empty_state()
    if _cache is not None and _cache[0] == mtime:
        return _cache[1]
    with _LOCK:
        # Re-check the cache under the lock — another thread may have
        # populated it while we were waiting.
        if _cache is not None and _cache[0] == mtime:
            return _cache[1]
        try:
            raw = TASKS_FILE.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict) or "sessions" not in data:
                raise ValueError("schema mismatch")
        except (OSError, json.JSONDecodeError, ValueError):
            # Back up the corrupt file. Millisecond resolution avoids
            # clobbering an earlier backup if corruption happens twice
            # in the same wall-clock second.
            try:
                backup = TASKS_FILE.with_suffix(
                    TASKS_FILE.suffix
                    + f".corrupt-{int(time.time() * 1000)}",
                )
                if TASKS_FILE.exists():
                    os.replace(TASKS_FILE, backup)
            except OSError:
                pass
            data = _empty_state()
        _prune(data)
        _cache = (mtime, data)
        return data


def _save_state(state: dict) -> bool:
    """Persist + refresh the mtime-keyed in-memory cache. Caller must
    already hold _LOCK.

    Returns True on successful disk write, False if the write failed
    (rare — disk full, permission denied). On failure the in-memory
    cache is invalidated so the next load re-reads on-disk truth
    rather than serving a phantom "saved" value the user could later
    discover was never persisted."""
    global _cache
    ok = _atomic_write_json(TASKS_FILE, state)
    if not ok:
        # In-memory state is now ahead of disk — invalidate so the
        # next load_state() goes back to disk and serves consistent
        # data, even if that means "losing" the failed mutation.
        _cache = None
        return False
    try:
        _cache = (TASKS_FILE.stat().st_mtime, state)
    except OSError:
        _cache = None
    return True


# ---------------- Public read API ----------------

def tasks_for_session(session_id: str) -> list[dict]:
    """Return the list of tasks for a session, in render order:
    open user tasks first, then pending suggestions, then recently-done.
    Dismissed entries are filtered out — they exist only to keep Haiku
    from re-suggesting the same content.

    Returns a copy of the list (safe to mutate); the underlying state
    is not touched."""
    if not session_id:
        return []
    state = load_state()
    entry = (state.get("sessions") or {}).get(session_id)
    if not isinstance(entry, dict):
        return []
    tasks = [t for t in (entry.get("tasks") or []) if t.get("status") != "dismissed"]
    return _render_sort(tasks)


def _render_sort(tasks: list[dict]) -> list[dict]:
    """Order tasks for rendering on a card.

    Bucket order:
      0. Open user-approved tasks (createdAt ascending — oldest first)
      1. Pending suggestions (createdAt ascending)
      2. Done user tasks (completedAt descending — most-recently-done first)

    The first two buckets together represent "things to look at"; bucket
    2 is recency-ordered history. Suggestions go after open user tasks
    so the user's own list dominates the visual weight.
    """
    def bucket(t: dict) -> int:
        status = t.get("status") or "open"
        if status == "done":
            return 2
        if t.get("source") == "suggested" and not t.get("approved"):
            return 1
        return 0

    def key(t: dict):
        b = bucket(t)
        if b == 2:
            # Most recently done first — negate epoch
            return (b, -(t.get("completedAt") or 0))
        return (b, t.get("createdAt") or 0)

    return sorted(tasks, key=key)


# ---------------- Public mutation API ----------------

def _content_is_valid(content: str) -> bool:
    """Reject empty / whitespace-only / absurdly-long task strings.
    Keeps malformed messages from the WebView bridge out of the file."""
    if not isinstance(content, str):
        return False
    stripped = content.strip()
    if not stripped:
        return False
    if len(stripped) > 280:  # tweet-ish; tasks aren't documents
        return False
    return True


def create_task(session_id: str, content: str) -> Optional[dict]:
    """Append a user-authored task to a session. Returns the new task
    dict on success, None on validation failure or persistence error.

    Idempotency: two creations with identical content within 2 seconds
    are coalesced — protects against double-click on the + add button.
    """
    if not session_id or not _content_is_valid(content):
        return None
    stripped = content.strip()
    now = time.time()
    with _LOCK:
        state = load_state()
        sessions = state.setdefault("sessions", {})
        entry = sessions.setdefault(session_id, _empty_session_entry())
        tasks = entry.setdefault("tasks", [])
        # Double-click coalesce: same content created < 2s ago wins.
        for t in tasks[-5:]:  # only scan the tail
            if (
                t.get("content") == stripped
                and t.get("source") == "user"
                and (now - (t.get("createdAt") or 0)) < 2.0
            ):
                return t
        task = {
            "id": _new_task_id(),
            "content": stripped,
            "status": "open",
            "source": "user",
            "approved": True,
            "createdAt": now,
            "completedAt": None,
            "suggestedBy": None,
            "suggestionContext": None,
        }
        tasks.append(task)
        # Enforce cap (no eviction of open tasks).
        open_count = sum(1 for t in tasks if t.get("status") == "open")
        if open_count > _TASKS_PER_SESSION_MAX:
            # Roll back — refuse to add more. Caller can surface this.
            tasks.pop()
            return None
        _save_state(state)
        return task


def _find_task(state: dict, session_id: str, task_id: str) -> Optional[dict]:
    entry = (state.get("sessions") or {}).get(session_id)
    if not isinstance(entry, dict):
        return None
    for t in entry.get("tasks") or []:
        if t.get("id") == task_id:
            return t
    return None


def complete_task(session_id: str, task_id: str) -> bool:
    """Flip a task open → done. Idempotent — returns True if the task
    is now in the requested terminal state, False if it wasn't found."""
    if not session_id or not task_id:
        return False
    with _LOCK:
        state = load_state()
        t = _find_task(state, session_id, task_id)
        if t is None:
            return False
        if t.get("status") == "done":
            return True
        t["status"] = "done"
        t["completedAt"] = time.time()
        _save_state(state)
        return True


def reopen_task(session_id: str, task_id: str) -> bool:
    """Flip a task done → open. The companion to ``complete_task`` —
    needed so the glyph toggle in the UI is round-trippable rather
    than terminal."""
    if not session_id or not task_id:
        return False
    with _LOCK:
        state = load_state()
        t = _find_task(state, session_id, task_id)
        if t is None:
            return False
        if t.get("status") == "open":
            return True
        t["status"] = "open"
        t["completedAt"] = None
        _save_state(state)
        return True


def toggle_task(session_id: str, task_id: str) -> Optional[str]:
    """One-shot helper used by the glyph-click message handler — returns
    the task's new status ("open" or "done"), or None on miss. Avoids
    a JS round-trip to figure out which direction we're flipping."""
    if not session_id or not task_id:
        return None
    with _LOCK:
        state = load_state()
        t = _find_task(state, session_id, task_id)
        if t is None:
            return None
        if t.get("status") == "done":
            t["status"] = "open"
            t["completedAt"] = None
        else:
            t["status"] = "done"
            t["completedAt"] = time.time()
        _save_state(state)
        return t["status"]


def delete_task(session_id: str, task_id: str) -> bool:
    """Hard-remove a user-authored task. Suggested tasks should use
    ``reject_suggestion`` instead so Haiku knows not to re-suggest."""
    if not session_id or not task_id:
        return False
    with _LOCK:
        state = load_state()
        entry = (state.get("sessions") or {}).get(session_id)
        if not isinstance(entry, dict):
            return False
        before = len(entry.get("tasks") or [])
        entry["tasks"] = [t for t in (entry.get("tasks") or []) if t.get("id") != task_id]
        if len(entry["tasks"]) == before:
            return False
        _save_state(state)
        return True


def approve_suggestion(session_id: str, task_id: str) -> bool:
    """Ratify a Haiku-suggested task into a normal user task. Used by
    v1.1's approve path — included now since the schema already
    supports it and the bridge action will land soon."""
    if not session_id or not task_id:
        return False
    with _LOCK:
        state = load_state()
        t = _find_task(state, session_id, task_id)
        if t is None or t.get("source") != "suggested":
            return False
        t["approved"] = True
        # Keep source="suggested" so we can later analyse approve vs
        # reject rates — but flip approved=true so the UI renders it
        # as a normal task.
        _save_state(state)
        return True


def reject_suggestion(session_id: str, task_id: str) -> bool:
    """Dismiss a Haiku-suggested task. The task itself is marked
    "dismissed" (not deleted) so the suggester can avoid re-proposing
    the same content within this session."""
    if not session_id or not task_id:
        return False
    with _LOCK:
        state = load_state()
        t = _find_task(state, session_id, task_id)
        if t is None or t.get("source") != "suggested":
            return False
        t["status"] = "dismissed"
        t["completedAt"] = time.time()
        # Add the content to the dismissed-suggestions list so the
        # Haiku sweeper can filter it out as a negative example.
        entry = (state.get("sessions") or {}).get(session_id)
        if isinstance(entry, dict):
            dismissed = entry.setdefault("dismissedSuggestions", [])
            stripped = (t.get("content") or "").strip()
            if stripped and stripped not in dismissed:
                dismissed.append(stripped)
                # Cap the dismissed list so it doesn't grow forever.
                if len(dismissed) > 50:
                    del dismissed[: len(dismissed) - 50]
        _save_state(state)
        return True


def summary_for_session(session_id: str) -> tuple[int, int]:
    """Return (open_count, total_count) for a session's tasks — used
    by the card render to compute the ``(done/total)`` label without
    re-walking the list. Excludes dismissed entries."""
    tasks = tasks_for_session(session_id)
    open_count = sum(1 for t in tasks if t.get("status") == "open")
    total = len(tasks)
    done = total - open_count
    return (done, total)
