#!/usr/bin/env python3
"""Smoke tests for scripts/tasks.py. Uses an isolated fixture path
so the user's production file (~/.claude-sessions-status-tasks.json)
is never touched."""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

TEST_FILE = Path("/tmp/test-tasks-module.json")
if TEST_FILE.exists():
    TEST_FILE.unlink()

import tasks  # noqa: E402
tasks.TASKS_FILE = TEST_FILE
tasks._cache = None
tasks._swept = False

SID = "smoke-suite"
results: list[bool] = []


def t(label, body):
    try:
        body()
        print(f"  PASS  {label}")
        results.append(True)
    except AssertionError as e:
        print(f"  FAIL  {label}: {e}")
        results.append(False)
    except Exception as e:  # noqa: BLE001
        print(f"  ERR   {label}: {e!r}")
        results.append(False)


def main() -> int:
    # CRUD round-trip
    def _crud():
        a = tasks.create_task(SID, "task A")
        b = tasks.create_task(SID, "task B")
        c = tasks.create_task(SID, "task C")
        assert a and b and c
        assert tasks.toggle_task(SID, a["id"]) == "done"
        assert tasks.delete_task(SID, b["id"]) is True
        assert tasks.summary_for_session(SID) == (1, 2)
    t("CRUD round-trip", _crud)

    # Validation
    def _validation():
        assert tasks.create_task(SID, "") is None
        assert tasks.create_task(SID, "   ") is None
        assert tasks.create_task(SID, "x" * 281) is None
        assert tasks.create_task(SID, None) is None  # type: ignore
        assert tasks.create_task(None, "x") is None  # type: ignore
        assert tasks.toggle_task(SID, "nope") is None
        assert tasks.delete_task(SID, "nope") is False
    t("validation rejects bad input", _validation)

    # Unicode + emoji + control chars
    def _unicode():
        out = tasks.create_task(SID, "🚀 السلام عليكم \x00 done")
        assert out is not None
        state = tasks.load_state()
        contents = [tt["content"] for tt in state["sessions"][SID]["tasks"]]
        assert "🚀 السلام عليكم \x00 done" in contents
    t("unicode + RTL + control chars round-trip", _unicode)

    # Concurrent writes (10 threads)
    def _concurrent():
        # Fresh session to avoid mixing with prior tasks
        sid = "concurrent-sid"
        def worker(i):
            tasks.create_task(sid, f"thread-{i}")
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for th in threads: th.start()
        for th in threads: th.join()
        state = tasks.load_state()
        rows = state["sessions"][sid]["tasks"]
        assert len(rows) == 10, f"got {len(rows)} rows, want 10"
        unique_ids = {r["id"] for r in rows}
        assert len(unique_ids) == 10, "id collision"
    t("10-thread concurrent create", _concurrent)

    # Atomic write + cache invalidation on failure
    def _atomic():
        ok = tasks._atomic_write_json(TEST_FILE, {"version": 1, "sessions": {}})
        assert ok is True
        # Simulate failure by writing to an unwritable path
        bad = Path("/nonexistent-dir-xyz/foo.json")
        ok2 = tasks._atomic_write_json(bad, {})
        assert ok2 is False
    t("atomic_write_json bool return", _atomic)

    # Approve / reject
    def _approve_reject():
        s2 = "approve-test"
        state = tasks.load_state()
        state["sessions"].setdefault(s2, tasks._empty_session_entry())
        state["sessions"][s2]["tasks"].append({
            "id": "sug-a", "content": "Approve me", "status": "open",
            "source": "suggested", "approved": False,
            "createdAt": time.time(), "completedAt": None,
        })
        state["sessions"][s2]["tasks"].append({
            "id": "sug-r", "content": "Reject me", "status": "open",
            "source": "suggested", "approved": False,
            "createdAt": time.time(), "completedAt": None,
        })
        tasks._save_state(state)
        assert tasks.approve_suggestion(s2, "sug-a") is True
        assert tasks.reject_suggestion(s2, "sug-r") is True
        # Approving a non-suggested returns False
        u = tasks.create_task(s2, "user task")
        assert tasks.approve_suggestion(s2, u["id"]) is False
    t("approve/reject suggestion paths", _approve_reject)

    # Render order
    def _render_order():
        s3 = "render-order"
        u1 = tasks.create_task(s3, "U1")
        u2 = tasks.create_task(s3, "U2")
        tasks.toggle_task(s3, u2["id"])  # u2 done
        state = tasks.load_state()
        state["sessions"][s3]["tasks"].append({
            "id": "sug-x", "content": "Suggestion", "status": "open",
            "source": "suggested", "approved": False,
            "createdAt": time.time(), "completedAt": None,
        })
        tasks._save_state(state)
        ordered = tasks.tasks_for_session(s3)
        # Expect: open user → suggested → done
        assert ordered[0]["content"] == "U1"
        assert ordered[1]["source"] == "suggested"
        assert ordered[2]["status"] == "done"
    t("render order: open user → suggested → done", _render_order)

    # Edit task content
    def _edit():
        s = "edit-test"
        t1 = tasks.create_task(s, "Original content")
        assert t1 is not None
        updated = tasks.update_task(s, t1["id"], "New content")
        assert updated is not None
        assert updated["content"] == "New content"
        assert updated["id"] == t1["id"]
        assert updated["createdAt"] == t1["createdAt"]
        assert "updatedAt" in updated
        # Empty / whitespace / overlong rejected
        assert tasks.update_task(s, t1["id"], "") is None
        assert tasks.update_task(s, t1["id"], "   ") is None
        assert tasks.update_task(s, t1["id"], "x" * 281) is None
        # Missing task
        assert tasks.update_task(s, "nope", "x") is None
        # Suggestions are immutable until approved
        state = tasks.load_state()
        state["sessions"][s]["tasks"].append({
            "id": "sug-edit", "content": "Suggested",
            "status": "open", "source": "suggested", "approved": False,
            "createdAt": time.time(), "completedAt": None,
        })
        tasks._save_state(state)
        assert tasks.update_task(s, "sug-edit", "Hijack") is None
        # Once approved, editable
        tasks.approve_suggestion(s, "sug-edit")
        assert tasks.update_task(s, "sug-edit", "Rewritten") is not None
        # No-op when content unchanged still returns the task
        latest = tasks.update_task(s, t1["id"], "New content")
        assert latest is not None
    t("update_task — edit content + reject empty/long/suggestion", _edit)

    # Corrupt file recovery
    def _corrupt():
        TEST_FILE.write_text("not json {{", encoding="utf-8")
        tasks._cache = None
        # load_state should swallow + back up + return empty
        state = tasks.load_state()
        assert state.get("version") == 1
        # The backup file should now exist
        backups = list(TEST_FILE.parent.glob(TEST_FILE.name + ".corrupt-*"))
        assert backups, "no corrupt backup created"
        for b in backups: b.unlink()
    t("corrupt file → backup + recover", _corrupt)

    passed = sum(results)
    total = len(results)
    print()
    print(f"{passed}/{total} passed")
    if TEST_FILE.exists():
        TEST_FILE.unlink()
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
