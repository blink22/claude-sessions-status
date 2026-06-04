#!/usr/bin/env python3
"""Bridge-dispatch unit tests for floating.PopoverVC._handle_script_message.

Verifies that JS-bridge actions (taskCreate / taskToggle / taskDelete /
taskApprove / taskReject) correctly drive the tasks.py persistence
layer. Uses an isolated tasks file at /tmp/ to keep the user's
production data (~/.claude-sessions-status-tasks.json) untouched.

Run:
    ~/.claude-sessions-status-venv/bin/python3 tests/test_bridge_dispatch.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

# Isolate the tasks file BEFORE importing tasks/floating so the
# module-level constants point at the test fixture.
TEST_TASKS = Path("/tmp/test-tasks-bridge.json")
if TEST_TASKS.exists():
    TEST_TASKS.unlink()

import tasks  # noqa: E402
tasks.TASKS_FILE = TEST_TASKS
tasks._cache = None
tasks._swept = False

import floating  # noqa: E402


class FakePopoverVC:
    """Duck-types PopoverVC for the dispatch method."""

    def __init__(self):
        self.last_rendered_rows = []
        self.popover_ref = None
        self.kanban_web = None
        self.kanban_web_ready = False
        self.kanban_web_pending = None
        self.refresh_count = 0
        self.opened_sessions = []

    def refresh(self):
        self.refresh_count += 1

    def _open_session_in_terminal(self, sid, cwd):
        self.opened_sessions.append((sid, cwd))

    def _kanban_web_evaluate(self, payload):
        pass


handle = floating.PopoverVC._handle_script_message
SID = "test-session-bridge"


def t(label, fn):
    """Tiny test runner with consistent output."""
    try:
        fn()
        print(f"  PASS  {label}")
        return True
    except AssertionError as e:
        print(f"  FAIL  {label}: {e}")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"  ERR   {label}: {e!r}")
        return False


def main() -> int:
    vc = FakePopoverVC()
    results: list[bool] = []

    def case(name, body):
        results.append(t(name, body))

    # --- taskCreate ---
    def _t1():
        handle(vc, "taskCreate", {"sessionId": SID, "content": "first"})
        state = tasks.load_state()
        assert len(state["sessions"][SID]["tasks"]) == 1
    case("T1 taskCreate persists", _t1)

    created_id = tasks.load_state()["sessions"][SID]["tasks"][0]["id"]

    # --- taskToggle ---
    def _t2():
        handle(vc, "taskToggle", {"sessionId": SID, "taskId": created_id})
        assert tasks.load_state()["sessions"][SID]["tasks"][0]["status"] == "done"
    case("T2 taskToggle open→done", _t2)

    def _t3():
        handle(vc, "taskToggle", {"sessionId": SID, "taskId": created_id})
        assert tasks.load_state()["sessions"][SID]["tasks"][0]["status"] == "open"
    case("T3 taskToggle done→open", _t3)

    # --- taskDelete ---
    def _t4():
        handle(vc, "taskDelete", {"sessionId": SID, "taskId": created_id})
        assert len(tasks.load_state()["sessions"][SID]["tasks"]) == 0
    case("T4 taskDelete removes row", _t4)

    # --- taskApprove ---
    def _t5():
        state = tasks.load_state()
        state["sessions"].setdefault(SID, tasks._empty_session_entry())
        state["sessions"][SID]["tasks"].append({
            "id": "sug1", "content": "Approve me", "status": "open",
            "source": "suggested", "approved": False,
            "createdAt": time.time(), "completedAt": None,
        })
        tasks._save_state(state)
        handle(vc, "taskApprove", {"sessionId": SID, "taskId": "sug1"})
        tt = next(t for t in tasks.load_state()["sessions"][SID]["tasks"]
                  if t["id"] == "sug1")
        assert tt["approved"] is True
    case("T5 taskApprove flips approved=True", _t5)

    # --- taskReject ---
    def _t6():
        state = tasks.load_state()
        state["sessions"][SID]["tasks"].append({
            "id": "sug2", "content": "Reject me", "status": "open",
            "source": "suggested", "approved": False,
            "createdAt": time.time(), "completedAt": None,
        })
        tasks._save_state(state)
        handle(vc, "taskReject", {"sessionId": SID, "taskId": "sug2"})
        tt = next(t for t in tasks.load_state()["sessions"][SID]["tasks"]
                  if t["id"] == "sug2")
        assert tt["status"] == "dismissed"
        assert "Reject me" in tasks.load_state()["sessions"][SID]["dismissedSuggestions"]
    case("T6 taskReject dismisses + tracks content", _t6)

    # --- Malformed input safety ---
    def _t7():
        before = len(tasks.load_state()["sessions"][SID]["tasks"])
        handle(vc, "taskCreate", {"sessionId": SID})
        handle(vc, "taskCreate", {"sessionId": SID, "content": ""})
        handle(vc, "taskCreate", {"sessionId": SID, "content": "  "})
        handle(vc, "taskCreate", {"sessionId": SID, "content": {"x": 1}})
        handle(vc, "taskCreate", {"sessionId": SID, "content": "x" * 500})
        handle(vc, "taskCreate", {})
        handle(vc, "taskToggle", {"sessionId": SID, "taskId": "nope"})
        handle(vc, "taskDelete", {"sessionId": SID, "taskId": "nope"})
        handle(vc, "taskApprove", {"sessionId": SID, "taskId": "nope"})
        handle(vc, "taskReject", {"sessionId": SID, "taskId": "nope"})
        after = len(tasks.load_state()["sessions"][SID]["tasks"])
        assert before == after, f"malformed inputs polluted file: {before}→{after}"
    case("T7 10 malformed inputs rejected cleanly", _t7)

    # --- Unknown action ---
    def _t8():
        handle(vc, "taskUnknown", {"sessionId": SID})  # must not crash
    case("T8 unknown action no-ops", _t8)

    # --- Refresh count ---
    def _t9():
        # T1 + T2 + T3 + T4 + T5 + T6 = 6 successful mutations
        assert vc.refresh_count >= 6, f"refresh count {vc.refresh_count}"
    case("T9 successful mutations trigger refresh", _t9)

    # --- NSString-like inputs ---
    def _t10():
        class FakeNSString(str):
            def UTF8String(self): return self.encode("utf-8")
        handle(vc, "taskCreate", {
            "sessionId": FakeNSString(SID),
            "content": FakeNSString("NSString task"),
        })
        assert any(t["content"] == "NSString task"
                   for t in tasks.load_state()["sessions"][SID]["tasks"])
    case("T10 NSString-like inputs accepted", _t10)

    passed = sum(results)
    total = len(results)
    print()
    print(f"{passed}/{total} passed")
    if TEST_TASKS.exists():
        TEST_TASKS.unlink()
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
