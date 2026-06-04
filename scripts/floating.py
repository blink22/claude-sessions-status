#!/usr/bin/env python3
"""Floating always-on-top panel showing the Claude sessions dashboard.

Runs as a separate process from SwiftBar (the menu bar plugin). Reuses
session/phase/state logic from dashboard.py — so the panel and the menu
bar always agree on what's happening.

Two layout modes:
  - 'list'   (default) — vertical list, similar to the menu dropdown
  - 'kanban' — three columns side-by-side (NEEDS YOU / WORKING / FINISHED)

Usage (normally invoked via `claude-sessions-status panel`):
  python floating.py [--kanban]

State files (in $HOME):
  .claude-sessions-status-window.json    saved x/y/w/h
  .claude-sessions-status-panel.pid      pid of the running panel
  .claude-sessions-status-panel-mode     current layout mode

Requires PyObjC (pyobjc-framework-Cocoa). The launcher in install.py
spawns this script via a dedicated venv with PyObjC pre-installed, so
end users don't have to think about it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# ---------- PyObjC ----------
try:
    from AppKit import (
        NSApp,
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSAttributedString,
        NSBackingStoreBuffered,
        NSBezierPath,
        NSButton,
        NSColor,
        NSCursor,
        NSEvent,
        NSFont,
        NSFontAttributeName,
        NSForegroundColorAttributeName,
        NSFontWeightMedium,
        NSFontWeightRegular,
        NSFontWeightSemibold,
        NSImage,
        NSImageSymbolConfiguration,
        NSKernAttributeName,
        NSLinkAttributeName,
        NSMakePoint,
        NSMakeRect,
        NSMakeSize,
        NSMenu,
        NSMenuItem,
        NSMutableAttributedString,
        NSMutableParagraphStyle,
        NSParagraphStyleAttributeName,
        NSPanel,
        NSPopover,
        NSPopUpButton,
        NSScrollView,
        NSSegmentedControl,
        NSSegmentStyleAutomatic,
        NSSegmentSwitchTrackingSelectOne,
        NSTextTab,
        NSStackView,
        NSTextField,
        NSTextView,
        NSView,
        NSViewController,
        NSViewHeightSizable,
        NSViewMaxXMargin,
        NSViewMinXMargin,
        NSViewMinYMargin,
        NSViewWidthSizable,
        NSVisualEffectView,
        NSWindowStyleMaskBorderless,
        NSWindowStyleMaskClosable,
        NSWindowStyleMaskMiniaturizable,
        NSWindowStyleMaskNonactivatingPanel,
        NSWindowStyleMaskResizable,
        NSWindowStyleMaskTitled,
    )
    from Foundation import NSObject, NSTimer, NSURL
    # WebKit is used by the kanban view to render the Linear-style
    # design from a single HTML/CSS file — pixel-parity with the
    # browser mockup without re-implementing CSS in CALayer.
    from WebKit import (
        WKWebView,
        WKWebViewConfiguration,
        WKUserContentController,
    )
    import objc

    NSFloatingWindowLevel = 5  # AppKit constant — not always exported by PyObjC.
    # NSWindow.collectionBehavior flags (not always exported either).
    NS_WINDOW_COLLECTION_CAN_JOIN_ALL_SPACES = 1 << 0
    NS_WINDOW_COLLECTION_STATIONARY = 1 << 4
    NS_WINDOW_COLLECTION_FULL_SCREEN_AUX = 1 << 8
    # NSStackView constants used below — hardcoded to avoid PyObjC import drift.
    NS_USER_INTERFACE_LAYOUT_ORIENTATION_HORIZONTAL = 0
    NS_USER_INTERFACE_LAYOUT_ORIENTATION_VERTICAL = 1
    NS_STACK_VIEW_GRAVITY_TOP = 1     # used for vertical stacks (top-aligned)
    NS_STACK_VIEW_GRAVITY_LEADING = 1 # used for horizontal stacks (leading-aligned)
    NS_STACK_VIEW_DISTRIBUTION_FILL_EQUALLY = 1
except ImportError as e:
    sys.stderr.write(
        "PyObjC is required for the floating panel.\n"
        f"  ({e})\n"
        "Run `claude-sessions-status install` to set up the dedicated venv,\n"
        "or install manually with `pip install pyobjc-framework-Cocoa`.\n"
    )
    sys.exit(1)

# ---------- Reuse dashboard logic ----------
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dashboard  # noqa: E402
import tasks as tasks_module  # noqa: E402 — per-session user-curated tasks
from dashboard import (  # noqa: E402
    classify,
    desktop_titles,
    find_sessions,
    format_ago,
    infer_phase,
    is_dormant,
    live_session_ids,
    next_action,
    recent_sessions,
    resolve_title,
    session_gist,
    state_for,
    subagent_summary,
    subagents_for_session,
    transcript_meta,
    SUBAGENT_DONE,
    SUBAGENT_INTERRUPTED,
    SUBAGENT_MAX_DISPLAY,
    SUBAGENT_RUNNING,
)


# ---------- Config ----------
HOME = Path(os.path.expanduser("~"))
WINDOW_STATE_FILE = HOME / ".claude-sessions-status-window.json"
PID_FILE = HOME / ".claude-sessions-status-panel.pid"
MODE_FILE = HOME / ".claude-sessions-status-panel-mode"
# File-flag for clean shutdown. POSIX signal delivery isn't reliable
# inside PyObjC's NSApp.run() runloop, so `panel --quit` writes this
# file and our periodic refresh tick notices it and terminates cleanly.
QUIT_FLAG = HOME / ".claude-sessions-status-panel-quit"
DEFAULT_FRAME_LIST = (240.0, 240.0, 420.0, 600.0)        # x, y, w, h
DEFAULT_FRAME_KANBAN = (180.0, 240.0, 1000.0, 540.0)
REFRESH_SECS = max(1.0, float(os.environ.get("CLAUDE_SESSIONS_REFRESH", "5")))

# Popover layout mode — list (current) or kanban (3 columns).
# Persists across launches so the badge remembers the user's preference.
POPOVER_MODE_FILE = HOME / ".claude-sessions-status-popover-mode"
POPOVER_LIST_SIZE = (360.0, 480.0)
POPOVER_KANBAN_SIZE = (720.0, 480.0)
# When the user toggles "Show older" in kanban mode, the dormant
# sessions render as a 4th column on the right. The popover widens
# to give that column room without squeezing the other three.
POPOVER_KANBAN_WITH_DORMANT_SIZE = (940.0, 480.0)


def _read_popover_mode() -> str:
    try:
        v = POPOVER_MODE_FILE.read_text(encoding="utf-8").strip()
        if v in ("list", "kanban"):
            return v
    except OSError:
        pass
    return "list"


def _write_popover_mode(mode: str) -> None:
    if mode not in ("list", "kanban"):
        return
    _atomic_write_text(POPOVER_MODE_FILE, mode)


# ---------- Dormant visibility (popover-only toggle) ----------
# Whether the dormant section is rendered inside the popover. Persists
# across launches. Defaults to True so behavior matches what shipped
# previously (dormant always visible at the bottom).
SHOW_DORMANT_FILE = HOME / ".claude-sessions-status-show-dormant"


def _read_show_dormant() -> bool:
    try:
        v = SHOW_DORMANT_FILE.read_text(encoding="utf-8").strip().lower()
        if v in ("0", "false", "no", "off"):
            return False
        if v in ("1", "true", "yes", "on"):
            return True
    except OSError:
        pass
    return True


def _write_show_dormant(value: bool) -> None:
    _atomic_write_text(SHOW_DORMANT_FILE, "1" if value else "0")


# ---------- Density (Glance / Focus / Detail) ----------
# How much per-session info each row/card renders. Orthogonal to
# List vs. Kanban. Persists across launches.
#   glance  — one line: title + age (max sessions visible)
#   focus   — title + phase + gist (default; balanced)
#   detail  — adds last-assistant snippet + cwd path
DENSITY_FILE = HOME / ".claude-sessions-status-density"
DENSITIES = ("glance", "focus", "detail")


def _read_density() -> str:
    try:
        v = DENSITY_FILE.read_text(encoding="utf-8").strip().lower()
        if v in DENSITIES:
            return v
    except OSError:
        pass
    return "focus"


def _write_density(value: str) -> None:
    if value not in DENSITIES:
        return
    _atomic_write_text(DENSITY_FILE, value)


# ---------- Unread / seen state ----------
# Per-session "last seen" timestamp lives in ~/.claude-sessions-status-seen.json.
# A session is *unread* when its current lastTurnEpoch (real conversation
# activity) is later than the saved seen epoch. This is opt-in: sessions
# the user has never marked-as-read are simply not tracked, so a fresh
# install doesn't dump dozens of unread dots on day one.
SEEN_FILE = HOME / ".claude-sessions-status-seen.json"

# mtime-keyed cache: avoids re-reading + re-parsing the seen JSON on
# every 5s tick when nothing has changed. Invalidated on mtime change
# (either we wrote it, or another process did).
_seen_cache: tuple[float, dict] | None = None


def _load_seen() -> dict:
    global _seen_cache
    if not SEEN_FILE.exists():
        _seen_cache = None
        return {}
    try:
        mtime = SEEN_FILE.stat().st_mtime
    except OSError:
        return _seen_cache[1] if _seen_cache else {}
    if _seen_cache is not None and _seen_cache[0] == mtime:
        return _seen_cache[1]
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        data = data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    _seen_cache = (mtime, data)
    return data


def _atomic_write_text(path: Path, content: str) -> None:
    """Write `content` to `path` atomically via tempfile+os.replace.
    Protects against partial-write corruption if the process is killed
    mid-write, and against the read-modify-write races that occur when
    a second floating process briefly coexists (uninstall, doctor,
    leftover from a crash). The replace is atomic on POSIX — readers
    either see the old file or the new file, never a half-written one."""
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


_SEEN_GC_TTL_S = 30 * 86400.0  # 30 days


def _gc_seen(seen: dict) -> dict:
    """Drop entries whose `lastSeenAt` is older than the GC TTL. Keeps
    the seen file from growing unboundedly over months of use without
    expensively cross-referencing every entry against on-disk session
    files. Returns a NEW dict (does not mutate the input)."""
    cutoff = time.time() - _SEEN_GC_TTL_S
    out: dict = {}
    for sid, entry in seen.items():
        if not isinstance(entry, dict):
            continue
        ts = entry.get("lastSeenAt")
        if isinstance(ts, (int, float)) and ts >= cutoff:
            out[sid] = entry
    return out


def _save_seen(seen: dict) -> None:
    global _seen_cache
    # GC expired entries before each save — once per mark-read action,
    # which is rare enough that the cost is invisible.
    seen = _gc_seen(seen)
    _atomic_write_text(SEEN_FILE, json.dumps(seen, indent=2, sort_keys=True))
    # Update the in-process cache so the next _load_seen avoids a disk
    # read. Use the file's new mtime as the cache key.
    try:
        _seen_cache = (SEEN_FILE.stat().st_mtime, dict(seen))
    except OSError:
        _seen_cache = None


def _is_session_unread(session_id: str, last_turn_epoch, seen: dict) -> bool:
    """True if this session has had real activity (a new user/assistant
    turn) since we last marked it read. No prior entry → not unread —
    we don't manufacture unread state out of nothing."""
    if not session_id or last_turn_epoch is None:
        return False
    entry = seen.get(session_id)
    if not isinstance(entry, dict):
        return False
    seen_epoch = entry.get("lastSeenEpoch")
    if not isinstance(seen_epoch, (int, float)):
        return False
    # 0.5s slack to avoid millisecond drift causing flickering.
    return float(last_turn_epoch) > float(seen_epoch) + 0.5


def _mark_session_read(session_id: str, last_turn_epoch) -> None:
    if not session_id or last_turn_epoch is None:
        return
    seen = _load_seen()
    seen[session_id] = {
        "lastSeenEpoch": float(last_turn_epoch),
        "lastSeenAt": time.time(),
    }
    _save_seen(seen)


def _mark_sessions_read(session_epoch_pairs: list) -> None:
    """Batch-mark many sessions read in one file write."""
    if not session_epoch_pairs:
        return
    seen = _load_seen()
    now = time.time()
    for sid, epoch in session_epoch_pairs:
        if not sid or epoch is None:
            continue
        seen[sid] = {"lastSeenEpoch": float(epoch), "lastSeenAt": now}
    _save_seen(seen)


# Badge — bento-tile layout: three independent rounded-square "tiles"
# arranged horizontally with small gaps between them. Each tile is its
# own NSVisualEffectView glass surface, giving each bucket equal visual
# weight (matches 2026 dev-tooling design language — Linear, Raycast,
# Arc, Vercel). Inside each tile: a large tinted numeral with a small
# uppercase label below it.
BADGE_STATE_FILE = HOME / ".claude-sessions-status-badge.json"
TILE_SIZE = 56.0
TILE_GAP = 6.0
TILE_CORNER = 12.0
# Tile order on the bento: 3 active buckets only. Dormant sessions are
# surfaced inside the popover (separate section at the bottom) — not on
# the badge, because they're stale by definition and shouldn't compete
# for attention with active work.
TILE_KEYS = ("needs", "working", "ready")
TILE_ICONS = {
    "needs":   "bell.badge.fill",
    "working": "gearshape.2.fill",
    "ready":   "checkmark.seal.fill",
    # `dormant` icon kept around for the popover's section header if
    # we ever surface it there visually; not used on the badge.
    "dormant": "moon.zzz.fill",
}
NUM_TILES = len(TILE_KEYS)
BADGE_WIDTH = TILE_SIZE * NUM_TILES + TILE_GAP * (NUM_TILES - 1)   # 180
BADGE_HEIGHT = TILE_SIZE                                          # 56
DEFAULT_BADGE_ORIGIN = (1200.0, 800.0)

# NSVisualEffectView constants. Hardcoded to avoid PyObjC export drift.
# Popover material is lighter and more "system chrome" feeling than
# HUDWindow — picks up wallpaper tint, reads as floating UI rather than
# a heavy panel.
NSVisualEffectMaterialPopover = 6
NSVisualEffectMaterialHUDWindow = 11
NSVisualEffectBlendingModeBehindWindow = 0
NSVisualEffectStateActive = 1

# NSPopover behavior — Transient = auto-close on click-outside, which is
# the macOS default for popovers anchored to a small target.
NSPopoverBehaviorApplicationDefined = 0
NSPopoverBehaviorTransient = 1
NSPopoverBehaviorSemitransient = 2

# Rect-edge constants for NSPopover.showRelativeToRect_…_preferredEdge_.
NSRectEdgeMinY = 1   # below the anchor
NSRectEdgeMaxY = 3   # above the anchor

