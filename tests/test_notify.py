#!/usr/bin/env python3
"""Unit tests for the proactive NEEDS-YOU notification logic in floating.py.

Covers the opt-in toggle, persisted notify-state (save/load + GC), the
AppleScript string escaper, and the transition/debounce/coalesce rules in
BadgeController._maybe_notify. Notifications are stubbed so the suite runs
silently (no real banners). State files are redirected to /tmp so the
user's real preferences are untouched.

Run:
    ~/.claude-sessions-status-venv/bin/python3 tests/test_notify.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import floating as f  # noqa: E402
from floating import BadgeController  # noqa: E402

# Redirect persisted state to /tmp BEFORE any helper runs.
f.NOTIFY_STATE_FILE = Path("/tmp/test-notify-state.json")
f.NOTIFY_ENABLED_FILE = Path("/tmp/test-notify-enabled")
f.NOTIFY_STATE_FILE.unlink(missing_ok=True)

_passed = 0


def ok(label: str) -> None:
    global _passed
    _passed += 1
    print(f"  PASS  {label}")


def row(sid, epoch, title="Sess", phase="Asking you"):
    return {"s": {"sessionId": sid}, "lastTurnEpoch": epoch,
            "title": title, "phase_label": phase}


def test_opt_in_toggle():
    f._write_notify_enabled(True)
    assert f._read_notify_enabled() is True
    f._write_notify_enabled(False)
    assert f._read_notify_enabled() is False
    ok("opt-in toggle round-trips, defaults off")


def test_osa_quote():
    assert f._osa_quote('a"b\\c') == '"a\\"b\\\\c"'
    ok("osascript string escaper handles quotes + backslashes")


def test_state_gc():
    now = time.time()
    f._save_notify_state({
        "fresh": {"epoch": 5.0, "at": now},
        "stale": {"epoch": 1.0, "at": now - 40 * 86400},
        "junk": "not-a-dict",
    })
    back = f._load_notify_state()
    assert "fresh" in back and "stale" not in back and "junk" not in back, back
    ok("notify-state save/load prunes stale + malformed entries")


def test_transition_debounce_coalesce():
    f.NOTIFY_STATE_FILE.unlink(missing_ok=True)
    f._write_notify_enabled(True)
    posts = []
    f._post_notification = lambda *a, **k: posts.append((a, k))

    class Fake:
        notify_seeded = False
        pulses = 0
        _maybe_notify = BadgeController._maybe_notify

        def _pulse_badge(self):
            self.pulses += 1

    me = Fake()

    # Seeding pass: a session already waiting at launch must stay silent.
    me._maybe_notify([row("a", 100.0)])
    assert posts == [] and me.notify_seeded
    ok("first tick seeds baseline silently")

    # A session sitting in NEEDS YOU (same epoch) never re-fires.
    me._maybe_notify([row("a", 100.0)])
    assert posts == []
    ok("static NEEDS YOU is debounced")

    # A genuinely new session fires once, carries the phase as subtitle,
    # and pulses the badge.
    me._maybe_notify([row("a", 100.0), row("b", 200.0, "Build", "Proposing a plan")])
    assert len(posts) == 1 and posts[0][1].get("subtitle") == "Proposing a plan"
    assert me.pulses == 1
    ok("new NEEDS YOU fires a single banner + pulse")

    # A new turn (epoch advances past epsilon) re-notifies.
    posts.clear()
    me._maybe_notify([row("a", 101.0), row("b", 200.0)])
    assert len(posts) == 1
    ok("advanced turn re-notifies")

    # Sub-epsilon epoch drift is ignored.
    posts.clear()
    me._maybe_notify([row("a", 101.3), row("b", 200.0)])
    assert posts == []
    ok("sub-epsilon epoch drift is ignored")

    # A simultaneous burst coalesces into one summary banner.
    posts.clear()
    me._maybe_notify([row("c", 300.0), row("d", 400.0)])
    assert len(posts) == 1 and "2 sessions" in posts[0][0][1].lower()
    ok("simultaneous burst coalesces into one banner")

    # Disabled → fully silent.
    f._write_notify_enabled(False)
    posts.clear()
    me._maybe_notify([row("e", 500.0)])
    assert posts == []
    ok("disabled opt-in stays silent")


if __name__ == "__main__":
    print("test_notify.py")
    test_opt_in_toggle()
    test_osa_quote()
    test_state_gc()
    test_transition_debounce_coalesce()
    f.NOTIFY_STATE_FILE.unlink(missing_ok=True)
    f.NOTIFY_ENABLED_FILE.unlink(missing_ok=True)
    print(f"\n{_passed}/{_passed} passed")
