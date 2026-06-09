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

    # Reorder
    def _reorder():
        s = "reorder-test"
        a = tasks.create_task(s, "alpha")
        b = tasks.create_task(s, "beta")
        c = tasks.create_task(s, "gamma")
        # Default order matches creation
        ordered = tasks.tasks_for_session(s)
        assert [t["content"] for t in ordered] == ["alpha", "beta", "gamma"]
        # Reorder: gamma → alpha → beta
        ok = tasks.reorder_tasks(s, [c["id"], a["id"], b["id"]])
        assert ok is True
        ordered = tasks.tasks_for_session(s)
        assert [t["content"] for t in ordered] == ["gamma", "alpha", "beta"]
        # Positions are 0/1/2
        positions = {t["content"]: t["position"] for t in ordered if "position" in t}
        assert positions == {"gamma": 0, "alpha": 1, "beta": 2}
        # Reorder with partial list — unmentioned tasks keep relative order
        # at the end. Reorder only [beta, gamma] → alpha should follow.
        tasks.reorder_tasks(s, [b["id"], c["id"]])
        ordered = tasks.tasks_for_session(s)
        # beta(0) + gamma(1) explicit; alpha gets next pos = 2 since
        # it's the only unmentioned task
        assert ordered[0]["content"] == "beta"
        assert ordered[1]["content"] == "gamma"
        assert ordered[2]["content"] == "alpha"
        # Empty / invalid input rejected
        assert tasks.reorder_tasks(s, []) is False
        assert tasks.reorder_tasks(s, [123, None]) is False  # type: ignore
        assert tasks.reorder_tasks("unknown-sid", [a["id"]]) is False
        # Idempotent — submitting same order doesn't crash
        same = [t["id"] for t in tasks.tasks_for_session(s) if t.get("status") == "open"]
        assert tasks.reorder_tasks(s, same) is True
        # Stale IDs (none match any task in this session) — bail
        # without scrambling unrelated positions.
        before = {t["content"]: t.get("position") for t in tasks.tasks_for_session(s)}
        assert tasks.reorder_tasks(s, ["t_doesnt_exist", "t_also_gone"]) is False
        after = {t["content"]: t.get("position") for t in tasks.tasks_for_session(s)}
        assert before == after, "stale-IDs reorder mutated positions"
        # Approved suggestion participates in reorder (regression test
        # for schema-migration of suggested→user-approved promotion).
        state = tasks.load_state()
        state["sessions"][s]["tasks"].append({
            "id": "sug-reorder", "content": "Suggested",
            "status": "open", "source": "suggested", "approved": False,
            "createdAt": time.time(), "completedAt": None,
        })
        tasks._save_state(state)
        tasks.approve_suggestion(s, "sug-reorder")
        # Now reorder so the approved suggestion is first
        all_open = [t["id"] for t in tasks.tasks_for_session(s) if t.get("status") == "open"]
        # Move sug-reorder to the front
        all_open.remove("sug-reorder")
        all_open.insert(0, "sug-reorder")
        assert tasks.reorder_tasks(s, all_open) is True
        ordered = tasks.tasks_for_session(s)
        first_open = next(t for t in ordered if t.get("status") == "open")
        assert first_open["id"] == "sug-reorder", \
            f"approved suggestion didn't move to front: {[t['id'] for t in ordered]}"
    t("reorder_tasks — update positions + idempotent + stale + approved sug", _reorder)

    # Launch-from-task attach: session_of_task + attach_task_to_new_session
    def _attach():
        # A task created unattached lives under its cwd's pseudo-session.
        cwd = "/tmp/proj-attach"
        pseudo = tasks.unattached_sid_for_cwd(cwd)
        created = tasks.create_unattached_task(cwd, "ship the thing")
        assert created is not None
        tid = created["id"]
        # session_of_task finds the pseudo bucket.
        assert tasks.session_of_task(tid) == pseudo
        # Attaching to a freshly-minted session UUID migrates it there.
        new_sid = "11111111-2222-3333-4444-555555555555"
        assert tasks.attach_task_to_new_session(tid, new_sid) is True
        assert tasks.session_of_task(tid) == new_sid
        # It no longer lives in the unattached bucket.
        assert all(t["id"] != tid for t in tasks.tasks_for_session(pseudo))
        # Now under the new session, render-visible.
        assert any(t["id"] == tid for t in tasks.tasks_for_session(new_sid))
        # Re-attaching to the same session is a no-op success.
        assert tasks.attach_task_to_new_session(tid, new_sid) is True
        # Unknown task id → False (nothing to move).
        assert tasks.attach_task_to_new_session("t_does_not_exist", new_sid) is False
        # Empty args → False, never raises.
        assert tasks.attach_task_to_new_session("", new_sid) is False
        assert tasks.attach_task_to_new_session(tid, "") is False
        assert tasks.session_of_task("t_does_not_exist") is None
    t("attach_task_to_new_session — unattached → new session UUID", _attach)

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