# Premium P3 bucket tints. Linear-grade urgency reds / amber / cyan-green.
# Two variants per bucket — light-appearance / dark-appearance.
_P3 = (lambda r, g, b: NSColor.colorWithDisplayP3Red_green_blue_alpha_(
    r / 255.0, g / 255.0, b / 255.0, 1.0,
))
BUCKET_COLOR_LIGHT = {
    "needs":   lambda: _P3(0xFF, 0x3B, 0x4C),
    "working": lambda: _P3(0xF5, 0xA5, 0x24),
    "ready":   lambda: _P3(0x1F, 0xCB, 0x7A),
}
BUCKET_COLOR_DARK = {
    "needs":   lambda: _P3(0xFF, 0x6B, 0x7A),
    "working": lambda: _P3(0xFF, 0xB9, 0x38),
    "ready":   lambda: _P3(0x3D, 0xDC, 0x97),
}


def _is_dark_appearance() -> bool:
    try:
        appearance = NSApp.effectiveAppearance()
        name = appearance.bestMatchFromAppearancesWithNames_(
            ["NSAppearanceNameDarkAqua", "NSAppearanceNameAqua"]
        )
        return name == "NSAppearanceNameDarkAqua"
    except Exception:  # noqa: BLE001
        return True


def _bucket_tint(key: str):
    table = BUCKET_COLOR_DARK if _is_dark_appearance() else BUCKET_COLOR_LIGHT
    fn = table.get(key)
    return fn() if fn else NSColor.tertiaryLabelColor()


def _draw_sf_symbol_centered(name: str, center_x: float, center_y: float,
                              point_size: float, color):
    """Draw an SF Symbol centered at (center_x, center_y), tinted with
    `color`, at the given point size. Falls back to a smaller set of
    AppKit features on older macOS (11), and silently no-ops if the
    system doesn't have the symbol at all."""
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if img is None:
        return
    # Step 1 (universal — macOS 11+): size + weight + scale config.
    size_cfg = None
    try:
        size_cfg = NSImageSymbolConfiguration.configurationWithPointSize_weight_scale_(
            point_size, NSFontWeightSemibold, 2,  # scale=Medium
        )
    except Exception:  # noqa: BLE001
        size_cfg = None
    # Step 2 (macOS 12+): hierarchical-color tint. If unavailable,
    # apply the size config alone — the icon ends up the system
    # foreground color but at the correct size, which is preferable to
    # an oversized default-rendered symbol.
    final_cfg = None
    try:
        color_cfg = NSImageSymbolConfiguration.configurationWithHierarchicalColor_(color)
        if size_cfg is not None and color_cfg is not None:
            final_cfg = size_cfg.configurationByApplyingConfiguration_(color_cfg)
        else:
            final_cfg = color_cfg or size_cfg
    except Exception:  # noqa: BLE001
        final_cfg = size_cfg
    if final_cfg is not None:
        try:
            img = img.imageWithSymbolConfiguration_(final_cfg)
        except Exception:  # noqa: BLE001
            pass
    sz = img.size()
    rect = NSMakeRect(
        center_x - sz.width / 2.0,
        center_y - sz.height / 2.0,
        sz.width, sz.height,
    )
    # Use the long form with respectFlipped=True so the bitmap renders
    # right-side-up in a flipped view (BadgeView.isFlipped() == True).
    # Without this, NSImage.drawInRect_… would render the SF Symbol
    # mirrored vertically because the image's bitmap is in unflipped
    # (origin-at-bottom) coordinates. NSCompositingOperationSourceOver = 2.
    img.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
        rect, NSMakeRect(0, 0, 0, 0), 2, 1.0, True, None,
    )


def _rounded_tabular_font(size: float, weight: float):
    """SF Rounded with tabular (monospaced) digit figures. Tabular figures
    keep the count numerals aligned when they change width (1 → 10)."""
    base = NSFont.systemFontOfSize_weight_(size, weight)
    desc = base.fontDescriptor()
    # Switch to the system "Rounded" design where supported.
    try:
        rounded = desc.fontDescriptorWithDesign_("NSCTFontUIFontDesignRounded")
        if rounded is not None:
            desc = rounded
    except Exception:  # noqa: BLE001
        pass
    # Apply monospaced-numerals feature: type=6 (kNumberSpacing),
    # selector=0 (kMonospacedNumbersSelector).
    try:
        desc = desc.fontDescriptorByAddingAttributes_({
            "NSFontFeatureSettings": [{
                "NSFontFeatureTypeIdentifier": 6,
                "NSFontFeatureSelectorIdentifier": 0,
            }]
        })
    except Exception:  # noqa: BLE001
        pass
    font = NSFont.fontWithDescriptor_size_(desc, size)
    return font or base


# Bucket presentation — kept in sync with menubar.py.
LABELS = {
    "needs":   "NEEDS YOU",
    "working": "WORKING",
    "ready":   "FINISHED",
    "dormant": "DORMANT",
}
COLOR_HEX = {
    "needs":   "#ff5555",
    "working": "#d29922",
    "ready":   "#3fb950",
    "dormant": "#6e7681",
}
BUCKET_ORDER = ("needs", "working", "ready", "dormant")


# ---------- Helpers ----------
def _hex_to_nscolor(h: str):
    h = h.lstrip("#")
    r = int(h[0:2], 16) / 255
    g = int(h[2:4], 16) / 255
    b = int(h[4:6], 16) / 255
    return NSColor.colorWithRed_green_blue_alpha_(r, g, b, 1.0)


def _load_frame(default_frame: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    if not WINDOW_STATE_FILE.exists():
        return default_frame
    try:
        data = json.loads(WINDOW_STATE_FILE.read_text(encoding="utf-8"))
        return (
            float(data.get("x", default_frame[0])),
            float(data.get("y", default_frame[1])),
            float(data.get("w", default_frame[2])),
            float(data.get("h", default_frame[3])),
        )
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return default_frame


def _save_frame(x: float, y: float, w: float, h: float) -> None:
    _atomic_write_text(
        WINDOW_STATE_FILE, json.dumps({"x": x, "y": y, "w": w, "h": h}),
    )


def _read_mode() -> str:
    try:
        v = MODE_FILE.read_text(encoding="utf-8").strip()
        if v in ("list", "kanban"):
            return v
    except OSError:
        pass
    return "list"


def _write_mode(mode: str) -> None:
    _atomic_write_text(MODE_FILE, mode)


# ---------- Live-session host detection ----------
# Each running `claude` CLI process registers itself by writing
# `~/.claude/sessions/<pid>.json` with its sessionId + cwd + entrypoint.
# We use that to find where (if anywhere) a session is currently
# running, so resume clicks can focus the existing host instead of
# duplicating it.
_CLAUDE_PROCESS_SESSIONS_DIR = Path(os.path.expanduser("~/.claude/sessions"))


# Terminal emulators we know how to bring forward by app name. Keys are
# substrings to match in `ps -o comm=` output for each emulator's
# process; values are the canonical macOS app name passed to `open -a`.
_KNOWN_TERMINAL_PROCS = {
    "Terminal":   "Terminal",
    "iTerm":      "iTerm",
    "iTerm2":     "iTerm",
    "Ghostty":    "Ghostty",
    "Alacritty":  "Alacritty",
    "WezTerm":    "WezTerm",
    "kitty":      "kitty",
    "Hyper":      "Hyper",
    "tabby":      "Tabby",
}


def _find_terminal_ancestor(pid: int) -> str | None:
    """Walk the parent-process chain from `pid` upward looking for a
    known terminal emulator. Returns the macOS app name to pass to
    `open -a`, or None if no terminal was found in the chain."""
    seen: set[int] = set()
    cur = pid
    while cur > 1 and cur not in seen:
        seen.add(cur)
        try:
            r = subprocess.run(
                ["ps", "-p", str(cur), "-o", "comm=,ppid="],
                capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        line = r.stdout.strip()
        if not line:
            return None
        parts = line.rsplit(None, 1)
        if len(parts) != 2:
            return None
        comm, ppid_str = parts
        for needle, app in _KNOWN_TERMINAL_PROCS.items():
            if needle in comm:
                return app
        try:
            cur = int(ppid_str)
        except ValueError:
            return None
    return None


def _tty_for_pid(pid: int) -> str:
    """Return the TTY of `pid` as `ps -o tty=` reports it (e.g. 'ttys001'
    or '' if no controlling tty)."""
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "tty="],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip()


def _find_live_session_host(session_id: str) -> dict | None:
    """Walk ~/.claude/sessions/*.json looking for a live process whose
    `sessionId` matches. Returns:
      {kind: "claude-desktop", pid: int}
        — the session is running inside the Claude Desktop app
      {kind: "terminal", pid: int, tty: str, terminal_app: str}
        — the session is running in a terminal emulator
      None
        — no live host for this session"""
    if not session_id or not _CLAUDE_PROCESS_SESSIONS_DIR.exists():
        return None
    try:
        files = list(_CLAUDE_PROCESS_SESSIONS_DIR.glob("*.json"))
    except OSError:
        return None
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("sessionId") != session_id:
            continue
        pid = data.get("pid")
        if not isinstance(pid, int):
            continue
        # Liveness probe — skip stale registrations.
        try:
            os.kill(pid, 0)
        except OSError:
            continue
        entrypoint = data.get("entrypoint", "")
        if entrypoint == "claude-desktop":
            return {"kind": "claude-desktop", "pid": pid}
        # Treat anything else (cli, interactive, missing) as terminal.
        return {
            "kind": "terminal",
            "pid": pid,
            "tty": _tty_for_pid(pid),
            "terminal_app": _find_terminal_ancestor(pid),
        }
    return None


def _focus_terminal_tab_for_tty(tty: str) -> bool:
    """Use AppleScript to find the Terminal.app tab whose `tty` matches
    and bring it to the front. `tty` here is `ps -o tty=` output like
    'ttys001' — we prepend '/dev/' to match what Terminal reports.
    Returns True iff a matching tab was focused."""
    if not tty:
        return False
    tty_path = f"/dev/{tty}"
    script = f'''
tell application "Terminal"
    activate
    repeat with w in windows
        try
            repeat with t in tabs of w
                if (tty of t as string) is "{tty_path}" then
                    set selected of t to true
                    set frontmost of w to true
                    return "found"
                end if
            end repeat
        end try
    end repeat
    return "not-found"
end tell
'''
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "found" in (r.stdout or "")


# ---------- Session aggregation ----------
# `classify` is imported from dashboard.py so menubar.py and floating.py
# share a single source of truth. The wrapper preserves the old `_classify`
# name in case anything else in this module imports it by that name.
_classify = classify


def _get_buckets() -> dict[str, list[dict]]:
    """Return {bucket_key: [enriched session rows]}."""
    sessions = recent_sessions(find_sessions())
    now = time.time()
    desktop_idx = desktop_titles()
    live_ids = live_session_ids()
    seen = _load_seen()

    buckets: dict[str, list[dict]] = {b: [] for b in BUCKET_ORDER}
    for s in sessions:
        full_path = s.get("fullPath") or ""
        meta = transcript_meta(full_path)
        last_epoch = meta.get("lastTurnEpoch")
        if last_epoch is None:
            last_epoch = s.get("fileMtime", 0) / 1000
        ago_s = now - last_epoch
        emoji, phase_label = infer_phase(meta)
        state, _ = state_for(meta, ago_s)
        active_bucket = _classify(state, phase_label)
        sid = s.get("sessionId") or ""
        bucket = "dormant" if is_dormant(sid, ago_s, live_ids, active_bucket) else active_bucket
        subs = subagents_for_session(full_path, now)
        sub_sum = subagent_summary(subs)
        # Promote to WORKING when any sub-agent is actively running, even
        # if the parent transcript looks idle or dormant. Mirrors the
        # override in dashboard.py:_prepare_row.
        if sub_sum.get("running"):
            bucket = "working"
            state = "Working…"
        row = {
            "s": s,
            "meta": meta,
            "ago_s": ago_s,
            "emoji": emoji,
            "phase_label": phase_label,
            "state": state,
            "bucket": bucket,
            "title": resolve_title(s, meta, desktop_idx),
            "gist": session_gist(s, meta, bucket),
            "hint": next_action(phase_label, meta.get("lastRole"), ago_s),
            # Inbox-style unread flag — true when last turn happened
            # after the last time the user marked this session as read.
            "unread": _is_session_unread(sid, last_epoch, seen),
            # Carry the resolved epoch so mark-as-read can persist it
            # without re-deriving from meta.
            "lastTurnEpoch": last_epoch,
            # Sub-agents (Task-spawned children). The renderers only
            # surface the actively-running ones; done/interrupted are
            # carried in `subagents` but not displayed.
            "subagents": subs,
            "subagent_summary": sub_sum,
        }
        buckets[bucket].append(row)
    return buckets


# ---------- Attributed-string builders ----------
def _font_label_color():
    """The system label color, which adapts to light/dark mode."""
    return NSColor.labelColor()


def _attr(text: str, font, color=None, link: str | None = None) -> NSAttributedString:
    attrs = {NSFontAttributeName: font}
    if color is not None:
        attrs[NSForegroundColorAttributeName] = color
    if link:
        attrs[NSLinkAttributeName] = NSURL.fileURLWithPath_(link)
    return NSAttributedString.alloc().initWithString_attributes_(text, attrs)


def _build_session_block(row: dict, bucket_color, *, indent: str = "") -> NSAttributedString:
    """Build the attributed-string fragment for one session row."""
    bold = NSFont.boldSystemFontOfSize_(13)
    small = NSFont.systemFontOfSize_(11)
    tiny = NSFont.systemFontOfSize_(10)
    dim = NSColor.secondaryLabelColor()

    out = NSMutableAttributedString.alloc().init()

    # Title (clickable as file:// link if cwd is known).
    cwd = row["meta"].get("cwd") or ""
    title_text = f"{indent}{row['title']}\n"
    out.appendAttributedString_(
        _attr(title_text, bold, _font_label_color(), link=cwd or None)
    )

    # Status line: phase-emoji  Phase  ·  Gist  ·  age
    bits = []
    if row["phase_label"]:
        bits.append(row["phase_label"])
    if row.get("gist"):
        bits.append(row["gist"])
    bits.append(format_ago(row["ago_s"]))
    emoji_part = f"{row['emoji']} " if row["emoji"] else ""
    status_text = f"{indent}   {emoji_part}{'  ·  '.join(bits)}\n"
    out.appendAttributedString_(_attr(status_text, small, bucket_color))

    # Folder (dim).
    if cwd:
        abbrev = cwd.replace(os.path.expanduser("~"), "~")
        out.appendAttributedString_(
            _attr(f"{indent}   {abbrev}\n", tiny, dim, link=cwd)
        )

    # Snippet only for NEEDS YOU / FINISHED (where literal content matters).
    if row["bucket"] in ("needs", "ready"):
        snippet = (row["meta"].get("lastAction") or "").replace("\n", " ").strip()
        if snippet:
            if len(snippet) > 110:
                snippet = snippet[:109] + "…"
            out.appendAttributedString_(
                _attr(f"{indent}   ↳ {snippet}\n", tiny, dim)
            )

    # One teal line per actively-running sub-agent below the card —
    # "◐ <type> · <description>" using the .meta.json description as
    # a brief. Done / interrupted children are hidden.
    subs = row.get("subagents") or []
    running_subs = [s for s in subs if s.get("state") == SUBAGENT_RUNNING]
    if running_subs:
        teal = (
            NSColor.systemTealColor()
            if hasattr(NSColor, "systemTealColor") else bucket_color
        )
        shown = running_subs[:SUBAGENT_MAX_DISPLAY]
        for sub in shown:
            atype = (sub.get("agent_type") or "").strip()
            desc = (sub.get("name") or "agent").strip()
            if len(desc) > 100:
                desc = desc[:99] + "…"
            line = f"{indent}   ◐ {atype} · {desc}\n" if atype else f"{indent}   ◐ {desc}\n"
            out.appendAttributedString_(_attr(line, tiny, teal))
        extra = len(running_subs) - len(shown)
        if extra > 0:
            out.appendAttributedString_(
                _attr(f"{indent}   + {extra} more working\n", tiny, dim)
            )

    return out


def _build_dormant_line(row: dict, bucket_color) -> NSAttributedString:
    """Single compact line for dormant rows."""
    tiny = NSFont.systemFontOfSize_(11)
    cwd = row["meta"].get("cwd") or ""
    abbrev = cwd.replace(os.path.expanduser("~"), "~") if cwd else ""
    parts = [row["title"]]
    if abbrev:
        parts.append(abbrev)
    parts.append(format_ago(row["ago_s"]))
    text = "  ·  ".join(parts) + "\n"
    return _attr(text, tiny, bucket_color, link=cwd or None)


def build_list_content(buckets: dict[str, list[dict]]) -> NSAttributedString:
    """Vertical list — same shape as the menu dropdown."""
    sys_font = NSFont.systemFontOfSize_(12)
    out = NSMutableAttributedString.alloc().init()

    first = True
    for key in BUCKET_ORDER:
        rows = buckets[key]
        if not rows:
            continue
        if not first:
            out.appendAttributedString_(_attr("\n", sys_font))
        first = False

        # Group header
        color = _hex_to_nscolor(COLOR_HEX[key])
        out.appendAttributedString_(
            _attr(
                f"{LABELS[key]}  ·  {len(rows)}\n",
                NSFont.boldSystemFontOfSize_(11),
                color,
            )
        )

        # Rows
        for row in rows:
            if key == "dormant":
                out.appendAttributedString_(_build_dormant_line(row, color))
            else:
                out.appendAttributedString_(_build_session_block(row, color))
                out.appendAttributedString_(_attr("\n", sys_font))

    if first:  # nothing rendered
        out.appendAttributedString_(
            _attr(
                "No active sessions in the last 24h.\n",
                sys_font,
                NSColor.secondaryLabelColor(),
            )
        )
    return out


def build_column_content(bucket_key: str, rows: list[dict]) -> NSAttributedString:
    """Per-column attributed string for kanban mode: one column header
    (e.g. 'NEEDS YOU  ·  3') plus its session rows. Dormant rows render
    as the compact one-liner used in list mode."""
    color = _hex_to_nscolor(COLOR_HEX[bucket_key])
    header_font = NSFont.boldSystemFontOfSize_(11)
    sys_font = NSFont.systemFontOfSize_(12)
    dim = NSColor.secondaryLabelColor()

    out = NSMutableAttributedString.alloc().init()
    out.appendAttributedString_(
        _attr(f"{LABELS[bucket_key]}  ·  {len(rows)}\n", header_font, color)
    )
    if not rows:
        out.appendAttributedString_(_attr("—\n", sys_font, dim))
        return out
    for row in rows:
        if bucket_key == "dormant":
            out.appendAttributedString_(_build_dormant_line(row, color))
        else:
            out.appendAttributedString_(_build_session_block(row, color))
            out.appendAttributedString_(_attr("\n", sys_font))
    return out


# ---------- Panel controller ----------
class PanelController(NSObject):
    panel = objc.ivar("panel")
    timer = objc.ivar("timer")
    mode = objc.ivar("mode")
    # List mode owns one scroll + text view; kanban mode owns three
    # scroll+text views laid out side-by-side via NSStackView. Only one
    # set is in the contentView at a time.
    list_scroll = objc.ivar("list_scroll")
    list_text_view = objc.ivar("list_text_view")
    kanban_stack = objc.ivar("kanban_stack")
    kanban_text_views = objc.ivar("kanban_text_views")  # list[NSTextView]

    def initWithMode_(self, mode: str):
        self = objc.super(PanelController, self).init()
        if self is None:
            return None
        self.mode = mode if mode in ("list", "kanban") else "list"

        # Pick a frame default appropriate to the layout the first time.
        default = DEFAULT_FRAME_KANBAN if self.mode == "kanban" else DEFAULT_FRAME_LIST
        frame = _load_frame(default)
        x, y, w, h = frame
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskResizable
            | NSWindowStyleMaskMiniaturizable
        )
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, w, h), style, NSBackingStoreBuffered, False
        )
        self.panel.setTitle_("Claude Sessions")
        self.panel.setLevel_(NSFloatingWindowLevel)
        self.panel.setReleasedWhenClosed_(False)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setDelegate_(self)

        # Build both layouts upfront so toggling is just a subview swap.
        self._build_list_view()
        self._build_kanban_view()
        self._install_current_layout()

        self._install_app_menu()

        # First render + timer
        self.refresh_(None)
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            REFRESH_SECS, self, "refresh:", None, True
        )
        return self

    @objc.python_method
    def _make_text_view(self) -> tuple:
        """Returns (scroll, text_view) pair for one column / the list view."""
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, 200, 200))
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(False)
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        scroll.setBorderType_(0)

        text_view = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 200, 200))
        text_view.setEditable_(False)
        text_view.setSelectable_(True)
        text_view.setRichText_(True)
        text_view.setHorizontallyResizable_(False)
        text_view.setAutoresizingMask_(NSViewWidthSizable)
        text_view.textContainer().setWidthTracksTextView_(True)
        text_view.setBackgroundColor_(NSColor.controlBackgroundColor())
        text_view.setTextContainerInset_(NSMakeSize(12, 12))
        scroll.setDocumentView_(text_view)
        return scroll, text_view

    @objc.python_method
    def _build_list_view(self):
        scroll, tv = self._make_text_view()
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.list_scroll = scroll
        self.list_text_view = tv

    @objc.python_method
    def _build_kanban_view(self):
        """NSStackView (horizontal) with three NSScrollViews — one per
        active bucket. Dormant rows tack on at the bottom of the right
        column to keep them visible without taking screen real-estate."""
        stack = NSStackView.alloc().init()
        stack.setOrientation_(NS_USER_INTERFACE_LAYOUT_ORIENTATION_HORIZONTAL)
        stack.setSpacing_(8.0)
        stack.setDistribution_(NS_STACK_VIEW_DISTRIBUTION_FILL_EQUALLY)
        # NSEdgeInsets is a 4-tuple: (top, left, bottom, right).
        stack.setEdgeInsets_((8.0, 8.0, 8.0, 8.0))
        stack.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)

        tvs: list = []
        for _ in range(3):
            scroll, tv = self._make_text_view()
            stack.addArrangedSubview_(scroll)
            tvs.append(tv)
        self.kanban_stack = stack
        self.kanban_text_views = tvs

    @objc.python_method
    def _install_current_layout(self):
        """Swap whichever subview is currently in the content view."""
        content = self.panel.contentView()
        for sub in list(content.subviews()):
            sub.removeFromSuperview()
        if self.mode == "kanban":
            self.kanban_stack.setFrame_(content.bounds())
            content.addSubview_(self.kanban_stack)
        else:
            self.list_scroll.setFrame_(content.bounds())
            content.addSubview_(self.list_scroll)

    def _install_app_menu(self):
        """A tiny app menu so cmd-Q quits, cmd-shift-K toggles kanban,
        cmd-W closes the window. Without this, even cmd-Q doesn't quit
        an LSUIElement-style app."""
        main = NSMenu.alloc().init()
        app_item = NSMenuItem.alloc().init()
        main.addItem_(app_item)
        app_menu = NSMenu.alloc().init()
        # Toggle kanban
        toggle = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Toggle Kanban / List", "toggleMode:", "k"
        )
        toggle.setKeyEquivalentModifierMask_(
            (1 << 17) | (1 << 20)  # NSEventModifierFlagShift | NSEventModifierFlagCommand
        )
        toggle.setTarget_(self)
        app_menu.addItem_(toggle)
        app_menu.addItem_(NSMenuItem.separatorItem())
        # Quit
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit", "terminate:", "q"
        )
        app_menu.addItem_(quit_item)
        app_item.setSubmenu_(app_menu)
        NSApp.setMainMenu_(main)

    @objc.python_method
    def _render(self):
        try:
            buckets = _get_buckets()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[floating] buckets failed: {e!r}\n")
            return
        try:
            if self.mode == "kanban":
                # Each of NEEDS YOU / WORKING / FINISHED gets its own
                # column. Dormant rows are appended below the right
                # column so they stay visible but de-emphasized.
                cols = ("needs", "working", "ready")
                for tv, key in zip(self.kanban_text_views, cols):
                    content = build_column_content(key, buckets[key])
                    if key == "ready" and buckets["dormant"]:
                        # Tuck dormant rows under the FINISHED column.
                        full = NSMutableAttributedString.alloc().initWithAttributedString_(content)
                        full.appendAttributedString_(_attr("\n", NSFont.systemFontOfSize_(12)))
                        full.appendAttributedString_(
                            build_column_content("dormant", buckets["dormant"])
                        )
                        tv.textStorage().setAttributedString_(full)
                    else:
                        tv.textStorage().setAttributedString_(content)
            else:
                mas = build_list_content(buckets)
                self.list_text_view.textStorage().setAttributedString_(mas)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[floating] render failed: {e!r}\n")

    def refresh_(self, _sender):
        # Check for the quit flag (set by `panel --quit`). NSApp.terminate_
        # routes through windowWillClose_ which saves the frame and unlinks
        # the pidfile.
        if QUIT_FLAG.exists():
            try:
                QUIT_FLAG.unlink(missing_ok=True)
            except OSError:
                pass
            self.panel.close()
            return
        self._render()

    def toggleMode_(self, _sender):
        self.mode = "list" if self.mode == "kanban" else "kanban"
        _write_mode(self.mode)
        # If the user hasn't custom-sized the window (we're at the old
        # default), grow/shrink it to the new mode's default size.
        frame = self.panel.frame()
        cur_w = float(frame.size.width)
        cur_h = float(frame.size.height)
        old_default = DEFAULT_FRAME_KANBAN if self.mode == "list" else DEFAULT_FRAME_LIST
        new_default = DEFAULT_FRAME_KANBAN if self.mode == "kanban" else DEFAULT_FRAME_LIST
        if abs(cur_w - old_default[2]) < 10 and abs(cur_h - old_default[3]) < 10:
            self.panel.setFrame_display_animate_(
                NSMakeRect(frame.origin.x, frame.origin.y, new_default[2], new_default[3]),
                True, True,
            )
        self._install_current_layout()
        self._render()

    # ---- NSWindowDelegate ----
    @objc.python_method
    def _save_current_frame(self):
        frame = self.panel.frame()
        _save_frame(
            frame.origin.x, frame.origin.y,
            frame.size.width, frame.size.height,
        )

    def windowDidMove_(self, _notification):
        # Saving on every move keeps the state file fresh even if the
        # process is killed by SIGTERM (which doesn't fire windowWillClose).
        self._save_current_frame()

    def windowDidResize_(self, _notification):
        self._save_current_frame()

    def windowWillClose_(self, _notification):
        self._save_current_frame()
        # NSTimer holds a strong reference to its target — invalidate it
        # so the controller can be released cleanly if the app ever
        # intercepts terminate_ in the future.
        if self.timer is not None:
            try:
                self.timer.invalidate()
            except Exception:  # noqa: BLE001
                pass
            self.timer = None
        try:
            PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        NSApp.terminate_(None)


# ---------- Badge (small floating icon) ----------
def _load_badge_origin() -> tuple[float, float]:
    if not BADGE_STATE_FILE.exists():
        return DEFAULT_BADGE_ORIGIN
    try:
        data = json.loads(BADGE_STATE_FILE.read_text(encoding="utf-8"))
        return float(data["x"]), float(data["y"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return DEFAULT_BADGE_ORIGIN


def _save_badge_origin(x: float, y: float) -> None:
    _atomic_write_text(BADGE_STATE_FILE, json.dumps({"x": x, "y": y}))


class BadgeView(NSView):
    """Modern glassmorphic badge — drawn on TOP of an NSVisualEffectView
    that provides the live frosted-glass background. This view only paints
    the foreground: glowing accent dots, count numerals, and a hairline
    inner-border ring. Clickable to toggle the panel; draggable anywhere
    on screen."""

    counts = objc.ivar("counts")           # dict[str, int]
    controller = objc.ivar("controller")   # BadgeController weak ref
    drag_anchor = objc.ivar("drag_anchor") # (mx, my, wx, wy) or None
    did_drag = objc.ivar("did_drag")       # bool

    def initWithFrame_(self, frame):
        self = objc.super(BadgeView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.counts = {"needs": 0, "working": 0, "ready": 0, "dormant": 0}
        self.drag_anchor = None
        self.did_drag = False
        # Accessibility: surface this widget to VoiceOver as a labeled
        # button. The dynamic count breakdown is exposed via the
        # accessibilityValue (refreshed in set_counts).
        try:
            self.setAccessibilityElement_(True)
            self.setAccessibilityRole_("AXButton")
            self.setAccessibilityLabel_("Claude Sessions Status")
            self.setAccessibilityValue_(self._accessibility_value_text())
        except Exception:  # noqa: BLE001
            pass
        return self

    @objc.python_method
    def _accessibility_value_text(self) -> str:
        """Human-readable per-bucket count string for VoiceOver."""
        c = self.counts or {}
        return (
            f"{int(c.get('needs', 0) or 0)} need you, "
            f"{int(c.get('working', 0) or 0)} working, "
            f"{int(c.get('ready', 0) or 0)} finished"
        )

    @objc.python_method
    def set_counts(self, counts: dict) -> None:
        self.counts = counts
        try:
            self.setAccessibilityValue_(self._accessibility_value_text())
        except Exception:  # noqa: BLE001
            pass
        self.setNeedsDisplay_(True)

    @objc.python_method
    def set_controller(self, ctrl) -> None:
        self.controller = ctrl

    def isFlipped(self):
        # Flipped coordinates make manual layout easier (origin top-left).
        return True

    def drawRect_(self, _dirty):
        # Per-tile content: SF Symbol icon at top, big numeral below.
        # The NSVisualEffectView behind each tile provides the glass.
        num_font = _rounded_tabular_font(20.0, NSFontWeightSemibold)

        for i, key in enumerate(TILE_KEYS):
            n = int(self.counts.get(key, 0) or 0)
            tile_x = i * (TILE_SIZE + TILE_GAP)
            tile_cx = tile_x + TILE_SIZE / 2.0

            # Dormant tile is ALWAYS muted (gray) — its presence on the
            # bento should be calm, not competing with active counts.
            if key == "dormant":
                num_color = NSColor.tertiaryLabelColor()
                icon_color = NSColor.tertiaryLabelColor()
            elif n > 0:
                num_color = _bucket_tint(key)
                icon_color = num_color
            else:
                num_color = NSColor.tertiaryLabelColor()
                icon_color = NSColor.tertiaryLabelColor().colorWithAlphaComponent_(0.6)

            # Numeral — big, tinted, tabular figures, slightly tightened.
            num_s = NSAttributedString.alloc().initWithString_attributes_(
                str(n),
                {
                    NSFontAttributeName: num_font,
                    NSForegroundColorAttributeName: num_color,
                    NSKernAttributeName: -0.4,
                },
            )
            num_sz = num_s.size()

            # Vertically center an [icon, 2pt gap, numeral] block.
            icon_pt = 14.0  # SF Symbol point size
            gap = 2.0
            block_h = icon_pt + gap + num_sz.height
            top = (TILE_SIZE - block_h) / 2.0

            # SF Symbol — drawn centered horizontally on the tile,
            # at the top of the block. The drawing helper centers it
            # on the given (cx, cy) point.
            icon_cy = top + icon_pt / 2.0
            _draw_sf_symbol_centered(
                TILE_ICONS[key], tile_cx, icon_cy, icon_pt, icon_color,
            )

            # Numeral below the icon.
            num_x = tile_cx - num_sz.width / 2.0
            num_y = top + icon_pt + gap
            num_s.drawAtPoint_(NSMakePoint(num_x, num_y))

    # ---- Mouse handling: click toggles the panel, drag moves the window ----
    def mouseDown_(self, event):
        win = self.window()
        if win is None:
            return
        mouse = NSEvent.mouseLocation()
        wf = win.frame()
        self.drag_anchor = (mouse.x, mouse.y, wf.origin.x, wf.origin.y)
        self.did_drag = False

    def mouseDragged_(self, event):
        if self.drag_anchor is None:
            return
        mx0, my0, wx0, wy0 = self.drag_anchor
        mouse = NSEvent.mouseLocation()
        dx = mouse.x - mx0
        dy = mouse.y - my0
        if not self.did_drag and (abs(dx) > 3 or abs(dy) > 3):
            self.did_drag = True
        if self.did_drag:
            self.window().setFrameOrigin_(NSMakePoint(wx0 + dx, wy0 + dy))

    def mouseUp_(self, event):
        was_drag = self.did_drag
        self.drag_anchor = None
        self.did_drag = False
        if was_drag:
            # Persist the new badge position.
            f = self.window().frame()
            _save_badge_origin(float(f.origin.x), float(f.origin.y))
            return
        # Pure click — toggle the detail panel.
        if self.controller is not None:
            self.controller.togglePanel_(None)


# ---------- Popover content (session rows) ----------
class _SeparatorRow(NSView):
    """1pt hairline separator drawn directly. NSStackView fills width
    automatically because we set autoresizing + an intrinsic content size."""

    def intrinsicContentSize(self):
        return NSMakeSize(-1, 1)  # auto width, fixed 1pt height

    def drawRect_(self, _dirty):
        b = self.bounds()
        # Inset 16pt from the leading edge for the "card-less" macOS look.
        inset_x = 16.0
        line_rect = NSMakeRect(inset_x, 0.0, max(0.0, b.size.width - inset_x), 1.0)
        NSColor.separatorColor().colorWithAlphaComponent_(0.6).setFill()
        NSBezierPath.fillRect_(line_rect)


class _SessionRowView(NSView):
    """A single session row inside the popover: a 3pt-wide colored leading
    bar, then title / gist / meta stacked vertically. Plain — no card
    container (cards on translucent material read as double-containers)."""

    row_data = objc.ivar("row_data")
    bucket_color = objc.ivar("bucket_color")

    def initWithRow_color_(self, row: dict, color):
        self = objc.super(_SessionRowView, self).initWithFrame_(
            NSMakeRect(0, 0, 360, 64)
        )
        if self is None:
            return None
        self.row_data = row
        self.bucket_color = color
        return self

    def isFlipped(self):
        return True

    def intrinsicContentSize(self):
        # Auto width via NSStackView; fixed height for the 3-line layout.
        return NSMakeSize(-1, 64)

    def drawRect_(self, _dirty):
        try:
            self._draw_safely()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[_SessionRowView.drawRect_] {e!r}\n")
            import traceback
            traceback.print_exc(file=sys.stderr)

    @objc.python_method
    def _draw_safely(self):
        bounds = self.bounds()
        w = bounds.size.width
        h = bounds.size.height

        # 3pt colored leading bar (full row height minus padding).
        bar_w = 3.0
        bar_x = 12.0
        bar_inset = 6.0
        bar_h = max(0.0, h - bar_inset * 2)
        bar_rect = NSMakeRect(bar_x, bar_inset, bar_w, bar_h)
        bar_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            bar_rect, 1.5, 1.5,
        )
        (self.bucket_color or NSColor.tertiaryLabelColor()).setFill()
        bar_path.fill()

        # Typography hierarchy:
        #   Title  — SF Pro Text Semibold 13pt, labelColor
        #   Gist   — SF Pro Text Regular 12pt, secondaryLabelColor
        #   Meta   — SF Pro Text Regular 11pt mono digits, tertiaryLabelColor
        text_x = bar_x + bar_w + 12.0  # leading inset for text column
        right_pad = 14.0
        avail_w = max(40.0, w - text_x - right_pad)

        title_font = NSFont.systemFontOfSize_weight_(13, NSFontWeightSemibold)
        gist_font = NSFont.systemFontOfSize_(12)
        meta_font = _rounded_tabular_font(11.0, NSFontWeightRegular)

        # Defensive: row_data might be a plain Python dict whose nested
        # access pattern raises; coerce missing keys to defaults.
        row = dict(self.row_data) if self.row_data else {}
        meta = row.get("meta") or {}
        title_text = (row.get("title") or "(untitled)").strip()
        phase = row.get("phase_label") or ""
        gist = row.get("gist") or ""
        ago = format_ago(row.get("ago_s") or 0)

        # Title.
        title_s = NSAttributedString.alloc().initWithString_attributes_(
            title_text,
            {
                NSFontAttributeName: title_font,
                NSForegroundColorAttributeName: NSColor.labelColor(),
            },
        )
        title_s.drawInRect_(NSMakeRect(text_x, 8.0, avail_w, 18.0))

        # Gist (phase prefix + gist body).
        line_parts = []
        if phase:
            line_parts.append(phase)
        if gist:
            line_parts.append(gist)
        gist_line = "  ·  ".join(line_parts) if line_parts else ""
        if gist_line:
            gist_s = NSAttributedString.alloc().initWithString_attributes_(
                gist_line,
                {
                    NSFontAttributeName: gist_font,
                    NSForegroundColorAttributeName: NSColor.secondaryLabelColor(),
                },
            )
            gist_s.drawInRect_(NSMakeRect(text_x, 26.0, avail_w, 16.0))

        # Meta (age + cwd) — bottom row.
        cwd_raw = meta.get("cwd") if isinstance(meta, dict) else None
        cwd = ""
        if isinstance(cwd_raw, str) and cwd_raw:
            cwd = cwd_raw.replace(os.path.expanduser("~"), "~")
        meta_bits = [ago]
        if cwd:
            meta_bits.append(cwd)
        meta_text = "  ·  ".join(meta_bits)
        meta_s = NSAttributedString.alloc().initWithString_attributes_(
            meta_text,
            {
                NSFontAttributeName: meta_font,
                NSForegroundColorAttributeName: NSColor.tertiaryLabelColor(),
            },
        )
        meta_s.drawInRect_(NSMakeRect(text_x, 44.0, avail_w, 14.0))


class _FirstMouseButton(NSButton):
    """NSButton that fires on the first click even when its window
    isn't key — needed for buttons inside NSPopovers, whose window
    isn't key on the click that opened them."""

    def acceptsFirstMouse_(self, _event):
        return True


class PopoverVC(NSViewController):
    """Popover content view controller with a List ↔ Kanban segmented
    control at the top. Choice persists to ~/.claude-sessions-status-popover-mode.

    List mode renders inside one NSTextView (attributed string).
    Kanban mode renders inside a single WKWebView that loads
    scripts/kanban.html and is fed JSON payloads via evaluateJavaScript.
    Clicks bridge back through a WKScriptMessageHandler named "kanban"."""

    mode = objc.ivar("mode")                  # "list" or "kanban"
    segmented = objc.ivar("segmented")
    content_host = objc.ivar("content_host")
    list_scroll = objc.ivar("list_scroll")
    list_text_view = objc.ivar("list_text_view")
    # WKWebView that renders the kanban from scripts/kanban.html — the
    # design-fidelity path. Refresh sends a JSON payload via
    # evaluateJavaScript and JS calls back through WKScriptMessageHandler.
    kanban_web = objc.ivar("kanban_web")
    kanban_web_ready = objc.ivar("kanban_web_ready")
    # Buckets buffered between "JS not loaded yet" and "JS now ready":
    # the WKWebView loads asynchronously, so the first refresh that
    # arrives before document-ready stashes its payload here and we
    # flush it once the page signals it's mounted.
    kanban_web_pending = objc.ivar("kanban_web_pending")
    # Last-seen mtime of scripts/kanban.html — dev convenience that
    # auto-reloads the CSS/JS when the file changes on disk. Set on
    # _build_kanban_web; checked once per refresh and re-loaded if
    # the file has been edited since.
    kanban_html_mtime = objc.ivar("kanban_html_mtime")
    popover_ref = objc.ivar("popover_ref")    # NSPopover, set by BadgeController
    last_rendered_rows = objc.ivar("last_rendered_rows")  # for mark-all-read
    show_dormant = objc.ivar("show_dormant")   # bool — toggle to hide dormant
    dormant_btn = objc.ivar("dormant_btn")     # the NSButton checkbox
    density = objc.ivar("density")             # "glance" | "focus" | "detail"
    density_seg = objc.ivar("density_seg")     # NSSegmentedControl
    mark_all_btn = objc.ivar("mark_all_btn")

    @objc.python_method
    def set_popover(self, popover):
        """Allows BadgeController to hand us the NSPopover so we can
        resize it when the user toggles mode."""
        self.popover_ref = popover

    def loadView(self):
        try:
            self._load_view_safely()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[PopoverVC.loadView] {e!r}\n")
            import traceback
            traceback.print_exc(file=sys.stderr)
            fallback = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 360, 80))
            self.setView_(fallback)

    @objc.python_method
    def _load_view_safely(self):
        # Restore last-used mode + density + show_dormant.
        self.mode = _read_popover_mode()
        self.density = _read_density()
        self.show_dormant = _read_show_dormant()
        # The dormant column adds a 4th lane in kanban → wider popover.
        size = (
            POPOVER_KANBAN_WITH_DORMANT_SIZE
            if (self.mode == "kanban" and self.show_dormant)
            else (POPOVER_KANBAN_SIZE if self.mode == "kanban" else POPOVER_LIST_SIZE)
        )
        w, h = size

        container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        # Opaque solid background — overrides NSPopover's default
        # translucent material so the popover content doesn't show
        # what's behind it. macOS clips this to the popover's
        # rounded-corner + arrow shape automatically.
        container.setWantsLayer_(True)
        c_layer = container.layer()
        if c_layer is not None:
            c_layer.setBackgroundColor_(
                NSColor.windowBackgroundColor().CGColor()
            )

        # ---- Top bar: segmented control (List | Kanban) ----
        # Width is tight enough that it fits in the 360pt list-mode
        # popover without colliding with the density popup (left) or
        # the dormant toggle (right).
        TOP_BAR_HEIGHT = 32.0
        seg = NSSegmentedControl.alloc().init()
        seg.setSegmentCount_(2)
        seg.setLabel_forSegment_("List", 0)
        seg.setLabel_forSegment_("Kanban", 1)
        seg.setSegmentStyle_(NSSegmentStyleAutomatic)
        seg.setTrackingMode_(NSSegmentSwitchTrackingSelectOne)
        seg.setSelectedSegment_(0 if self.mode == "list" else 1)
        seg.setTarget_(self)
        seg.setAction_("segmentChanged:")
        # Center the segmented control horizontally in the top bar.
        seg_w = 130.0
        seg.setFrame_(NSMakeRect((w - seg_w) / 2, h - TOP_BAR_HEIGHT + 4, seg_w, 22))
        seg.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxXMargin | NSViewMinYMargin)
        container.addSubview_(seg)
        self.segmented = seg

        # ---- Top bar (left): density popup (Glance / Focus / Detail) ----
        # A compact NSPopUpButton instead of a segmented control. The
        # segmented version was 180pt wide and overlapped the centered
        # List/Kanban control in 360pt list mode. The popup is ~90pt
        # and shows the current selection inline ("Focus ▾").
        density_pop = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(12, h - TOP_BAR_HEIGHT + 4, 96, 22),
            False,
        )
        density_pop.addItemsWithTitles_(["Glance", "Focus", "Detail"])
        density_pop.selectItemAtIndex_(DENSITIES.index(self.density))
        density_pop.setTarget_(self)
        density_pop.setAction_("densityChanged:")
        density_pop.setAutoresizingMask_(NSViewMaxXMargin | NSViewMinYMargin)
        container.addSubview_(density_pop)
        self.density_seg = density_pop

        # ---- Top bar (right): "Show dormant" checkbox toggle ----
        # Lives in the right corner so it doesn't compete with the
        # primary List/Kanban control. self.show_dormant is set in the
        # _load_view_safely preamble so the initial popover size can
        # depend on it; here we just build the UI for the toggle.
        dormant_btn = NSButton.alloc().init()
        dormant_btn.setButtonType_(3)        # NSButtonTypeSwitch (checkbox)
        dormant_btn.setTitle_("Show older")
        dormant_btn.setState_(1 if self.show_dormant else 0)
        dormant_btn.setTarget_(self)
        dormant_btn.setAction_("toggleDormant:")
        dormant_btn.sizeToFit()
        dbf = dormant_btn.frame()
        dormant_btn.setFrame_(NSMakeRect(
            w - dbf.size.width - 12,
            h - TOP_BAR_HEIGHT + 6,
            dbf.size.width, dbf.size.height,
        ))
        # Stick to the right edge as the popover resizes.
        dormant_btn.setAutoresizingMask_(NSViewMinXMargin | NSViewMinYMargin)
        container.addSubview_(dormant_btn)
        self.dormant_btn = dormant_btn

        # ---- Top bar (right, kanban only): "Mark all N read" button ----
        # Hidden in list mode (which already has an inline footer link
        # inside the NSTextView) and when there are zero unreads. We
        # set the title + final frame on each refresh because the
        # number changes.
        mark_btn = _FirstMouseButton.alloc().init()
        mark_btn.setBordered_(False)
        mark_btn.setFont_(NSFont.systemFontOfSize_(12))
        mark_btn.setTarget_(self)
        mark_btn.setAction_("markAllReadClicked:")
        mark_btn.setTitle_("")
        mark_btn.setHidden_(True)
        mark_btn.setContentTintColor_(
            NSColor.controlAccentColor()
            if hasattr(NSColor, "controlAccentColor") else NSColor.labelColor()
        )
        # Slot it to the LEFT of the dormant checkbox. Stays right-aligned
        # as the popover resizes.
        mark_btn.setFrame_(NSMakeRect(
            w - dbf.size.width - 24,  # placeholder; finalized in refresh
            h - TOP_BAR_HEIGHT + 6,
            0, dbf.size.height,
        ))
        mark_btn.setAutoresizingMask_(NSViewMinXMargin | NSViewMinYMargin)
        container.addSubview_(mark_btn)
        self.mark_all_btn = mark_btn

        # ---- Content host below top bar ----
        host = NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, w, h - TOP_BAR_HEIGHT)
        )
        host.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        container.addSubview_(host)
        self.content_host = host

        # Build BOTH layouts upfront; only the current one is in the host.
        self._build_list_views()
        self._build_kanban_web()
        self._install_layout()

        self.setView_(container)

    @objc.python_method
    def _build_list_views(self):
        scroll = NSScrollView.alloc().initWithFrame_(self.content_host.bounds())
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(0)
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        scroll.setDrawsBackground_(False)

        tv = NSTextView.alloc().initWithFrame_(self.content_host.bounds())
        tv.setEditable_(False)
        tv.setSelectable_(True)
        tv.setRichText_(True)
        tv.setHorizontallyResizable_(False)
        tv.setVerticallyResizable_(True)
        tv.setAutoresizingMask_(NSViewWidthSizable)
        tv.textContainer().setWidthTracksTextView_(True)
        tv.setDrawsBackground_(False)
        tv.setTextContainerInset_(NSMakeSize(0, 8))

        scroll.setDocumentView_(tv)
        self.list_scroll = scroll
        self.list_text_view = tv

    def dealloc(self):
        """Break the retain cycle WKUserContentController → PopoverVC
        on teardown. PopoverVC currently lives for the lifetime of the
        app under BadgeController so this rarely fires — but on a
        clean quit/restart it keeps the old VC from receiving stale
        messages from a still-live web view in tear-down order."""
        try:
            if self.kanban_web is not None:
                cfg = self.kanban_web.configuration()
                if cfg is not None:
                    cc = cfg.userContentController()
                    if cc is not None:
                        cc.removeScriptMessageHandlerForName_("kanban")
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[PopoverVC.dealloc] cleanup: {e!r}\n")
        objc.super(PopoverVC, self).dealloc()

    @objc.python_method
    def _build_kanban_web(self):
        """Build a single WKWebView that renders the kanban from
        scripts/kanban.html. This replaces the native NSStackView +
        scroll-view-per-column path. Refresh feeds the view a JSON
        payload via evaluateJavaScript; clicks come back through a
        WKScriptMessageHandler named 'kanban'."""
        host_bounds = self.content_host.bounds()
        if host_bounds.size.width < 10:
            host_bounds = NSMakeRect(
                0, 0, POPOVER_KANBAN_SIZE[0],
                POPOVER_KANBAN_SIZE[1] - 32,
            )

        config = WKWebViewConfiguration.alloc().init()
        content_ctrl = WKUserContentController.alloc().init()
        # Bridge JS → Python. JS posts via window.webkit.messageHandlers.kanban.
        content_ctrl.addScriptMessageHandler_name_(self, "kanban")
        config.setUserContentController_(content_ctrl)

        web = WKWebView.alloc().initWithFrame_configuration_(
            host_bounds, config,
        )
        web.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        # Transparent so the popover bg shows through during initial
        # paint instead of a flash of white.
        try:
            web.setValue_forKey_(False, "drawsBackground")
        except Exception:  # noqa: BLE001
            pass
        # Suppress the macOS "rubber band" elastic scroll outside the
        # bounds — feels weird in a panel.
        try:
            web.setAllowsBackForwardNavigationGestures_(False)
        except Exception:  # noqa: BLE001
            pass
        # Reload-on-script-change is nice in dev but we'll skip for
        # now — change the HTML and restart the panel.

        html_path = self._kanban_html_path()
        try:
            html_str = html_path.read_text(encoding="utf-8")
            mtime = html_path.stat().st_mtime
        except OSError as e:
            sys.stderr.write(f"[kanban-web] failed to read {html_path}: {e}\n")
            html_str = "<h1>kanban.html missing</h1>"
            mtime = 0.0
        # baseURL points at the scripts/ dir so any future relative
        # resources (fonts, images) resolve cleanly.
        base_url = NSURL.fileURLWithPath_(str(html_path.parent) + "/")
        web.loadHTMLString_baseURL_(html_str, base_url)

        self.kanban_web = web
        self.kanban_web_ready = False
        self.kanban_web_pending = None
        self.kanban_html_mtime = mtime

    @objc.python_method
    def _kanban_html_path(self) -> Path:
        return Path(__file__).resolve().parent / "kanban.html"

    @objc.python_method
    def _maybe_reload_kanban_html(self):
        """Dev convenience: if kanban.html has been edited on disk
        since we last loaded it, re-read + reload into the WKWebView.
        Called once per refresh; one stat() call per ~5s is cheap and
        makes design iteration feel instant — no badge restart needed."""
        if self.kanban_web is None:
            return
        path = self._kanban_html_path()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        if mtime <= (self.kanban_html_mtime or 0.0):
            return
        try:
            html_str = path.read_text(encoding="utf-8")
        except OSError as e:
            sys.stderr.write(f"[kanban-web] reload read failed: {e}\n")
            return
        self.kanban_html_mtime = mtime
        # Re-loading drops the JS context, so flip ready back to False
        # and let the new page's "ready" handshake flush the next
        # refresh's payload (already buffered into kanban_web_pending
        # by _render_kanban before this method returns).
        self.kanban_web_ready = False
        base_url = NSURL.fileURLWithPath_(str(path.parent) + "/")
        self.kanban_web.loadHTMLString_baseURL_(html_str, base_url)
        sys.stderr.write(f"[kanban-web] reloaded {path.name} (mtime {mtime:.0f})\n")

    @objc.python_method
    def _install_layout(self):
        """Install the WKWebView as the popover's content view. Both
        list and kanban modes render through the same web view —
        kanban.html branches its CSS on the payload.mode field, so we
        never need to swap subviews when toggling between modes.
        Only the popover *size* changes per mode (see _target_popover_size)."""
        if self.content_host is None:
            return
        for sub in list(self.content_host.subviews()):
            sub.removeFromSuperview()
        if self.kanban_web is not None:
            self.kanban_web.setFrame_(self.content_host.bounds())
            self.content_host.addSubview_(self.kanban_web)

    @objc.python_method
    def _target_popover_size(self) -> tuple:
        """Pick the right popover (w, h) for the current mode + toggle."""
        if self.mode != "kanban":
            return POPOVER_LIST_SIZE
        if self.show_dormant:
            return POPOVER_KANBAN_WITH_DORMANT_SIZE
        return POPOVER_KANBAN_SIZE

    @objc.python_method
    def _update_mark_all_button(self) -> None:
        """Position the top-bar 'Mark all N read' button to sit just
        left of the 'Show older' checkbox, sized to fit its current
        title. Visible only in kanban mode and only when ≥ 1 unread.

        List mode is unaffected: its inline footer link inside the
        NSTextView already covers this action."""
        btn = self.mark_all_btn
        if btn is None:
            return
        unread_count = sum(
            1 for r in (self.last_rendered_rows or []) if r.get("unread")
        )
        if self.mode != "kanban" or unread_count <= 0:
            btn.setHidden_(True)
            return
        btn.setTitle_(f"✓ Mark all {unread_count} as read")
        btn.sizeToFit()
        bf = btn.frame()
        # Anchor to the right edge of the container, immediately left
        # of the dormant checkbox.
        container = self.view()
        cw = container.frame().size.width if container is not None else 720
        # Match the dormant button's top inset (h - TOP_BAR_HEIGHT + 6).
        TOP_BAR_HEIGHT = 32.0
        ch = container.frame().size.height if container is not None else 480
        dormant_w = (
            self.dormant_btn.frame().size.width
            if self.dormant_btn is not None else 90.0
        )
        x = cw - dormant_w - 24 - bf.size.width
        y = ch - TOP_BAR_HEIGHT + 6
        btn.setFrame_(NSMakeRect(x, y, bf.size.width, bf.size.height))
        btn.setHidden_(False)

    @objc.python_method
    def _apply_popover_size(self) -> None:
        if self.popover_ref is None:
            return
        try:
            w, h = self._target_popover_size()
            self.popover_ref.setContentSize_(NSMakeSize(w, h))
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[_apply_popover_size] {e!r}\n")

    def segmentChanged_(self, sender):
        try:
            idx = sender.selectedSegment()
            new_mode = "kanban" if idx == 1 else "list"
            if new_mode == self.mode:
                return
            self.mode = new_mode
            _write_popover_mode(new_mode)
            self._apply_popover_size()
            self._install_layout()
            self.refresh()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[segmentChanged_] {e!r}\n")
            import traceback
            traceback.print_exc(file=sys.stderr)

    def toggleDormant_(self, sender):
        """Flip whether dormant sessions are shown in the popover. In
        kanban mode this also widens the popover to fit a 4th column."""
        try:
            self.show_dormant = bool(sender.state())   # 0 / 1
            _write_show_dormant(self.show_dormant)
            self._apply_popover_size()
            self.refresh()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[toggleDormant_] {e!r}\n")

    def densityChanged_(self, sender):
        """Glance / Focus / Detail — orthogonal to list/kanban."""
        try:
            # NSPopUpButton exposes indexOfSelectedItem; the older
            # NSSegmentedControl exposed selectedSegment. Use whichever
            # the sender supports so this stays robust if we swap the
            # control type back later.
            if hasattr(sender, "indexOfSelectedItem"):
                idx = sender.indexOfSelectedItem()
            else:
                idx = sender.selectedSegment()
            if not (0 <= idx < len(DENSITIES)):
                return
            new_density = DENSITIES[idx]
            if new_density == self.density:
                return
            self.density = new_density
            _write_density(new_density)
            self.refresh()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[densityChanged_] {e!r}\n")

    def markAllReadClicked_(self, _sender):
        """Kanban footer 'Mark all N read' button. Same effect as the
        cssreadall:// link in list mode: mark every currently-rendered
        session as read at its current lastTurnEpoch."""
        try:
            pairs = []
            for r in (self.last_rendered_rows or []):
                sid = (r.get("s") or {}).get("sessionId")
                epoch = r.get("lastTurnEpoch")
                if sid and epoch is not None:
                    pairs.append((sid, epoch))
            if pairs:
                _mark_sessions_read(pairs)
                self.refresh()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[markAllReadClicked_] {e!r}\n")

    @objc.python_method
    def refresh(self):
        try:
            self._refresh_safely()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[PopoverVC.refresh] {e!r}\n")
            import traceback
            traceback.print_exc(file=sys.stderr)

    @objc.python_method
    def _refresh_safely(self):
        try:
            buckets = _get_buckets()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[popover] data fetch failed: {e!r}\n")
            return

        # Cache rows so "Mark all read" knows which sessions are visible.
        flat: list = []
        for k in ("needs", "working", "ready", "dormant"):
            flat.extend(buckets.get(k) or [])
        self.last_rendered_rows = flat

        # Both modes route through the same WKWebView — kanban.html's
        # CSS branches on body.mode-{list,kanban}. The web view is
        # always the popover's content view; we just push a different
        # payload + mode field per refresh.
        self._render_popover(buckets)

    @objc.python_method
    def _render_list(self, buckets):
        """Back-compat shim — kept so any future caller that explicitly
        asks for the list-mode render still works. Delegates to the
        unified popover renderer below."""
        self._render_popover(buckets)

    @objc.python_method
    def _serialize_row(self, row: dict) -> dict:
        """Slim a backend row dict down to what kanban.html actually
        needs. Anything the JS doesn't read is dropped — keeps the
        evaluateJavaScript payload small and avoids leaking transcript
        meta blobs into the web context."""
        s = row.get("s") or {}
        meta = row.get("meta") or {}
        cwd = meta.get("cwd") if isinstance(meta.get("cwd"), str) else ""
        # Friendlier folder display: collapse $HOME → ~ so a path like
        # /Users/moustafasamir/Documents/X reads as ~/Documents/X.
        if cwd.startswith(str(HOME)):
            cwd_display = "~" + cwd[len(str(HOME)):]
        else:
            cwd_display = cwd
        # Sub-agents — only carry the three fields JS renders.
        subs_out = []
        for sub in (row.get("subagents") or []):
            subs_out.append({
                "type": (sub.get("agent_type") or "").strip(),
                "desc": (sub.get("name") or "").strip()[:140],
                "state": sub.get("state") or "",
            })
        # User-curated tasks for this session — render-ordered.
        # The schema carries (id, content, status, source, approved)
        # plus timestamps; the JS only needs the first five so we
        # slim the payload here.
        sid = s.get("sessionId") or ""
        session_tasks = []
        for t in tasks_module.tasks_for_session(sid):
            session_tasks.append({
                "id": t.get("id") or "",
                "content": t.get("content") or "",
                "status": t.get("status") or "open",
                "source": t.get("source") or "user",
                "approved": bool(t.get("approved", True)),
            })
        return {
            "sessionId": sid,
            "cwd": cwd,
            "folder": cwd_display,
            "title": (row.get("title") or "(untitled)").strip(),
            "phase": row.get("phase_label") or "",
            "gist": row.get("gist") or "",
            "age": format_ago(row.get("ago_s") or 0),
            "bucket": row.get("bucket") or "dormant",
            "unread": bool(row.get("unread")),
            "subagents": subs_out,
            "tasks": session_tasks,
        }

    @objc.python_method
    def _render_kanban(self, buckets):
        """Back-compat alias for the unified popover renderer."""
        self._render_popover(buckets)

    @objc.python_method
    def _render_popover(self, buckets):
        """Push the bucket data into the WKWebView. The same rendering
        path serves both kanban (3-4 columns) and list (vertical stack)
        modes — kanban.html branches on body.mode-{kanban,list} from
        the payload.mode field. JS does the actual DOM build; we just
        serialize + evaluateJavaScript."""
        if self.kanban_web is None:
            return

        # Hot-reload kanban.html if it changed on disk. Cheap (one
        # stat call); makes design iteration feel instant.
        self._maybe_reload_kanban_html()

        payload = {
            "buckets": {
                k: [self._serialize_row(r) for r in (buckets.get(k) or [])]
                for k in ("needs", "working", "ready", "dormant")
            },
            "showDormant": bool(self.show_dormant),
            "density": self.density or "focus",
            # "list" or "kanban" — controls the CSS layout branch.
            "mode": "list" if self.mode == "list" else "kanban",
        }

        # If the JS isn't ready yet (initial loadHTMLString hasn't
        # settled), buffer the payload — the navigation-finished JS
        # handshake flushes it via _kanban_web_flush_pending.
        if not self.kanban_web_ready:
            self.kanban_web_pending = payload
        else:
            self._kanban_web_evaluate(payload)

        self._update_mark_all_button()

    @objc.python_method
    def _kanban_web_evaluate(self, payload: dict):
        """Serialize + evaluateJavaScript window.renderApp(payload)."""
        try:
            # ensure_ascii=True is defense-in-depth: any non-ASCII
            # character in a session title / gist / folder path gets
            # rewritten as a \uXXXX escape, so we never need to worry
            # about a transcript embedding `</script>`, U+2028, or any
            # other JS-source-aware sequence smuggling into the JS
            # string literal we're about to build below.
            js_data = json.dumps(payload, ensure_ascii=True)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[kanban-web] payload serialize: {e!r}\n")
            return
        # Use JSON.parse on a string literal — embedding the JSON
        # directly as a JS literal would force us to escape every
        # interior backtick/quote. JSON.parse handles arbitrary
        # content inside a single quoted string instead.
        encoded = json.dumps(js_data)  # double-encode to a JS string literal
        js = f"window.renderApp(JSON.parse({encoded}));"
        self.kanban_web.evaluateJavaScript_completionHandler_(js, None)

    # ---- WKScriptMessageHandler ----
    # Called by WebKit when JS posts via
    # window.webkit.messageHandlers.kanban.postMessage(...). This is
    # a thin ObjC method that decodes the NSDictionary body into a
    # plain Python dict, then delegates to _handle_script_message —
    # which is testable directly (no ObjC self check) so the action
    # dispatch can be exercised without instantiating an AppKit view.
    def userContentController_didReceiveScriptMessage_(self, ctrl, msg):
        try:
            body = msg.body()
            if hasattr(body, "objectForKey_"):
                action = str(body.objectForKey_("action") or "")
                payload_raw = body.objectForKey_("payload") or {}
                if hasattr(payload_raw, "objectForKey_"):
                    payload = {}
                    for k in payload_raw.allKeys():
                        payload[str(k)] = payload_raw.objectForKey_(k)
                else:
                    payload = dict(payload_raw or {})
            else:
                action = str((body or {}).get("action") or "")
                payload = dict((body or {}).get("payload") or {})
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[kanban-web] msg decode: {e!r}\n")
            return
        self._handle_script_message(action, payload)

    @objc.python_method
    def _handle_script_message(self, action: str, payload: dict):
        """Pure-Python dispatch for bridge actions. Separated from the
        ObjC entry point so unit tests can call this directly with a
        plain dict, avoiding the ObjC ``isKindOfClass`` self-check
        that prevents duck-typed test doubles."""
        def pstr(k: str) -> str:
            """Extract payload[k] as a plain Python string. Rejects
            anything that isn't a string-like type — a malformed JS
            message that sent a dict or a number for `content` would
            otherwise stringify to garbage like ``str({"foo":1})`` and
            pollute the tasks file. NSString is bridged as a str
            subclass by PyObjC, so the isinstance check accepts it
            naturally."""
            v = payload.get(k)
            if isinstance(v, str):
                return v
            # NSString that didn't inherit from str (older PyObjC):
            # try to coerce only if the value implements the
            # NSString-y selector. Anything else (dict, list, number,
            # None) returns the empty string and the per-action
            # validation in tasks.py rejects it cleanly.
            if v is not None and hasattr(v, "UTF8String"):
                try:
                    return str(v)
                except Exception:  # noqa: BLE001
                    return ""
            return ""

        sid = pstr("sessionId")
        cwd = pstr("cwd")

        if action == "resume":
            if not sid:
                sys.stderr.write("[kanban-web] resume: empty sessionId\n")
                return
            # Mark-as-read on resume — engaging with the session.
            for r in (self.last_rendered_rows or []):
                if (r.get("s") or {}).get("sessionId") == sid:
                    epoch = r.get("lastTurnEpoch")
                    _mark_session_read(sid, epoch if epoch is not None else time.time())
                    break
            host = _find_live_session_host(sid)
            sys.stderr.write(
                f"[kanban-web] resume sid={sid!r} cwd={cwd!r} "
                f"host={host!r}\n"
            )
            self._open_session_in_terminal(sid, cwd or os.path.expanduser("~"))
            if self.popover_ref is not None:
                try:
                    self.popover_ref.close()
                except Exception:  # noqa: BLE001
                    pass
        elif action == "markRead":
            if not sid:
                return
            epoch = None
            for r in (self.last_rendered_rows or []):
                if (r.get("s") or {}).get("sessionId") == sid:
                    epoch = r.get("lastTurnEpoch")
                    break
            _mark_session_read(sid, epoch if epoch is not None else time.time())
            self.refresh()
        elif action == "ready":
            # First time the JS finishes mounting — flush any buffered
            # payload from refreshes that arrived before now.
            self.kanban_web_ready = True
            if self.kanban_web_pending is not None:
                self._kanban_web_evaluate(self.kanban_web_pending)
                self.kanban_web_pending = None
        elif action == "taskCreate":
            # User typed a task into the + add input and hit Enter.
            content = pstr("content")
            if not sid or not content:
                return
            created = tasks_module.create_task(sid, content)
            if created is None:
                # Validation failed (empty, too long, or per-session
                # cap hit). Don't refresh — JS keeps the input open so
                # the user can edit and retry without losing context.
                sys.stderr.write(
                    f"[tasks] create rejected sid={sid[:8]} "
                    f"content_len={len(content)}\n"
                )
                return
            self.refresh()
        elif action == "taskToggle":
            # Click on the task glyph: open ↔ done.
            tid = pstr("taskId")
            if not sid or not tid:
                return
            new_status = tasks_module.toggle_task(sid, tid)
            if new_status is None:
                sys.stderr.write(f"[tasks] toggle miss sid={sid[:8]} tid={tid}\n")
                return
            self.refresh()
        elif action == "taskEdit":
            # User finished editing a task's text (Enter or blur on a
            # changed value). Only user-authored tasks are editable;
            # tasks.update_task enforces that rule.
            tid = pstr("taskId")
            content = pstr("content")
            # Trace every attempt so we can diagnose Enter-doesn't-save
            # complaints by tailing /tmp/floating.log. The `source`
            # field comes from the JS commit() and tells us whether
            # the event fired from enter-keydown / enter-keyup / blur.
            # Truncate the content preview so a 280-char task doesn't
            # flood the log.
            sys.stderr.write(
                f"[tasks] edit recv sid={sid[:8] if sid else '<empty>'} "
                f"tid={tid!r} source={pstr('source')!r} "
                f"content_len={len(content)} "
                f"preview={content[:60]!r}\n"
            )
            if not sid or not tid or not content:
                sys.stderr.write(
                    f"[tasks] edit dropped — sid={bool(sid)} "
                    f"tid={bool(tid)} content={bool(content)}\n"
                )
                return
            if tasks_module.update_task(sid, tid, content) is None:
                sys.stderr.write(
                    f"[tasks] edit rejected by update_task — "
                    f"sid={sid[:8]} tid={tid} content_len={len(content)}\n"
                )
                return
            sys.stderr.write(f"[tasks] edit applied sid={sid[:8]} tid={tid}\n")
            self.refresh()
        elif action == "taskDelete":
            # Hover-revealed × on a user-authored task row.
            tid = pstr("taskId")
            if not sid or not tid:
                return
            if not tasks_module.delete_task(sid, tid):
                sys.stderr.write(f"[tasks] delete miss sid={sid[:8]} tid={tid}\n")
                return
            self.refresh()
        elif action == "taskReorder":
            # User drag-dropped a task to reorder. JS sends the full
            # new order as a list of task IDs (open user-authored
            # bucket only). Other buckets are not touched.
            if not sid:
                return
            ordered_raw = payload.get("orderedIds")
            # Coerce NSArray → list[str] if needed.
            if hasattr(ordered_raw, "objectAtIndex_"):
                ordered: list = []
                try:
                    for i in range(ordered_raw.count()):
                        v = ordered_raw.objectAtIndex_(i)
                        if isinstance(v, str):
                            ordered.append(v)
                        elif hasattr(v, "UTF8String"):
                            try:
                                ordered.append(str(v))
                            except Exception:  # noqa: BLE001
                                pass
                except Exception as e:  # noqa: BLE001
                    sys.stderr.write(f"[tasks] reorder decode: {e!r}\n")
                    return
            elif isinstance(ordered_raw, (list, tuple)):
                ordered = [str(x) for x in ordered_raw if isinstance(x, str)]
            else:
                ordered = []
            if not ordered:
                return
            if not tasks_module.reorder_tasks(sid, ordered):
                sys.stderr.write(
                    f"[tasks] reorder failed sid={sid[:8]} "
                    f"count={len(ordered)}\n"
                )
                return
            self.refresh()
        elif action == "taskApprove":
            # ✓ on a Haiku-suggested row — ratify into a normal task.
            tid = pstr("taskId")
            if not sid or not tid:
                return
            if not tasks_module.approve_suggestion(sid, tid):
                sys.stderr.write(f"[tasks] approve miss sid={sid[:8]} tid={tid}\n")
                return
            self.refresh()
        elif action == "taskReject":
            # ✗ on a Haiku-suggested row — dismiss + remember to not
            # re-suggest the same content within this session.
            tid = pstr("taskId")
            if not sid or not tid:
                return
            if not tasks_module.reject_suggestion(sid, tid):
                sys.stderr.write(f"[tasks] reject miss sid={sid[:8]} tid={tid}\n")
                return
            self.refresh()
        else:
            sys.stderr.write(f"[kanban-web] unknown action: {action!r}\n")

    @objc.python_method
    def _row_paragraph_style(self, right_edge: float):
        """Paragraph style with a right tab stop, used so the age aligns
        to the right edge of the row. The line break mode is set to
        truncating-tail so long titles get an ellipsis instead of
        pushing the tab stop off-screen / onto a wrapped line."""
        ps = NSMutableParagraphStyle.alloc().init()
        tab = NSTextTab.alloc().initWithTextAlignment_location_options_(
            2,             # NSTextAlignmentRight
            right_edge,
            {},
        )
        ps.setTabStops_([tab])
        # If the title alone exceeds the tab-stop position, NSTextView
        # would normally wrap and put the age on a new line. Setting
        # truncate-tail (=4) makes the title get clipped with "…"
        # instead, keeping the row a tidy single line.
        ps.setLineBreakMode_(4)
        # And: any tab past our explicit stop falls back to a default
        # interval. Force that interval to the same right_edge so a
        # second tab (if anything ever introduces one) doesn't snap
        # to a stray 28pt grid.
        ps.setDefaultTabInterval_(right_edge)
        return ps

    # ---- NSTextView link delegate ----
    # NSTextView calls this when the user clicks an attributed-string
    # link. We use it to intercept our custom cssread:// (single session)
    # and cssreadall:// (all visible sessions) URLs.
    def textView_clickedOnLink_atIndex_(self, _text_view, link, _char_index):
        try:
            url_str = str(link) if link is not None else ""
            if url_str.startswith("cssread://"):
                sid = url_str[len("cssread://"):]
                # Find the corresponding row to learn its current epoch.
                epoch = None
                for r in (self.last_rendered_rows or []):
                    if (r.get("s") or {}).get("sessionId") == sid:
                        epoch = r.get("lastTurnEpoch")
                        break
                _mark_session_read(sid, epoch if epoch is not None else time.time())
                self.refresh()
                return True
            if url_str.startswith("cssreadall:"):
                pairs = []
                for r in (self.last_rendered_rows or []):
                    sid = (r.get("s") or {}).get("sessionId")
                    epoch = r.get("lastTurnEpoch")
                    if sid and epoch is not None:
                        pairs.append((sid, epoch))
                _mark_sessions_read(pairs)
                self.refresh()
                return True
            if url_str.startswith("cssresume://"):
                sid = url_str[len("cssresume://"):]
                # Look up cwd for the click target so we can cd before
                # running claude --resume. Missing cwd falls back to $HOME.
                cwd = os.path.expanduser("~")
                for r in (self.last_rendered_rows or []):
                    if (r.get("s") or {}).get("sessionId") == sid:
                        meta = r.get("meta") or {}
                        c = meta.get("cwd")
                        if isinstance(c, str) and c.strip():
                            cwd = c
                        break
                self._open_session_in_terminal(sid, cwd)
                # Auto-mark-as-read when the user resumes — they're
                # clearly engaging with the session.
                if sid:
                    epoch = None
                    for r in (self.last_rendered_rows or []):
                        if (r.get("s") or {}).get("sessionId") == sid:
                            epoch = r.get("lastTurnEpoch")
                            break
                    _mark_session_read(sid, epoch if epoch is not None else time.time())
                # Close the popover — the user is moving to the terminal.
                if self.popover_ref is not None:
                    try:
                        self.popover_ref.close()
                    except Exception:  # noqa: BLE001
                        pass
                return True
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[textView_clickedOnLink] {e!r}\n")
            import traceback
            traceback.print_exc(file=sys.stderr)
        return False

    @objc.python_method
    def _open_session_in_terminal(self, sid: str, cwd: str) -> None:
        """Navigate to a session — preferring an existing host over
        spawning a new one. Three branches:

        1. Session is alive in Claude.app (entrypoint=claude-desktop)
           → bring Claude.app to the front so the user can select that
           session in its in-app sidebar.

        2. Session is alive in a Terminal.app tab → match the claude
           process's TTY to a tab's tty via AppleScript and focus that
           exact tab. No duplicate window.

        3. Session isn't alive anywhere → spawn a new Terminal at the
           project cwd and run `claude --resume <sid>`.

        Stays no-op safe if any step fails (the user sees nothing
        happen but no crash)."""
        if not sid:
            return
        host = _find_live_session_host(sid)
        if host is not None:
            if host.get("kind") == "claude-desktop":
                self._activate_claude_desktop()
                return
            if host.get("kind") == "terminal":
                term_app = host.get("terminal_app") or "Terminal"
                if term_app == "Terminal" and host.get("tty"):
                    if _focus_terminal_tab_for_tty(host["tty"]):
                        return
                # Unknown terminal — best effort, bring the app forward.
                if term_app:
                    self._activate_app(term_app)
                    return
        # No live host or focus failed — spawn a new Terminal session.
        self._spawn_new_terminal_session(sid, cwd)

    @objc.python_method
    def _activate_claude_desktop(self) -> None:
        """`open -a Claude` brings the Claude Desktop app to the front.
        From there the user can pick the specific session in the
        Desktop's own sidebar. We can't navigate further than that
        without an undocumented URL scheme."""
        self._activate_app("Claude")

    @objc.python_method
    def _activate_app(self, app_name: str) -> None:
        try:
            subprocess.Popen(
                ["open", "-a", app_name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as e:
            sys.stderr.write(f"[_activate_app {app_name}] {e!r}\n")

    @objc.python_method
    def _spawn_new_terminal_session(self, sid: str, cwd: str) -> None:
        import shlex
        shell_cmd = (
            f"cd {shlex.quote(cwd)} && claude --resume {shlex.quote(sid)}"
        )
        try:
            subprocess.Popen(
                [
                    "osascript",
                    "-e", 'tell application "Terminal" to activate',
                    "-e", f'tell application "Terminal" to do script "{shell_cmd}"',
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as e:
            sys.stderr.write(f"[_spawn_new_terminal_session] {e!r}\n")

    @objc.python_method
    def _accent_color(self):
        """Unread indicator color — uses the system accent (blue by
        default, but adapts to whatever the user picked in System
        Settings ▸ Appearance)."""
        try:
            return NSColor.controlAccentColor()
        except Exception:  # noqa: BLE001
            # Fall back to a fixed Apple blue for older macOS.
            return NSColor.colorWithDisplayP3Red_green_blue_alpha_(
                0.0392, 0.5176, 1.0, 1.0,
            )

    @objc.python_method
    def _append_row(self, out, row, color, title_font, gist_font, meta_font,
                    bar_font, dim, very_dim, *, right_edge: float = 332.0,
                    show_preview: bool = True, kanban_mode: bool = False,
                    density: str = "focus"):
        """Render one session row with strong visual hierarchy:
          1. Big bold title + right-aligned age (via tab stop)
          2. Phase + gist line (secondary text)
          3. Quoted preview of Claude's most recent text (the actual
             content you'd want to read for context-switching)
          4. Project path (tertiary, smallest)
        Followed by a blank line to give the next row breathing room.

        Density gates what's shown:
          - glance: just line 1 (title + age + ✓)
          - focus:  lines 1 + 2 (title, phase/gist)
          - detail: all four lines (adds quoted preview + cwd)
        """
        meta = row.get("meta") or {}
        title = (row.get("title") or "(untitled)").strip()
        phase = row.get("phase_label") or ""
        gist = row.get("gist") or ""
        ago = format_ago(row.get("ago_s") or 0)
        cwd_raw = meta.get("cwd") if isinstance(meta, dict) else None
        cwd = (
            cwd_raw.replace(os.path.expanduser("~"), "~")
            if isinstance(cwd_raw, str) and cwd_raw else ""
        )
        def _clip(s: str, n: int) -> str:
            s = " ".join(s.split())
            return s if len(s) <= n else s[: n - 1] + "…"

        preview = ""
        gist_extra = ""        # appended to line 2 in detail mode
        user_prompt = ""
        tools_list: list = []
        if show_preview and isinstance(meta, dict):
            raw = meta.get("lastAssistantText") or meta.get("lastAction") or ""
            if isinstance(raw, str) and raw.strip():
                # In detail mode we lift the assistant preview *into*
                # line 2 (giving the summary more words) instead of
                # showing it as a separate 'Claude: …' grey line.
                # Focus mode doesn't render a preview at all.
                if density == "detail":
                    gist_extra = _clip(raw, 240)
                else:
                    preview = _clip(raw, 120)
            up = meta.get("latestUserPrompt") or ""
            if isinstance(up, str) and up.strip() and density == "detail":
                # Tighter than before — the grey block carries less
                # weight now that line 2 is doing more of the work.
                user_prompt = _clip(up, 100)
            if density == "detail":
                tools_seen: set = set()
                for t in (meta.get("recentTools")
                          or meta.get("lastAssistantTools") or []):
                    if not isinstance(t, str) or t in tools_seen:
                        continue
                    tools_seen.add(t)
                    tools_list.append(t)
                    if len(tools_list) >= 4:
                        break

        # Fonts for the gist line specifically — promoted to share
        # visual focus with the title (13pt vs 14pt title; same primary
        # labelColor for the gist body, bucket-tint semibold for the
        # phase tag prefix).
        gist_emphasis_font = NSFont.systemFontOfSize_(13)
        phase_tag_font = NSFont.systemFontOfSize_weight_(13, NSFontWeightSemibold)

        ps = self._row_paragraph_style(right_edge)

        def append(text: str, attrs: dict) -> None:
            out.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(text, attrs)
            )

        unread = bool(row.get("unread"))
        sid = row.get("s", {}).get("sessionId") or ""
        accent = self._accent_color()

        # ---- Line 1: [● if unread] ▎ Title  [tab]  age  [✓ if unread] ----
        # Leading 2-space indent (same as before).
        append("  ", {
            NSFontAttributeName: title_font,
            NSParagraphStyleAttributeName: ps,
        })
        # Unread indicator — small blue dot, takes the slot where the
        # ▎ bar would be alone for read rows.
        if unread:
            append("● ", {
                NSFontAttributeName: bar_font,
                NSForegroundColorAttributeName: accent,
                NSParagraphStyleAttributeName: ps,
            })
        append("▎", {
            NSFontAttributeName: bar_font,
            NSForegroundColorAttributeName: color,
            NSParagraphStyleAttributeName: ps,
        })
        # Title — clickable link that resumes the session in Terminal.
        # `cssresume://<sessionId>` is intercepted by our NSTextView
        # delegate (textView_clickedOnLink_atIndex_) which spawns
        # `claude --resume <sid>` in a Terminal window at the session's
        # cwd. Hovering shows the standard underline + I-beam cursor.
        title_attrs = {
            NSFontAttributeName: title_font,
            NSForegroundColorAttributeName: NSColor.labelColor(),
            NSParagraphStyleAttributeName: ps,
        }
        if sid:
            title_attrs[NSLinkAttributeName] = NSURL.URLWithString_(
                f"cssresume://{sid}",
            )
        append(f" {title}", title_attrs)

        # Trailing block: age + (optional) ✓ mark-read link.
        #
        # List mode: render inline on the same line as the title, tab-
        # right-aligned, because list rows are wide (360+px).
        #
        # Kanban mode: kanban columns are narrow (~240px); the title
        # almost always exceeds the available width and truncatingTail
        # would eat the ✓ at the end. So in kanban we end the title
        # line and put `age  ✓` on its own indented line below, which
        # is always visible regardless of title length.
        age_attrs = {
            NSFontAttributeName: meta_font,
            NSForegroundColorAttributeName: very_dim,
            NSParagraphStyleAttributeName: ps,
        }
        check_attrs = {
            NSFontAttributeName: title_font,
            NSForegroundColorAttributeName: accent,
            NSLinkAttributeName: NSURL.URLWithString_(f"cssread://{sid}") if sid else None,
            NSParagraphStyleAttributeName: ps,
        }
        # Drop the NSLinkAttribute entry if it'd be None (no sid).
        check_attrs = {k: v for k, v in check_attrs.items() if v is not None}

        if kanban_mode:
            # End the title line, no inline tab/age.
            append("\n", title_attrs)
            # Indented meta line: age + optional ✓ on its own row,
            # using a plain paragraph style (no tab-right alignment)
            # so it doesn't risk overflowing the column.
            plain_attrs = {
                NSFontAttributeName: meta_font,
                NSForegroundColorAttributeName: very_dim,
            }
            append("     ", plain_attrs)
            append(ago, plain_attrs)
            if unread and sid:
                append("  ", plain_attrs)
                append("✓", {
                    NSFontAttributeName: title_font,
                    NSForegroundColorAttributeName: accent,
                    NSLinkAttributeName: NSURL.URLWithString_(f"cssread://{sid}"),
                })
            append("\n", plain_attrs)
        elif unread and sid:
            append(f"\t{ago}  ", age_attrs)
            append("✓", check_attrs)
            append("\n", age_attrs)
        else:
            append(f"\t{ago}\n", age_attrs)

        # ---- Line 2: phase (quiet secondary) + gist (primary text) ----
        # Hidden in glance mode (one-line-per-row). The phase used to
        # be rendered in the bucket-tint color (red/amber/green) which
        # made every row's line 2 a competing color cue alongside the
        # left ▎ bar — too loud. The bar is now the only bucket-tinted
        # element on a row; the phase reads in secondary-label color so
        # the gist (primary label color) wins focus.
        if density != "glance" and (phase or gist):
            # Indent matching the title's leading inset.
            append("     ", {NSFontAttributeName: gist_emphasis_font})
            if phase:
                append(phase, {
                    NSFontAttributeName: phase_tag_font,
                    NSForegroundColorAttributeName: dim,
                })
                if gist:
                    # Subtle separator dot, slightly dimmer than the
                    # phase label so the eye treats it as punctuation.
                    append("  ·  ", {
                        NSFontAttributeName: phase_tag_font,
                        NSForegroundColorAttributeName: very_dim,
                    })
            if gist:
                append(gist, {
                    NSFontAttributeName: gist_emphasis_font,
                    NSForegroundColorAttributeName: NSColor.labelColor(),
                })
            # Extend line 2 in detail mode with a longer assistant
            # follow-on so the summary itself carries more words. The
            # separate 'Claude: …' grey line is intentionally dropped.
            if gist_extra:
                if gist:
                    append("  —  ", {
                        NSFontAttributeName: gist_emphasis_font,
                        NSForegroundColorAttributeName: dim,
                    })
                append(gist_extra, {
                    NSFontAttributeName: gist_emphasis_font,
                    NSForegroundColorAttributeName: NSColor.labelColor(),
                })
            append("\n", {NSFontAttributeName: gist_emphasis_font})

        # ---- Detail mode: user prompt → tools → cwd ----
        # (The assistant snippet now lives in line 2; this block stays
        # short on purpose.)
        if density == "detail":
            quote_font = NSFont.systemFontOfSize_(12)
            label_font = NSFont.systemFontOfSize_weight_(11, NSFontWeightSemibold)
            if user_prompt:
                append("     ", {NSFontAttributeName: quote_font})
                append("You: ", {
                    NSFontAttributeName: label_font,
                    NSForegroundColorAttributeName: very_dim,
                })
                append(f"{user_prompt}\n", {
                    NSFontAttributeName: quote_font,
                    NSForegroundColorAttributeName: dim,
                })
            if tools_list:
                append("     ", {NSFontAttributeName: meta_font})
                append("Tools: ", {
                    NSFontAttributeName: label_font,
                    NSForegroundColorAttributeName: very_dim,
                })
                append(f"{' · '.join(tools_list)}\n", {
                    NSFontAttributeName: meta_font,
                    NSForegroundColorAttributeName: dim,
                })
            if cwd:
                append(f"     {cwd}\n", {
                    NSFontAttributeName: meta_font,
                    NSForegroundColorAttributeName: very_dim,
                })

        # Blank spacer line so rows don't run together. In glance mode
        # we use a smaller gap since rows are one line each.
        spacer_font = (
            NSFont.systemFontOfSize_(4) if density == "glance" else gist_font
        )
        append("\n", {NSFontAttributeName: spacer_font})

    @objc.python_method
    def _build_attributed(self, buckets: dict) -> NSAttributedString:
        """Render bucket headers + session rows into one attributed string
        for the list view. Hierarchy is: bigger bold title with the age
        right-aligned via a tab stop, secondary phase/gist line, a
        quoted preview of Claude's actual recent text (the context you'd
        want for switching back into the session), and a tertiary path."""
        header_marker_font = NSFont.systemFontOfSize_(11)
        header_font = NSFont.systemFontOfSize_weight_(11, NSFontWeightSemibold)
        header_count_font = _rounded_tabular_font(11.0, NSFontWeightRegular)
        title_font = NSFont.systemFontOfSize_weight_(14, NSFontWeightSemibold)
        gist_font = NSFont.systemFontOfSize_(12)
        meta_font = _rounded_tabular_font(11.0, NSFontWeightRegular)
        bar_font = NSFont.systemFontOfSize_(16)  # slightly taller for the ▎ glyph
        section_spacer_font = NSFont.systemFontOfSize_(8)

        dim = NSColor.secondaryLabelColor()
        very_dim = NSColor.tertiaryLabelColor()

        # Right tab stop x position — slightly less than the popover
        # content width so the age sits with a small right margin.
        list_right_edge = POPOVER_LIST_SIZE[0] - 28

        out = NSMutableAttributedString.alloc().init()
        any_rows = False
        first_section = True

        section_pairs = [(k, _bucket_tint(k)) for k in ("needs", "working", "ready")]
        # Dormant only renders when the user has the toggle enabled.
        if self.show_dormant:
            section_pairs.append(("dormant", NSColor.tertiaryLabelColor()))

        for key, color in section_pairs:
            rows = buckets.get(key) or []
            if not rows:
                continue
            any_rows = True

            # Add a generous breathing-room spacer above every section
            # except the first. Pure whitespace as the section divider —
            # no horizontal lines, no headers competing with the title.
            if not first_section:
                out.appendAttributedString_(
                    NSAttributedString.alloc().initWithString_attributes_(
                        "\n",
                        {NSFontAttributeName: section_spacer_font},
                    )
                )
            first_section = False

            # Bucket header — refined: small bucket-tinted dot + a
            # sentence-case label in secondary color + tabular count in
            # tertiary. The dot carries the status color; the label
            # stays muted so the row titles below dominate visually.
            label_text = LABELS[key].title()      # "NEEDS YOU" → "Needs You"
            header_attr = NSMutableAttributedString.alloc().init()
            header_attr.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(
                    "  ●  ", {
                        NSFontAttributeName: header_marker_font,
                        NSForegroundColorAttributeName: color,
                    },
                )
            )
            header_attr.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(
                    label_text, {
                        NSFontAttributeName: header_font,
                        NSForegroundColorAttributeName: dim,
                    },
                )
            )
            header_attr.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(
                    f"   {len(rows)}\n", {
                        NSFontAttributeName: header_count_font,
                        NSForegroundColorAttributeName: very_dim,
                    },
                )
            )
            out.appendAttributedString_(header_attr)

            for row in rows:
                self._append_row(
                    out, row, color,
                    title_font, gist_font, meta_font, bar_font,
                    dim, very_dim,
                    right_edge=list_right_edge,
                    # Dormant rows don't need the literal preview —
                    # they're stale by definition, save vertical space.
                    show_preview=(key != "dormant"),
                    density=self.density or "focus",
                )

        if not any_rows:
            out.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(
                    "  No active sessions in the last 24h.\n",
                    {
                        NSFontAttributeName: NSFont.systemFontOfSize_(12),
                        NSForegroundColorAttributeName: dim,
                    },
                )
            )
            return out

        # "Mark all read" footer link — only shown when at least one row
        # is currently unread, so we don't clutter the popover when
        # everything's caught up.
        unread_count = sum(
            1 for r in (self.last_rendered_rows or []) if r.get("unread")
        )
        if unread_count > 0:
            footer_font = NSFont.systemFontOfSize_(11)
            out.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(
                    "  ", {NSFontAttributeName: footer_font}
                )
            )
            out.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(
                    f"✓ Mark all {unread_count} as read",
                    {
                        NSFontAttributeName: footer_font,
                        NSForegroundColorAttributeName: self._accent_color(),
                        NSLinkAttributeName:
                            NSURL.URLWithString_("cssreadall://"),
                    },
                )
            )
            out.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(
                    "\n", {NSFontAttributeName: footer_font}
                )
            )
        return out


class BadgeController(NSObject):
    """Owns the always-visible capsule badge AND its attached NSPopover.
    Click the badge to toggle the popover (which renders the session
    list); the popover stays anchored to the badge via NSPopover's built-in
    arrow + auto-positioning."""

    badge_window = objc.ivar("badge_window")
    badge_view = objc.ivar("badge_view")
    popover = objc.ivar("popover")
    timer = objc.ivar("timer")
    outside_click_monitor = objc.ivar("outside_click_monitor")  # NSEvent monitor

    def initWithPanelMode_(self, panel_mode: str):
        self = objc.super(BadgeController, self).init()
        if self is None:
            return None

        # Build the badge window — borderless, non-activating panel.
        x, y = _load_badge_origin()
        style = (
            NSWindowStyleMaskBorderless
            | NSWindowStyleMaskNonactivatingPanel
        )
        win = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, BADGE_WIDTH, BADGE_HEIGHT),
            style, NSBackingStoreBuffered, False,
        )
        win.setLevel_(NSFloatingWindowLevel)
        win.setOpaque_(False)
        win.setBackgroundColor_(NSColor.clearColor())
        win.setHasShadow_(True)
        win.setReleasedWhenClosed_(False)
        win.setMovableByWindowBackground_(False)  # we handle drag ourselves
        win.setHidesOnDeactivate_(False)
        # Visible on every Space + alongside fullscreen apps. Stationary
        # keeps the badge from sliding around during Mission Control / Spaces
        # transitions.
        win.setCollectionBehavior_(
            NS_WINDOW_COLLECTION_CAN_JOIN_ALL_SPACES
            | NS_WINDOW_COLLECTION_STATIONARY
            | NS_WINDOW_COLLECTION_FULL_SCREEN_AUX
        )

        # Bento tiles: a transparent container holding 3 independent
        # NSVisualEffectView glass surfaces (one per bucket), with the
        # BadgeView drawing numerals + labels overlaid on top.
        container = NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, BADGE_WIDTH, BADGE_HEIGHT)
        )
        container.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)

        for i in range(NUM_TILES):
            tx = i * (TILE_SIZE + TILE_GAP)
            tile = NSVisualEffectView.alloc().initWithFrame_(
                NSMakeRect(tx, 0, TILE_SIZE, TILE_SIZE)
            )
            tile.setMaterial_(NSVisualEffectMaterialPopover)
            tile.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
            tile.setState_(NSVisualEffectStateActive)
            tile.setWantsLayer_(True)
            t_layer = tile.layer()
            if t_layer is not None:
                t_layer.setCornerRadius_(TILE_CORNER)
                t_layer.setMasksToBounds_(True)
                # Hairline inner border on each tile for definition.
                t_layer.setBorderWidth_(0.5)
                # CGColor of separatorColor at ~50% alpha.
                t_layer.setBorderColor_(
                    NSColor.separatorColor()
                    .colorWithAlphaComponent_(0.5).CGColor()
                )
            container.addSubview_(tile)

        # Foreground BadgeView (transparent) overlaid on the tiles —
        # draws numerals + labels, captures click/drag for the whole
        # badge regardless of which tile the user pressed.
        view = BadgeView.alloc().initWithFrame_(
            NSMakeRect(0, 0, BADGE_WIDTH, BADGE_HEIGHT)
        )
        view.set_controller(self)
        view.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        container.addSubview_(view)

        win.setContentView_(container)

        self.badge_window = win
        self.badge_view = view

        # NSPopover anchored to the badge view. Created up-front so we
        # don't pay setup cost on first click. Behavior=Transient should
        # auto-dismiss on outside clicks — but when the host is a
        # non-activating panel, NSPopover doesn't always detect those
        # outside clicks. We install a global NSEvent monitor in
        # togglePanel_ to close the popover on any click anywhere else
        # on screen as a robust fallback.
        self.popover = NSPopover.alloc().init()
        self.popover.setBehavior_(NSPopoverBehaviorTransient)
        self.popover.setAnimates_(True)
        # BadgeController is the popover's delegate so we get
        # popoverDidClose: callbacks and can tear down the event
        # monitor whenever the popover closes (for any reason).
        self.popover.setDelegate_(self)
        popover_vc = PopoverVC.alloc().init()
        # Hand the VC a reference to the popover so it can resize itself
        # when the user toggles between list and kanban.
        popover_vc.set_popover(self.popover)
        self.popover.setContentViewController_(popover_vc)
        # Initial content size matches the saved popover mode (and
        # widens to fit a 4th dormant column when show_dormant is on).
        initial_mode = _read_popover_mode()
        initial_show_dormant = _read_show_dormant()
        if initial_mode == "kanban":
            init_size = (
                POPOVER_KANBAN_WITH_DORMANT_SIZE
                if initial_show_dormant else POPOVER_KANBAN_SIZE
            )
        else:
            init_size = POPOVER_LIST_SIZE
        self.popover.setContentSize_(NSMakeSize(*init_size))
        self.outside_click_monitor = None

        # Remember the user's preferred mode for popover content (the
        # popover renders a vertical list — kanban as a popover doesn't
        # make sense at this size, so we ignore the kanban flag here).
        self._panel_mode = panel_mode

        # Install a tiny app menu so ⌘Q works.
        self._install_app_menu()

        # Initial paint + timer.
        self.refresh_(None)
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            REFRESH_SECS, self, "refresh:", None, True
        )
        return self

    def _install_app_menu(self):
        main = NSMenu.alloc().init()
        app_item = NSMenuItem.alloc().init()
        main.addItem_(app_item)
        app_menu = NSMenu.alloc().init()
        toggle = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Toggle Panel", "togglePanel:", "p"
        )
        toggle.setTarget_(self)
        app_menu.addItem_(toggle)
        app_menu.addItem_(NSMenuItem.separatorItem())
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit", "terminate:", "q"
        )
        app_menu.addItem_(quit_item)
        app_item.setSubmenu_(app_menu)
        NSApp.setMainMenu_(main)

    @objc.python_method
    def _counts(self) -> dict:
        try:
            buckets = _get_buckets()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[badge] buckets failed: {e!r}\n")
            return {"needs": 0, "working": 0, "ready": 0, "dormant": 0}
        return {k: len(v) for k, v in buckets.items()}

    def refresh_(self, _sender):
        if QUIT_FLAG.exists():
            try:
                QUIT_FLAG.unlink(missing_ok=True)
            except OSError:
                pass
            f = self.badge_window.frame()
            _save_badge_origin(float(f.origin.x), float(f.origin.y))
            # Tear down the recurring timer + outside-click monitor so
            # the controller can be released cleanly during terminate_.
            if self.timer is not None:
                try:
                    self.timer.invalidate()
                except Exception:  # noqa: BLE001
                    pass
                self.timer = None
            self._stop_outside_click_monitor()
            try:
                PID_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            NSApp.terminate_(None)
            return
        # Update badge counts.
        self.badge_view.set_counts(self._counts())
        # If popover is open, re-render its content too. Guard against
        # the case where the popover is mid-dismissal — `isShown()`
        # returns True briefly after `close()` while the animation runs,
        # but the contentViewController's text view may already be torn
        # down. The hasattr + try/except below absorbs that.
        if self.popover is not None and self.popover.isShown():
            vc = self.popover.contentViewController()
            if vc is not None and hasattr(vc, "refresh"):
                try:
                    vc.refresh()
                except Exception as e:  # noqa: BLE001
                    sys.stderr.write(f"[badge.refresh -> vc.refresh] {e!r}\n")

    def togglePanel_(self, _sender):
        """Show/hide the NSPopover anchored to the badge."""
        try:
            if self.popover is None:
                return
            if self.popover.isShown():
                # `close` is synchronous and bypasses the delegate's
                # `popoverShouldClose:` check (we don't have a delegate,
                # so they're equivalent in practice — but `close` makes
                # isShown() flip to False immediately).
                self.popover.close()
                return
            vc = self.popover.contentViewController()
            # Force loadView before refresh — accessing `.view()` triggers
            # NSViewController's loadView, which populates `vc.stack`.
            # Without this, the first click produced an empty popover.
            if vc is not None:
                _ = vc.view()
                if hasattr(vc, "refresh"):
                    try:
                        vc.refresh()
                    except Exception as e:  # noqa: BLE001
                        sys.stderr.write(f"[popover.refresh] {e!r}\n")
                        import traceback
                        traceback.print_exc(file=sys.stderr)
            anchor_rect = self.badge_view.bounds()
            self.popover.showRelativeToRect_ofView_preferredEdge_(
                anchor_rect, self.badge_view, NSRectEdgeMinY,
            )
            # Install the global click monitor so any click outside our
            # popover (in any other app or on the desktop) dismisses it.
            self._start_outside_click_monitor()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[togglePanel] {e!r}\n")
            import traceback
            traceback.print_exc(file=sys.stderr)

    @objc.python_method
    def _start_outside_click_monitor(self):
        """Listen globally for mouse clicks in OTHER apps — fire close()
        when one happens. Global monitors don't see events for our own
        app, so clicks on the badge / inside the popover are not caught
        here (good — those are handled by their own event paths).

        Always tears down any previous monitor before installing a new
        one. Two installs without a teardown leak a global event tap
        and have historically been the source of intermittent flicker
        on rapid open→close→open sequences."""
        self._stop_outside_click_monitor()
        try:
            # 1<<1 = NSEventMaskLeftMouseDown, 1<<3 = NSEventMaskRightMouseDown.
            mask = (1 << 1) | (1 << 3)
            self.outside_click_monitor = (
                NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                    mask, self._handle_outside_click,
                )
            )
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[outside_click_monitor.start] {e!r}\n")

    @objc.python_method
    def _stop_outside_click_monitor(self):
        if self.outside_click_monitor is None:
            return
        try:
            NSEvent.removeMonitor_(self.outside_click_monitor)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[outside_click_monitor.stop] {e!r}\n")
        self.outside_click_monitor = None

    @objc.python_method
    def _handle_outside_click(self, _event):
        """Global monitor callback — close the popover on any click in
        another app or the desktop background.

        Defers `close()` to the next runloop turn via performSelector
        with delay=0, because closing the popover synchronously from
        inside a global event handler has historically (10.14–11.x)
        produced crashes when NSPopoverBehaviorTransient's own dismissal
        logic fires on the same event."""
        if self.popover is not None and self.popover.isShown():
            try:
                self.performSelector_withObject_afterDelay_(
                    "deferredClosePopover:", None, 0.0,
                )
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"[outside_click_monitor.handler] {e!r}\n")

    def deferredClosePopover_(self, _sender):
        """Called on the next runloop tick from the click monitor."""
        if self.popover is not None and self.popover.isShown():
            try:
                self.popover.close()
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"[deferredClosePopover] {e!r}\n")

    # ---- NSPopoverDelegate ----
    def popoverDidClose_(self, _notification):
        """Always tear down the click monitor when the popover closes,
        no matter how it was dismissed (badge click, Esc, click outside,
        or programmatically)."""
        self._stop_outside_click_monitor()


# ---------- Entry ----------
def _quit_existing() -> int:
    """Ask the running panel to shut down by writing the quit-flag file.
    The panel's refresh tick picks it up within `REFRESH_SECS`."""
    if not PID_FILE.exists():
        print("No running floating panel.")
        return 0
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)  # alive check
    except (OSError, ValueError):
        print("Panel pidfile is stale; removing.")
        try:
            PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return 0
    try:
        QUIT_FLAG.touch()
        print(f"Asked floating panel (pid {pid}) to quit. "
              f"It will exit on its next refresh tick (≤{int(REFRESH_SECS)}s).")
    except OSError as e:
        print(f"Couldn't create quit flag: {e}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="claude-sessions-status panel")
    parser.add_argument("--kanban", action="store_true", help="Open in kanban mode")
    parser.add_argument("--list", dest="list_mode", action="store_true",
                        help="Open in vertical-list mode (default)")
    parser.add_argument("--badge", action="store_true",
                        help="Run in badge mode: a small always-visible icon "
                             "you click to toggle the detail panel.")
    parser.add_argument("--quit", action="store_true",
                        help="Quit any running floating panel/badge and exit")
    args = parser.parse_args()

    if args.quit:
        return _quit_existing()

    # Prevent two panels at once.
    if PID_FILE.exists():
        try:
            existing = int(PID_FILE.read_text().strip())
            os.kill(existing, 0)  # alive check
            sys.stderr.write(
                f"Another floating panel is already running (pid {existing}).\n"
                "Quit it first with `claude-sessions-status panel --quit`.\n"
            )
            return 1
        except (OSError, ValueError):
            PID_FILE.unlink(missing_ok=True)

    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass

    # Clean any stale quit flag from a previous run so we don't quit
    # immediately on the first tick.
    try:
        QUIT_FLAG.unlink(missing_ok=True)
    except OSError:
        pass

    # Decide layout mode: CLI flag > saved preference > default 'list'.
    if args.kanban:
        mode = "kanban"
    elif args.list_mode:
        mode = "list"
    else:
        mode = _read_mode()
    _write_mode(mode)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    if args.badge:
        # Badge mode: the small circular floating icon. Clicking it
        # toggles the detail panel, which uses `mode` (list/kanban).
        controller = BadgeController.alloc().initWithPanelMode_(mode)
        controller.badge_window.orderFrontRegardless()
    else:
        controller = PanelController.alloc().initWithMode_(mode)
        controller.panel.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
