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
  .claude-sessions-status-badge-style    floating-button shape (right-click menu)

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
    pid_files_present,
    recent_sessions,
    resolve_title,
    runtime_status_for,
    session_gist,
    session_runtime_status,
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
# Kanban mode includes the 200px slim sidebar (Idea 2.α from the
# canonical mockup) as fixed chrome on the left, so each kanban
# size grows by 200px relative to the pre-sidebar baseline. List
# mode is too narrow to host the sidebar — CSS hides it there.
POPOVER_KANBAN_SIZE = (920.0, 480.0)
# When the user toggles "Show older" in kanban mode, the dormant
# sessions render as a 4th column on the right. The popover widens
# to give that column room without squeezing the other three.
POPOVER_KANBAN_WITH_DORMANT_SIZE = (1140.0, 480.0)
# Opening the right-side tasks drawer adds another 280-px track to
# the body grid. The popover grows by that amount instead of
# shrinking the kanban — same UX pattern as "Show older" — so the
# kanban + sidebar widths stay constant whether the drawer is open
# or closed. Drawer-open composes with show_dormant: both add
# their own 280 / 220 px when active.
DRAWER_WIDTH_PX = 280.0
POPOVER_KANBAN_WITH_DRAWER_SIZE = (
    POPOVER_KANBAN_SIZE[0] + DRAWER_WIDTH_PX, POPOVER_KANBAN_SIZE[1])
POPOVER_KANBAN_WITH_DORMANT_AND_DRAWER_SIZE = (
    POPOVER_KANBAN_WITH_DORMANT_SIZE[0] + DRAWER_WIDTH_PX,
    POPOVER_KANBAN_WITH_DORMANT_SIZE[1])
# Persist drawer-open state Python-side (mirrors the popover-mode
# file pattern) so the popover opens at the correct size on the
# very first render. Without this, the popover would briefly flash
# at the closed size before JS read localStorage and asked Python
# to grow it — visible jank on every open.
DRAWER_OPEN_FILE = HOME / ".claude-sessions-status-drawer-open"


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


def _read_drawer_open() -> bool:
    """Read persisted drawer-open flag. Defaults to False (closed) on
    missing / malformed file so first-time users see a clean popover
    without the right-side panel."""
    try:
        return DRAWER_OPEN_FILE.read_text(encoding="utf-8").strip() == "1"
    except OSError:
        return False


def _write_drawer_open(open_: bool) -> None:
    _atomic_write_text(DRAWER_OPEN_FILE, "1" if open_ else "0")


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


# ---------- New-session launch config ----------
# Options the user picks in the "Start a new Claude session" modal before
# launching: permission/auto mode, model, and whether to continue the
# folder's most recent session. Persisted globally (last-used) so the
# modal reopens pre-filled with the previous choice, and turned into
# `claude` CLI flags at spawn time. JSON because it's a small heterogenous
# dict (string + string + bool), unlike the plain-text single-value prefs.
START_CONFIG_FILE = HOME / ".claude-sessions-status-start-config.json"
START_PERM_MODES = ("acceptEdits", "plan", "auto")
START_SESSION_TARGETS = ("new", "existing")
START_LOCATIONS = ("same", "worktree")
START_CONFIG_DEFAULT = {
    # First-run seed only — the modal pre-selects the user's last choice.
    # An older saved "default" (no-flag) value migrates here via _coerce.
    "permissionMode": "acceptEdits",
    "sessionTarget": "new",
    "location": "same",
}


def _coerce_start_config(raw: dict) -> dict:
    """Validate an arbitrary dict into a clean start-config, dropping
    anything unrecognized back to its default. Shared by load + save so
    a corrupt file and a malformed JS payload both normalize the same way.
    Also migrates the old schema's ``continueSession`` flag to the new
    ``sessionTarget`` field."""
    cfg = dict(START_CONFIG_DEFAULT)
    if isinstance(raw, dict):
        pm = str(raw.get("permissionMode") or "").strip()
        if pm == "bypass":   # old value for the Auto pill
            pm = "auto"
        if pm in START_PERM_MODES:
            cfg["permissionMode"] = pm
        st = str(raw.get("sessionTarget") or "").strip()
        if st in START_SESSION_TARGETS:
            cfg["sessionTarget"] = st
        elif raw.get("continueSession"):
            cfg["sessionTarget"] = "existing"
        loc = str(raw.get("location") or "").strip()
        if loc in START_LOCATIONS:
            cfg["location"] = loc
    return cfg


def _load_start_config() -> dict:
    try:
        raw = json.loads(START_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(START_CONFIG_DEFAULT)
    return _coerce_start_config(raw)


def _save_start_config(cfg: dict) -> None:
    try:
        _atomic_write_text(
            START_CONFIG_FILE,
            json.dumps(_coerce_start_config(cfg), indent=2),
        )
    except (TypeError, ValueError) as e:  # noqa: BLE001
        sys.stderr.write(f"[start-config save] {e!r}\n")


def _start_config_flags(cfg: dict) -> list[str]:
    """Turn a start-config dict into the `claude` CLI argv flags that
    realize it. Each permission mode (acceptEdits / plan / auto) maps to
    ``--permission-mode <mode>``. An "existing" session target resumes a
    specific past session via --resume <id> when ``resumeSessionId`` is
    given, else falls back to the folder's most recent session via
    --continue; "new" (the default) starts fresh."""
    flags: list[str] = []
    pm = cfg.get("permissionMode", "acceptEdits")
    if pm in ("acceptEdits", "plan", "auto"):
        flags += ["--permission-mode", pm]
    if cfg.get("sessionTarget") == "existing":
        rid = str(cfg.get("resumeSessionId") or "").strip()
        if rid:
            flags += ["--resume", rid]
        else:
            flags.append("--continue")
    return flags


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


def _install_edit_menu(main) -> None:
    """Append a standard Edit menu (Cut/Copy/Paste/Select All) to `main`.

    An accessory/LSUIElement app has no system-provided menu bar, so the
    ⌘X/⌘C/⌘V/⌘A key equivalents that AppKit relies on to turn keystrokes
    into cut:/copy:/paste:/selectAll: actions don't exist unless we add
    them. Without this menu, text fields and WKWebView inputs accept typing
    but silently ignore paste. The items carry no target (nil) on purpose,
    so each action travels the responder chain to whatever is focused."""
    edit_item = NSMenuItem.alloc().init()
    main.addItem_(edit_item)
    edit_menu = NSMenu.alloc().initWithTitle_("Edit")
    for title, action, key in (
        ("Cut", "cut:", "x"),
        ("Copy", "copy:", "c"),
        ("Paste", "paste:", "v"),
        ("Select All", "selectAll:", "a"),
    ):
        it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        edit_menu.addItem_(it)  # no setTarget_ -> nil -> responder chain
    edit_item.setSubmenu_(edit_menu)


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


# ---------- Proactive NEEDS-YOU notifications ----------
# Opt-in. When a session transitions into the NEEDS YOU bucket, the badge
# can fire a one-shot macOS notification + subtle sound + a brief visual
# pulse — turning the badge from "a thing I glance at" into "a thing that
# tells me." Off by default; toggled from the badge's right-click menu.
#
# Transition detection reuses the same epoch-comparison idea as the
# unread/seen machinery: we remember the lastTurnEpoch we last notified
# about per session, and only fire when a *newer* turn lands a session in
# NEEDS YOU. A session that simply sits in NEEDS YOU keeps the same epoch
# tick after tick, so it never re-notifies — that's the debounce.
NOTIFY_ENABLED_FILE = HOME / ".claude-sessions-status-notify"
NOTIFY_STATE_FILE = HOME / ".claude-sessions-status-notify-state.json"
_NOTIFY_GC_TTL_S = 30 * 86400.0  # 30 days — same horizon as seen GC
# Slack to absorb millisecond epoch drift between ticks (matches the
# 0.5s used by _is_session_unread).
_NOTIFY_EPSILON_S = 0.5


def _read_notify_enabled() -> bool:
    """Whether proactive NEEDS-YOU notifications are on. Opt-in: defaults
    to False so a fresh install is silent until the user asks for it."""
    try:
        v = NOTIFY_ENABLED_FILE.read_text(encoding="utf-8").strip().lower()
        return v in ("1", "true", "yes", "on")
    except OSError:
        return False


def _write_notify_enabled(value: bool) -> None:
    _atomic_write_text(NOTIFY_ENABLED_FILE, "1" if value else "0")


def _load_notify_state() -> dict:
    """Map session-id -> {"epoch": lastNotifiedTurnEpoch, "at": wallclock}.
    Persisted so a badge restart doesn't re-announce sessions we already
    notified about."""
    if not NOTIFY_STATE_FILE.exists():
        return {}
    try:
        data = json.loads(NOTIFY_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_notify_state(state: dict) -> None:
    cutoff = time.time() - _NOTIFY_GC_TTL_S
    out: dict = {}
    for sid, entry in state.items():
        if not isinstance(entry, dict):
            continue
        at = entry.get("at")
        if isinstance(at, (int, float)) and at >= cutoff:
            out[sid] = entry
    _atomic_write_text(
        NOTIFY_STATE_FILE, json.dumps(out, indent=2, sort_keys=True))


def _osa_quote(s: str) -> str:
    """Quote a Python string as an AppleScript string literal."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _post_notification(title: str, body: str, *, subtitle: str = "",
                       sound: bool = True) -> None:
    """Fire a macOS Notification Center banner via osascript. Detached
    (Popen, no wait) so it never blocks the badge's 5s refresh runloop."""
    parts = [
        "display notification", _osa_quote(body),
        "with title", _osa_quote(title),
    ]
    if subtitle:
        parts += ["subtitle", _osa_quote(subtitle)]
    if sound:
        # A soft, short system sound — present on every macOS install.
        parts += ["sound name", _osa_quote("Tink")]
    script = " ".join(parts)
    try:
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as e:  # noqa: BLE001
        sys.stderr.write(f"[notify] osascript failed: {e!r}\n")


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
            # `comm=` truncates to ~16 chars on macOS, so a path like
            # '/System/Applications/Utilities/Terminal.app/.../Terminal'
            # comes back as '/System/Applicat' and never matches. Use the
            # untruncated `command=` and match against just the executable
            # path (its first whitespace-delimited token) so process
            # arguments can't trigger false positives. ppid goes first so
            # we can peel it off the front before the (space-containing)
            # command.
            r = subprocess.run(
                ["ps", "-p", str(cur), "-o", "ppid=,command="],
                capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        line = r.stdout.strip()
        if not line:
            return None
        parts = line.split(None, 1)
        if not parts:
            return None
        ppid_str = parts[0]
        command = parts[1] if len(parts) > 1 else ""
        exe = command.split(None, 1)[0] if command else ""
        for needle, app in _KNOWN_TERMINAL_PROCS.items():
            if needle in exe:
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
    `sessionId` matches. The host kind is decided by the process's real
    controlling TTY, not the `entrypoint` field — the CLI bundled with
    Claude Desktop records entrypoint="claude-desktop" even when launched
    from a shell, so entrypoint alone misroutes terminal sessions.
    Returns:
      {kind: "claude-desktop", pid: int}
        — headless inside the Claude Desktop app (no controlling TTY)
      {kind: "terminal", pid: int, tty: str, terminal_app: str}
        — the session owns a TTY in a terminal emulator
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
        # The controlling TTY — not `entrypoint` — is the reliable host
        # signal. The `claude` CLI binary that ships *inside* Claude
        # Desktop writes entrypoint="claude-desktop" even when the user
        # launched it from a terminal shell, so a terminal session would
        # otherwise be misrouted to "activate Claude Desktop". A real
        # TTY (e.g. 'ttys000') means a terminal emulator owns the
        # process; '??' / empty means it's running headless inside
        # Claude Desktop.
        tty = _tty_for_pid(pid)
        is_real_tty = bool(tty) and tty not in ("?", "??")
        if is_real_tty:
            return {
                "kind": "terminal",
                "pid": pid,
                "tty": tty,
                "terminal_app": _find_terminal_ancestor(pid),
            }
        # No controlling TTY — genuinely headless. Trust entrypoint, and
        # treat a Claude Desktop ancestor (no terminal in the chain) the
        # same way.
        entrypoint = data.get("entrypoint", "")
        ancestor = _find_terminal_ancestor(pid)
        if entrypoint == "claude-desktop" or ancestor is None:
            return {"kind": "claude-desktop", "pid": pid}
        # Has a terminal ancestor but no tty yet (rare startup race) —
        # fall back to treating it as a terminal session.
        return {
            "kind": "terminal",
            "pid": pid,
            "tty": tty,
            "terminal_app": ancestor,
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
    status_map = session_runtime_status()
    tracking = pid_files_present()
    seen = _load_seen()

    buckets: dict[str, list[dict]] = {b: [] for b in BUCKET_ORDER}
    for s in sessions:
        full_path = s.get("fullPath") or ""
        meta = transcript_meta(full_path)
        last_epoch = meta.get("lastTurnEpoch")
        if last_epoch is None:
            last_epoch = s.get("fileMtime", 0) / 1000
        ago_s = now - last_epoch
        sid = s.get("sessionId") or ""
        runtime_status = runtime_status_for(sid, live_ids, status_map, tracking)
        emoji, phase_label = infer_phase(meta)
        state, _ = state_for(meta, ago_s, runtime_status)
        active_bucket = _classify(state, phase_label, runtime_status)
        bucket = "dormant" if is_dormant(sid, ago_s, live_ids, active_bucket) else active_bucket
        subs = subagents_for_session(full_path, now)
        sub_sum = subagent_summary(subs)
        # Promote to WORKING when any sub-agent is actively running, even
        # if the parent transcript looks idle or dormant. Mirrors the
        # override in dashboard.py:_prepare_row.
        if sub_sum.get("running"):
            bucket = "working"
            state = "Working…"
        # When the parent's most recent assistant turn dispatched a Task
        # (sub-agent), align the displayed state with the WORKING bucket
        # that `classify` already chose. Without this, the card shows
        # "Waiting on you" (the state_for output for any assistant entry
        # ending in text) under the WORKING column — visually confusing.
        # Triggers even when `sub_sum.running == 0`, which happens during
        # long quiet stretches inside a sub-agent (no jsonl write for
        # >60s) — the parent is still genuinely waiting on its child.
        elif phase_label == "Awaiting sub-agent" and bucket == "working":
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
        _install_edit_menu(main)
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


# ---------- Badge style (user-selectable floating-button shape) ----------
# The badge can render in one of several shapes. "bento" is the original
# three-glass-tile look; the others adopt the expanded popover's visual
# language (a unified dark surface #1c1c20, hairline borders, muted
# red/amber/green, SF Mono tabular numerals, dot markers). The choice
# persists across launches and is changed via the badge's right-click
# settings menu (BadgeController.badge_settings_menu).
BADGE_STYLE_FILE = HOME / ".claude-sessions-status-badge-style"
BADGE_STYLES = ("bento", "pill", "header", "card", "dots")
BADGE_STYLE_LABELS = {
    "bento":  "Bento tiles (glass)",
    "pill":   "Unified pill",
    "header": "Kanban header",
    "card":   "Card of chips",
    "dots":   "Compact dots",
}
# Per-style borderless-window dimensions (w, h) in points.
_BADGE_DIMS = {
    "bento":  (BADGE_WIDTH, BADGE_HEIGHT),  # 180 x 56
    "pill":   (156.0, 40.0),
    "header": (228.0, 52.0),
    "card":   (176.0, 40.0),
    "dots":   (150.0, 36.0),
}
# Muted bucket colors lifted verbatim from the popover (kanban.html) so
# the flat badge styles match the expanded window exactly.
FLAT_BUCKET_HEX = {
    "needs":   "#f06f6f",
    "working": "#e0b34a",
    "ready":   "#4fc78a",
}
# Short uppercase labels for the "header" style.
FLAT_BUCKET_LABEL = {
    "needs":   "NEEDS",
    "working": "WORKING",
    "ready":   "DONE",
}
_BADGE_SURFACE_HEX = "#1c1c20"   # --bg-popover
_BADGE_LIFT_HEX = "#232328"      # --bg-popover-lift


def _read_badge_style() -> str:
    try:
        v = BADGE_STYLE_FILE.read_text(encoding="utf-8").strip().lower()
        if v in BADGE_STYLES:
            return v
    except OSError:
        pass
    return "bento"


def _write_badge_style(value: str) -> None:
    if value not in BADGE_STYLES:
        return
    _atomic_write_text(BADGE_STYLE_FILE, value)


def _badge_dims(style: str) -> tuple[float, float]:
    return _BADGE_DIMS.get(style, _BADGE_DIMS["bento"])


def _flat_tint(key: str):
    return _hex_to_nscolor(FLAT_BUCKET_HEX.get(key, "#8a8a8a"))


def _white_alpha(a: float):
    return NSColor.colorWithRed_green_blue_alpha_(1.0, 1.0, 1.0, a)


def _mono_tabular_font(size: float, weight: float):
    """SF Mono / monospaced system font with tabular figures — matches the
    popover's count typography. Falls back to the rounded tabular font on
    very old macOS where monospacedSystemFontOfSize_weight_ is missing."""
    try:
        f = NSFont.monospacedSystemFontOfSize_weight_(size, weight)
        if f is not None:
            return f
    except Exception:  # noqa: BLE001
        pass
    return _rounded_tabular_font(size, weight)


# ---- Lightweight drawing primitives shared by the flat badge styles ----
def _fill_round_rect(rect, radius, color):
    path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        rect, radius, radius)
    color.setFill()
    path.fill()


def _stroke_round_rect(rect, radius, line_w, color):
    # Inset by half the line width so the stroke stays inside `rect`.
    inset = NSMakeRect(
        rect.origin.x + line_w / 2.0, rect.origin.y + line_w / 2.0,
        rect.size.width - line_w, rect.size.height - line_w)
    path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        inset, radius, radius)
    path.setLineWidth_(line_w)
    color.setStroke()
    path.stroke()


def _fill_circle(cx, cy, diameter, color):
    rect = NSMakeRect(cx - diameter / 2.0, cy - diameter / 2.0,
                      diameter, diameter)
    path = NSBezierPath.bezierPathWithOvalInRect_(rect)
    color.setFill()
    path.fill()


def _badge_attr(text, font, color, kern=0.0):
    attrs = {
        NSFontAttributeName: font,
        NSForegroundColorAttributeName: color,
    }
    if kern:
        attrs[NSKernAttributeName] = kern
    return NSAttributedString.alloc().initWithString_attributes_(text, attrs)


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
    style = objc.ivar("style")             # one of BADGE_STYLES

    def initWithFrame_(self, frame):
        self = objc.super(BadgeView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.counts = {"needs": 0, "working": 0, "ready": 0, "dormant": 0}
        self.drag_anchor = None
        self.did_drag = False
        self.style = "bento"
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

    @objc.python_method
    def set_style(self, style) -> None:
        self.style = style if style in BADGE_STYLES else "bento"
        self.setNeedsDisplay_(True)

    def isFlipped(self):
        # Flipped coordinates make manual layout easier (origin top-left).
        return True

    def drawRect_(self, _dirty):
        # Dispatch to the per-style renderer. "bento" keeps the original
        # glass-tile look (its frosted background is provided by the
        # NSVisualEffectView tiles behind this view); the flat styles
        # draw their own unified dark surface to match the popover.
        try:
            style = self.style or "bento"
            if style == "pill":
                self._draw_pill()
            elif style == "header":
                self._draw_header()
            elif style == "card":
                self._draw_card()
            elif style == "dots":
                self._draw_dots()
            else:
                self._draw_bento()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[BadgeView.drawRect_] {e!r}\n")

    @objc.python_method
    def _draw_pill(self):
        """Option A — one popover-dark surface, hairline dividers between
        buckets, colored dot + SF Mono count per bucket."""
        b = self.bounds()
        w, h = b.size.width, b.size.height
        bg = NSMakeRect(0.5, 0.5, w - 1.0, h - 1.0)
        _fill_round_rect(bg, 12.0, _hex_to_nscolor(_BADGE_SURFACE_HEX))
        _stroke_round_rect(bg, 12.0, 1.0, _white_alpha(0.10))
        num_font = _mono_tabular_font(15.0, NSFontWeightMedium)
        seg_w = w / 3.0
        for i, key in enumerate(TILE_KEYS):
            n = int(self.counts.get(key, 0) or 0)
            seg_x = i * seg_w
            if i > 0:
                div = NSMakeRect(seg_x - 0.5, 9.0, 1.0, h - 18.0)
                _white_alpha(0.06).setFill()
                NSBezierPath.fillRect_(div)
            active = n > 0
            dot_c = _flat_tint(key) if active else _white_alpha(0.28)
            num_c = _white_alpha(0.92) if active else _white_alpha(0.30)
            s = _badge_attr(str(n), num_font, num_c)
            sz = s.size()
            dot_d, gap = 8.0, 7.0
            gw = dot_d + gap + sz.width
            gx = seg_x + (seg_w - gw) / 2.0
            cy = h / 2.0
            _fill_circle(gx + dot_d / 2.0, cy, dot_d, dot_c)
            s.drawAtPoint_(NSMakePoint(gx + dot_d + gap, cy - sz.height / 2.0))

    @objc.python_method
    def _draw_header(self):
        """Option B — mini kanban column-headers: a tiny uppercase label
        with a dot above an SF Mono count, per bucket."""
        b = self.bounds()
        w, h = b.size.width, b.size.height
        bg = NSMakeRect(0.5, 0.5, w - 1.0, h - 1.0)
        _fill_round_rect(bg, 11.0, _hex_to_nscolor(_BADGE_SURFACE_HEX))
        _stroke_round_rect(bg, 11.0, 1.0, _white_alpha(0.10))
        lbl_font = NSFont.systemFontOfSize_weight_(9.0, NSFontWeightSemibold)
        num_font = _mono_tabular_font(19.0, NSFontWeightMedium)
        seg_w = w / 3.0
        for i, key in enumerate(TILE_KEYS):
            n = int(self.counts.get(key, 0) or 0)
            seg_x = i * seg_w
            if i > 0:
                div = NSMakeRect(seg_x - 0.5, 10.0, 1.0, h - 20.0)
                _white_alpha(0.06).setFill()
                NSBezierPath.fillRect_(div)
            active = n > 0
            tint = _flat_tint(key) if active else _white_alpha(0.30)
            # Top row: dot + uppercase label (gray, letter-spaced).
            lbl = _badge_attr(
                FLAT_BUCKET_LABEL[key], lbl_font, _white_alpha(0.38), kern=0.7)
            lsz = lbl.size()
            dot_d, gap = 6.0, 5.0
            gw = dot_d + gap + lsz.width
            gx = seg_x + (seg_w - gw) / 2.0
            top_cy = 15.0
            _fill_circle(gx + dot_d / 2.0, top_cy, dot_d, tint)
            lbl.drawAtPoint_(
                NSMakePoint(gx + dot_d + gap, top_cy - lsz.height / 2.0))
            # Bottom row: count, colored (or gray when zero), centered.
            num = _badge_attr(str(n), num_font, tint)
            nsz = num.size()
            num.drawAtPoint_(NSMakePoint(seg_x + (seg_w - nsz.width) / 2.0, 28.0))

    @objc.python_method
    def _draw_card(self):
        """Option C — one lifted card holding three count chips, reusing the
        kanban card surface/border tokens."""
        b = self.bounds()
        w, h = b.size.width, b.size.height
        outer = NSMakeRect(0.5, 0.5, w - 1.0, h - 1.0)
        _fill_round_rect(outer, 8.0, _hex_to_nscolor(_BADGE_LIFT_HEX))
        _stroke_round_rect(outer, 8.0, 1.0, _white_alpha(0.05))
        pad, gap = 7.0, 4.0
        chip_w = (w - 2.0 * pad - 2.0 * gap) / 3.0
        chip_h = h - 2.0 * pad
        num_font = _mono_tabular_font(13.0, NSFontWeightMedium)
        for i, key in enumerate(TILE_KEYS):
            n = int(self.counts.get(key, 0) or 0)
            cx0 = pad + i * (chip_w + gap)
            chip = NSMakeRect(cx0, pad, chip_w, chip_h)
            active = n > 0
            _fill_round_rect(chip, 5.0, _white_alpha(0.03 if active else 0.02))
            _stroke_round_rect(chip, 5.0, 1.0, _white_alpha(0.05))
            dot_c = _flat_tint(key) if active else _white_alpha(0.28)
            num_c = _white_alpha(0.92) if active else _white_alpha(0.30)
            s = _badge_attr(str(n), num_font, num_c)
            sz = s.size()
            dot_d, ig = 8.0, 6.0
            gw = dot_d + ig + sz.width
            gx = cx0 + (chip_w - gw) / 2.0
            cy = h / 2.0
            _fill_circle(gx + dot_d / 2.0, cy, dot_d, dot_c)
            s.drawAtPoint_(NSMakePoint(gx + dot_d + ig, cy - sz.height / 2.0))

    @objc.python_method
    def _draw_dots(self):
        """Option D — smallest footprint: a capsule with dot + SF Mono count
        per bucket, no dividers."""
        b = self.bounds()
        w, h = b.size.width, b.size.height
        bg = NSMakeRect(0.5, 0.5, w - 1.0, h - 1.0)
        _fill_round_rect(bg, h / 2.0, _hex_to_nscolor(_BADGE_SURFACE_HEX))
        _stroke_round_rect(bg, h / 2.0, 1.0, _white_alpha(0.10))
        num_font = _mono_tabular_font(13.0, NSFontWeightMedium)
        dot_d, ig, seg_gap = 8.0, 6.0, 12.0
        groups = []
        for key in TILE_KEYS:
            n = int(self.counts.get(key, 0) or 0)
            s = _badge_attr(
                str(n), num_font, _white_alpha(0.92 if n > 0 else 0.30))
            sz = s.size()
            groups.append((key, n, s, sz, dot_d + ig + sz.width))
        total = sum(g[4] for g in groups) + seg_gap * (len(groups) - 1)
        x = (w - total) / 2.0
        cy = h / 2.0
        for key, n, s, sz, gw in groups:
            dot_c = _flat_tint(key) if n > 0 else _white_alpha(0.28)
            _fill_circle(x + dot_d / 2.0, cy, dot_d, dot_c)
            s.drawAtPoint_(NSMakePoint(x + dot_d + ig, cy - sz.height / 2.0))
            x += gw + seg_gap

    @objc.python_method
    def _draw_bento(self):
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
    def acceptsFirstMouse_(self, _event):
        """Fire on the very first click even when our app isn't front.
        Without this, the very next click on the badge after another
        app takes focus (e.g. Terminal, after a task's ▶ Start fires
        a new Claude session there) gets eaten by AppKit's default
        first-mouse behavior — the click is treated as "activate the
        window" only, mouseDown_ never runs, and the badge looks
        frozen / unresponsive. Returning True wires the first click
        directly to our handler. Safe because the badge's panel uses
        NSWindowStyleMaskNonactivatingPanel, so the click won't
        accidentally promote claude-sessions-status to the front app
        — it just delivers the event to us as expected."""
        return True

    def mouseDown_(self, event):
        # ⌃-click (control-click) opens the settings menu, same as a
        # right-click — the trackpad-friendly path.
        try:
            if event.modifierFlags() & (1 << 18):  # NSEventModifierFlagControl
                self.rightMouseDown_(event)
                return
        except Exception:  # noqa: BLE001
            pass
        win = self.window()
        if win is None:
            return
        mouse = NSEvent.mouseLocation()
        wf = win.frame()
        self.drag_anchor = (mouse.x, mouse.y, wf.origin.x, wf.origin.y)
        self.did_drag = False

    def rightMouseDown_(self, event):
        """Right-click (or ⌃-click) opens the badge settings menu, where
        the user picks the floating-button shape."""
        try:
            if self.controller is None:
                return
            menu = self.controller.badge_settings_menu()
            NSMenu.popUpContextMenu_withEvent_forView_(menu, event, self)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[badge.rightMouseDown_] {e!r}\n")

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
    dormant_btn = objc.ivar("dormant_btn")     # the "Show older" recessed pill
    density = objc.ivar("density")             # "glance" | "focus" | "detail"
    density_seg = objc.ivar("density_seg")     # legacy — kept for compat, unused
    mark_all_btn = objc.ivar("mark_all_btn")
    # Center mode-switch pills (replaced the NSSegmentedControl). Two
    # push-on-push-off NSButtons with mutex behavior implemented in
    # modeButtonClicked:. self.segmented is kept as None for any old
    # callers that look for it.
    mode_list_btn = objc.ivar("mode_list_btn")
    mode_kanban_btn = objc.ivar("mode_kanban_btn")
    # Drawer-related state. drawer_open mirrors ~/.claude-sessions-
    # status-drawer-open + the body.drawer-open class in JS; tasks_btn
    # is the native top-bar toggle (recessed-pill NSButton) that
    # replaces the floating HTML #drawer-toggle.
    drawer_open = objc.ivar("drawer_open")
    tasks_btn = objc.ivar("tasks_btn")
    # Settings gear (top-left). Opens the badge-style menu, which lives
    # on the BadgeController (it owns the badge window we restyle).
    settings_btn = objc.ivar("settings_btn")
    badge_controller = objc.ivar("badge_controller")

    @objc.python_method
    def set_popover(self, popover):
        """Allows BadgeController to hand us the NSPopover so we can
        resize it when the user toggles mode."""
        self.popover_ref = popover

    @objc.python_method
    def set_badge_controller(self, ctrl):
        """BadgeController hands us a back-reference so the settings gear
        can build + apply the badge-style menu (the controller owns the
        badge window)."""
        self.badge_controller = ctrl

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
        # Restore last-used mode + density + show_dormant + drawer_open.
        self.mode = _read_popover_mode()
        self.density = _read_density()
        self.show_dormant = _read_show_dormant()
        # Persisted drawer-open flag (~/.claude-sessions-status-drawer-
        # open). Drawer-open grows the popover by DRAWER_WIDTH_PX so
        # the kanban + sidebar widths stay constant; reading it here
        # before the initial sizing avoids a visible "snap wider" on
        # the first render when the user had the drawer open.
        self.drawer_open = _read_drawer_open()
        # Initial popover size — accounts for mode + show_dormant +
        # drawer_open in one shot via the same helper used everywhere
        # else (single source of truth).
        size = self._target_popover_size()
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

        # ---- Top bar: every control uses the same recessed-pill style ----
        # The dark-popover toolbar (top 32-px strip) hosts FOUR control
        # groups, all rendered as NSBezelStyleRecessed NSButtons so
        # they read as one consistent design language:
        #   • Center: List / Kanban  — mutex pair (push-on-push-off,
        #     two pills with mutually-exclusive selection)
        #   • Right:  Tasks · N      — push-on-push-off toggle
        #             Show older     — push-on-push-off toggle
        #   • Right(L): Mark all N read — momentary push (briefly
        #     flashes, no sticky state)
        # The previous Glance/Focus/Detail density popup at the top-left
        # was removed entirely in this pass — the user can no longer
        # change density from the UI, but the persisted value still
        # drives rendering.
        TOP_BAR_HEIGHT = 32.0
        NS_BUTTON_TYPE_MOMENTARY_LIGHT = 0
        NS_BUTTON_TYPE_PUSH_ON_PUSH_OFF = 1
        NS_BEZEL_STYLE_RECESSED = 13
        TOGGLE_GAP_PX = 6.0
        toggle_font = NSFont.systemFontOfSize_(11.0)

        def _make_toolbar_toggle(title, initial_on, action_sel,
                                 button_type=NS_BUTTON_TYPE_PUSH_ON_PUSH_OFF):
            # _FirstMouseButton overrides acceptsFirstMouse: so the
            # very first click after the popover opens fires the
            # action — without this, NSPopover eats the first click
            # because its window isn't yet key.
            btn = _FirstMouseButton.alloc().init()
            btn.setButtonType_(button_type)
            btn.setBezelStyle_(NS_BEZEL_STYLE_RECESSED)
            btn.setFont_(toggle_font)
            btn.setTitle_(title)
            btn.setState_(1 if initial_on else 0)
            btn.setTarget_(self)
            btn.setAction_(action_sel)
            # Recessed-style buttons need an explicit content tint to
            # look right on the dark popover; otherwise the "off" state
            # text reads almost invisibly. labelColor adapts to system
            # appearance so this stays correct in both light/dark.
            if hasattr(btn, "setContentTintColor_"):
                btn.setContentTintColor_(NSColor.labelColor())
            btn.sizeToFit()
            return btn

        # List | Kanban — mode picker. NSSegmentedControl is the
        # macOS-native one-of-N control; using it (rather than two
        # standalone pills) communicates the radio semantics visually
        # and keeps state mutex correct without any JS scaffolding.
        # NSSegmentStyleSeparated (= 8) renders each segment as a
        # discrete recessed pill — visually consistent with the
        # other recessed-bezel buttons on the right edge.
        NS_SEGMENT_STYLE_SEPARATED = 8
        seg = NSSegmentedControl.alloc().init()
        seg.setSegmentCount_(2)
        seg.setLabel_forSegment_("List", 0)
        seg.setLabel_forSegment_("Kanban", 1)
        seg.setSegmentStyle_(NS_SEGMENT_STYLE_SEPARATED)
        seg.setTrackingMode_(NSSegmentSwitchTrackingSelectOne)
        seg.setSelectedSegment_(0 if self.mode == "list" else 1)
        seg.setTarget_(self)
        seg.setAction_("segmentChanged:")
        seg.setFont_(toggle_font)
        # Width: let it size to its content, then center horizontally.
        seg.sizeToFit()
        sf = seg.frame()
        seg.setFrame_(NSMakeRect(
            (w - sf.size.width) / 2.0, h - TOP_BAR_HEIGHT + 4,
            sf.size.width, sf.size.height,
        ))
        seg.setAutoresizingMask_(
            NSViewMinXMargin | NSViewMaxXMargin | NSViewMinYMargin)
        container.addSubview_(seg)
        self.segmented = seg
        # The two-pill mutex experiment (mode_list_btn / mode_kanban_btn)
        # is retired — kept as None so any stale wiring no-ops safely.
        self.mode_list_btn = None
        self.mode_kanban_btn = None

        # ---- Top bar (left): settings gear ----
        # Momentary recessed button showing a gear glyph; clicking opens
        # the badge-style menu (same one as right-clicking the badge).
        # Anchored to the left edge so it never collides with the
        # right-side toggles or the centered mode picker.
        gear = _FirstMouseButton.alloc().init()
        gear.setButtonType_(NS_BUTTON_TYPE_MOMENTARY_LIGHT)
        gear.setBezelStyle_(NS_BEZEL_STYLE_RECESSED)
        gear.setTarget_(self)
        gear.setAction_("showStyleMenu:")
        gear_img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "gearshape", "Floating button settings")
        if gear_img is not None:
            gear.setImage_(gear_img)
            gear.setImagePosition_(1)  # NSImageOnly
        else:
            gear.setTitle_("Style")
            gear.setFont_(toggle_font)
        if hasattr(gear, "setContentTintColor_"):
            gear.setContentTintColor_(NSColor.labelColor())
        gear.setToolTip_("Floating button style")
        gear.sizeToFit()
        gf = gear.frame()
        gear.setFrame_(NSMakeRect(
            10.0, h - TOP_BAR_HEIGHT + 4,
            max(gf.size.width, 28.0), gf.size.height,
        ))
        gear.setAutoresizingMask_(NSViewMaxXMargin | NSViewMinYMargin)
        container.addSubview_(gear)
        self.settings_btn = gear

        dormant_btn = _make_toolbar_toggle(
            "Show older", self.show_dormant, "toggleDormant:")
        # Right-anchor the dormant pill 12px from the popover edge.
        dbf = dormant_btn.frame()
        dormant_btn.setFrame_(NSMakeRect(
            w - dbf.size.width - 12,
            h - TOP_BAR_HEIGHT + 4,
            dbf.size.width, dbf.size.height,
        ))
        dormant_btn.setAutoresizingMask_(NSViewMinXMargin | NSViewMinYMargin)
        container.addSubview_(dormant_btn)
        self.dormant_btn = dormant_btn

        # Tasks pill — second toolbar toggle, sits to the LEFT of
        # Show older with TOGGLE_GAP_PX of breathing room so the two
        # read as a button group. Title gets a "· N" count suffix on
        # each refresh (see _update_tasks_btn_count) when there are
        # open tasks, so the user has a glanceable signal even when
        # the drawer is closed.
        tasks_btn = _make_toolbar_toggle(
            "Tasks", self.drawer_open, "toggleTasksDrawer:")
        tbf = tasks_btn.frame()
        tasks_btn.setFrame_(NSMakeRect(
            w - dbf.size.width - 12 - TOGGLE_GAP_PX - tbf.size.width,
            h - TOP_BAR_HEIGHT + 4,
            tbf.size.width, tbf.size.height,
        ))
        tasks_btn.setAutoresizingMask_(NSViewMinXMargin | NSViewMinYMargin)
        # List mode has no drawer (popover is too narrow for it) so
        # the Tasks pill stays hidden there. Visibility is also
        # refreshed in _update_tasks_btn_count on every render.
        tasks_btn.setHidden_(self.mode == "list")
        container.addSubview_(tasks_btn)
        self.tasks_btn = tasks_btn

        # ---- Top bar (right, kanban only): "Mark all N read" pill ----
        # Same recessed-bezel look as the other top-bar controls, but
        # MOMENTARY (button-type 0) — clicking fires the action and
        # the pill returns to its un-pressed state immediately, since
        # mark-all-read isn't a toggle.
        # Hidden in list mode (which already has an inline footer link
        # inside the NSTextView) and when there are zero unreads. We
        # set the title + final frame on each refresh because the
        # number changes.
        mark_btn = _make_toolbar_toggle(
            "", False, "markAllReadClicked:",
            button_type=NS_BUTTON_TYPE_MOMENTARY_LIGHT,
        )
        mark_btn.setHidden_(True)
        # Initial frame is finalized in _update_mark_all_button on
        # each refresh; the y/height numbers here just give it a
        # valid frame before the first refresh runs.
        mark_btn.setFrame_(NSMakeRect(
            w - dbf.size.width - 24,
            h - TOP_BAR_HEIGHT + 4,
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
        """Pick the right popover (w, h) for the current mode + the two
        compose-able expansion toggles (show_dormant adds a 4th column,
        drawer_open adds a right-side panel). Both grow the popover
        rather than shrinking the kanban so the cards' column widths
        stay constant whichever toggles are on."""
        if self.mode != "kanban":
            return POPOVER_LIST_SIZE
        # drawer_open is a defensive getattr so legacy code paths that
        # construct PopoverVC without going through _load_view_safely
        # still get a valid size (default: drawer closed).
        drawer = bool(getattr(self, "drawer_open", False))
        if self.show_dormant and drawer:
            return POPOVER_KANBAN_WITH_DORMANT_AND_DRAWER_SIZE
        if self.show_dormant:
            return POPOVER_KANBAN_WITH_DORMANT_SIZE
        if drawer:
            return POPOVER_KANBAN_WITH_DRAWER_SIZE
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
        # of the Tasks pill (which itself sits to the left of the
        # Show Older pill). With three right-anchored elements, the
        # mark-all link drifts further left so it doesn't collide.
        container = self.view()
        cw = container.frame().size.width if container is not None else 720
        # Match the dormant pill's top inset (h - TOP_BAR_HEIGHT + 4 —
        # recessed-bezel buttons are taller than the old checkbox so
        # the +4 inset centers better in the 32-px top bar).
        TOP_BAR_HEIGHT = 32.0
        ch = container.frame().size.height if container is not None else 480
        dormant_w = (
            self.dormant_btn.frame().size.width
            if self.dormant_btn is not None else 90.0
        )
        tasks_w = (
            self.tasks_btn.frame().size.width
            if getattr(self, "tasks_btn", None) is not None else 70.0
        )
        # right edge - dormant - 6px gap - tasks - 12px gap - bf
        x = cw - 12 - dormant_w - 6 - tasks_w - 12 - bf.size.width
        y = ch - TOP_BAR_HEIGHT + 4
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

    def showStyleMenu_(self, sender):
        """Settings-gear action — pop up the badge-style menu just below
        the gear. The menu (and its item actions) live on the
        BadgeController, which owns the badge window being restyled."""
        try:
            ctrl = self.badge_controller
            if ctrl is None:
                return
            menu = ctrl.badge_settings_menu()
            loc = NSMakePoint(0.0, sender.bounds().size.height + 2.0)
            menu.popUpMenuPositioningItem_atLocation_inView_(None, loc, sender)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[showStyleMenu_] {e!r}\n")

    def segmentChanged_(self, sender):
        """Primary callback for the List | Kanban segmented control.
        The separated-style NSSegmentedControl owns the radio
        semantics — clicking a segment selects it and deselects the
        other, all without any extra JS. We just translate the
        selected index into a mode string and hand off to the
        shared _switch_mode helper."""
        try:
            idx = sender.selectedSegment()
            new_mode = "kanban" if idx == 1 else "list"
            self._switch_mode(new_mode)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[segmentChanged_] {e!r}\n")

    # modeButtonClicked_ retired alongside the two-pill mutex
    # experiment — NSSegmentedControl owns the radio behavior now.

    @objc.python_method
    def _switch_mode(self, new_mode: str) -> None:
        if new_mode not in ("list", "kanban"):
            return
        if new_mode == self.mode:
            return
        self.mode = new_mode
        _write_popover_mode(new_mode)
        self._apply_popover_size()
        self._install_layout()
        self.refresh()

    def deferredClosePopover_(self, _sender):
        """Close the popover on the next runloop tick, off the call
        stack of whatever triggered the close. Used after spawning
        a Terminal session — closing the popover synchronously from
        inside didReceiveScriptMessage: tears the WKWebView down
        while WebKit is still mid-dispatch, which has produced
        intermittent main-thread stalls visible as a hover-beachball
        on the badge. performSelector with delay=0 schedules the
        close as a separate runloop event so the bridge call
        finishes cleanly first."""
        if self.popover_ref is not None:
            try:
                self.popover_ref.close()
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"[deferredClosePopover] {e!r}\n")

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

    def toggleTasksDrawer_(self, sender):
        """Top-bar Tasks pill clicked. Delegates to the JS layer so
        the existing flow (localStorage + body.drawer-open class +
        bridge → drawerToggled action → popover resize) stays the
        single source of truth. Python doesn't directly touch the
        drawer state here — it gets called BACK via drawerToggled
        almost immediately and updates self.drawer_open + the
        button's own visual state there.

        The button's own toggled state (sender.state()) is therefore
        ephemeral until the JS round-trip completes; if anything
        fails on the JS side, the drawerToggled handler will simply
        not fire and the button reverts on the next refresh's
        _update_tasks_btn_count call."""
        try:
            if self.kanban_web is not None:
                self.kanban_web.evaluateJavaScript_completionHandler_(
                    "window.toggleDrawer && window.toggleDrawer();", None
                )
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[toggleTasksDrawer_] {e!r}\n")

    @objc.python_method
    def _update_tasks_btn_count(self, drawer_data) -> None:
        """Refresh the Tasks pill's title to include the live count of
        OPEN tasks across all visible sessions (e.g. "Tasks · 3").
        Called from _render_popover after the drawer payload is built.
        Falls back to a bare "Tasks" label when zero — keeps the pill
        from showing a noisy "· 0" badge."""
        btn = getattr(self, "tasks_btn", None)
        if btn is None:
            return
        # Hide entirely in list mode — no drawer there. Show otherwise.
        try:
            btn.setHidden_(self.mode == "list")
        except Exception:  # noqa: BLE001
            pass
        if self.mode == "list":
            return
        n = 0
        if drawer_data:
            for g in (drawer_data.get("groups") or []):
                for t in (g.get("tasks") or []):
                    if t.get("status") == "open":
                        n += 1
        new_title = "Tasks" if n == 0 else f"Tasks · {n}"
        try:
            if btn.title() != new_title:
                btn.setTitle_(new_title)
                btn.sizeToFit()
                # Re-anchor right edge: sizeToFit may have changed width.
                container = self.view()
                if container is not None and self.dormant_btn is not None:
                    cw = container.frame().size.width
                    ch = container.frame().size.height
                    dbf = self.dormant_btn.frame()
                    tbf = btn.frame()
                    btn.setFrame_(NSMakeRect(
                        cw - dbf.size.width - 12 - 6.0 - tbf.size.width,
                        ch - 32.0 + 4,
                        tbf.size.width, tbf.size.height,
                    ))
                    # mark-all-button anchors off these widths too —
                    # nudge it back into place after the resize.
                    self._update_mark_all_button()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[_update_tasks_btn_count] {e!r}\n")

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
            # Raw seconds-ago — used by the sidebar's "Today" smart
            # view (filter to ago < 24h) and any other client-side
            # recency rules. Kept as a number so JS doesn't have to
            # parse `format_ago`'s "5m" / "2h" string back into one.
            "ago_s": float(row.get("ago_s") or 0),
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
    def _build_drawer_data(self, buckets: dict) -> dict:
        """Build the right-side tasks-drawer payload — all open + done
        tasks across every visible session, grouped by their cwd (the
        cwd basename becomes the project name; a stable md5 hash picks
        the project color). The drawer is the closest we get to a
        cross-project task view without the full projects entity.

        Shape:
            {
                "groups": [
                    {
                        "label": "Talk back plugin",
                        "color": "purple",
                        "cwd":   "/Users/.../Talk back plugin",
                        "tasks": [
                            {"id", "content", "status", "sessionId",
                             "sessionTitle", "approved", "source"},
                            ...
                        ],
                    },
                    ...
                ]
            }

        Tasks within a group are ordered by their session's bucket
        (needs → working → ready → dormant) so the most-urgent work
        floats to the top per project."""
        groups: dict[str, dict] = {}
        bucket_priority = {"needs": 0, "working": 1, "ready": 2, "dormant": 3}
        # Two-pass scan:
        # 1. Walk all buckets, register every session under its cwd
        #    group (so the create / reattach pickers see ALL sessions
        #    in a project, even sessions with zero tasks). Sessions
        #    appearing here without tasks won't have any task rows
        #    rendered — they're only present in the .sessions list
        #    used by the pickers.
        # 2. Tasks are appended in a second loop below — only groups
        #    that contain at least one task survive.
        for bkey in ("needs", "working", "ready", "dormant"):
            for row in (buckets.get(bkey) or []):
                meta = row.get("meta") or {}
                cwd = meta.get("cwd") if isinstance(meta.get("cwd"), str) else ""
                sid = (row.get("s") or {}).get("sessionId") or ""
                if not sid:
                    continue
                session_title = (row.get("title") or "(untitled)").strip()
                session_tasks = tasks_module.tasks_for_session(sid)
                if not session_tasks:
                    # Track the session for the picker but don't
                    # surface a group from it alone — groups need
                    # at least one task to be worth showing.
                    if cwd in groups:
                        if sid not in groups[cwd]["_session_ids"]:
                            groups[cwd]["_session_ids"].add(sid)
                            groups[cwd]["sessions"].append({
                                "id": sid,
                                "title": session_title,
                                "bucket": bkey,
                                "phase": row.get("phase_label") or "",
                            })
                    continue
                if cwd not in groups:
                    label, color = tasks_module.derive_project_label(cwd)
                    groups[cwd] = {
                        "label": label,
                        "color": color,
                        "cwd": cwd,
                        "tasks": [],
                        "sessions": [],   # for the create + reattach pickers
                        "_min_bucket_priority": bucket_priority[bkey],
                        "_session_ids": set(),
                    }
                else:
                    # Keep the lowest (most urgent) bucket-priority
                    # seen — drives cross-group ordering below.
                    groups[cwd]["_min_bucket_priority"] = min(
                        groups[cwd]["_min_bucket_priority"],
                        bucket_priority[bkey],
                    )
                # Build a slim session entry per group so the drawer's
                # create/reattach pickers can list them. Skip dupes —
                # in practice a session appears in exactly one bucket.
                if sid and sid not in groups[cwd]["_session_ids"]:
                    groups[cwd]["_session_ids"].add(sid)
                    groups[cwd]["sessions"].append({
                        "id": sid,
                        "title": session_title,
                        "bucket": bkey,
                        "phase": row.get("phase_label") or "",
                    })
                for t in session_tasks:
                    groups[cwd]["tasks"].append({
                        "id": t.get("id") or "",
                        "content": t.get("content") or "",
                        "status": t.get("status") or "open",
                        "sessionId": sid,
                        "sessionTitle": session_title,
                        "approved": bool(t.get("approved", True)),
                        "source": t.get("source") or "user",
                    })
        # Second pass: backfill any sessions that share a cwd with an
        # existing group but were skipped on the first pass (e.g.,
        # session has no tasks AND appeared before its sibling with
        # tasks). Ensures the create + reattach pickers see every
        # session in the project group.
        for bkey in ("needs", "working", "ready", "dormant"):
            for row in (buckets.get(bkey) or []):
                meta = row.get("meta") or {}
                cwd = meta.get("cwd") if isinstance(meta.get("cwd"), str) else ""
                sid = (row.get("s") or {}).get("sessionId") or ""
                if not sid or cwd not in groups:
                    continue
                if sid in groups[cwd]["_session_ids"]:
                    continue
                groups[cwd]["_session_ids"].add(sid)
                groups[cwd]["sessions"].append({
                    "id": sid,
                    "title": (row.get("title") or "(untitled)").strip(),
                    "bucket": bkey,
                    "phase": row.get("phase_label") or "",
                })

        # Third pass: unattached tasks. These live under per-project
        # pseudo-session ids (__unattached__:<cwd>) in tasks.json —
        # they have no real Claude session yet. Surface them inside
        # the same cwd's drawer group so the user sees them next to
        # the attached ones. If the cwd has no group yet (rare —
        # would mean an unattached task in a project with zero live
        # sessions), bootstrap one so the task still renders.
        try:
            ts_state = tasks_module.load_state() or {}
        except Exception:  # noqa: BLE001
            ts_state = {}
        ts_sessions = (ts_state.get("sessions") or {}) if isinstance(ts_state, dict) else {}
        for pseudo_sid, entry in ts_sessions.items():
            if not tasks_module.is_unattached_sid(pseudo_sid):
                continue
            if not isinstance(entry, dict):
                continue
            pseudo_tasks = entry.get("tasks") or []
            # Skip empty pseudo-sessions — they'd add a phantom group
            # with no rows.
            if not any(
                t for t in pseudo_tasks
                if t.get("status") in ("open", "done")
            ):
                continue
            pseudo_cwd = tasks_module.cwd_for_unattached_sid(pseudo_sid)
            if pseudo_cwd not in groups:
                # No live session in this cwd, but unattached tasks
                # exist — bootstrap the group so they have a home.
                label, color = tasks_module.derive_project_label(pseudo_cwd)
                groups[pseudo_cwd] = {
                    "label": label,
                    "color": color,
                    "cwd": pseudo_cwd,
                    "tasks": [],
                    "sessions": [],
                    # No real bucket — sort below projects with live
                    # work but above truly empty ones.
                    "_min_bucket_priority": 98,
                    "_session_ids": set(),
                }
            for t in pseudo_tasks:
                if t.get("status") not in ("open", "done"):
                    continue
                groups[pseudo_cwd]["tasks"].append({
                    "id": t.get("id") or "",
                    "content": t.get("content") or "",
                    "status": t.get("status") or "open",
                    "sessionId": pseudo_sid,
                    # Sentinel — the drawer JS reads this and renders
                    # "(unattached) — ↗ attach" instead of "↗ <title>".
                    "sessionTitle": "",
                    "unattached": True,
                    "approved": bool(t.get("approved", True)),
                    "source": t.get("source") or "user",
                })

        # Order groups by their most-urgent session bucket, so
        # projects with NEEDS-YOU work sit at the top of the drawer.
        ordered = sorted(
            groups.values(),
            key=lambda g: (g.get("_min_bucket_priority", 99), g["label"].lower()),
        )
        # Strip the internal sort keys before sending to JS.
        for g in ordered:
            g.pop("_min_bucket_priority", None)
            g.pop("_session_ids", None)
        return {"groups": ordered}

    @objc.python_method
    def _build_sidebar_data(self, buckets: dict) -> dict:
        """Build the left-rail (Idea 2.α) payload — project list derived
        from session cwds, with per-project session counts and an
        alertCount for any NEEDS-YOU sessions in that project. Plus the
        two smart-view counts (Needs you, Today). Projects are the
        cross-session organizing principle until the full projects
        entity ships; until then we use cwd as the natural project key.

        Shape:
            {
              "projects": [
                {
                  "cwd":        "/Users/foo/Talk back plugin",
                  "label":      "Talk back plugin",
                  "color":      "purple",
                  "count":      5,         # sessions in this project
                  "alertCount": 1,         # sessions in NEEDS YOU bucket
                },
                ...
              ],
              "totals": {
                "all":     17,             # total visible sessions
                "needsYou": 3,             # smart view: bucket == needs
                "today":   6,              # smart view: ago < 24h
              },
            }

        Projects are ordered by alertCount desc, then label asc, so
        projects with NEEDS-YOU work float to the top of the rail.
        Returned even in list mode so the popover-mode toggle doesn't
        have to re-fetch; the kanban-only sidebar CSS gates display."""
        import time as _time
        projects: dict[str, dict] = {}
        total_all = 0
        total_needs = 0
        total_today = 0
        now = _time.time()
        # Last-24h window — matches the "Today" smart view in the mockup.
        TODAY_WINDOW_S = 24 * 3600
        for bkey in ("needs", "working", "ready", "dormant"):
            for row in (buckets.get(bkey) or []):
                meta = row.get("meta") or {}
                cwd = meta.get("cwd") if isinstance(meta.get("cwd"), str) else ""
                sid = (row.get("s") or {}).get("sessionId") or ""
                if not sid:
                    continue
                total_all += 1
                if bkey == "needs":
                    total_needs += 1
                ago_s = row.get("ago_s")
                if isinstance(ago_s, (int, float)) and ago_s < TODAY_WINDOW_S:
                    total_today += 1
                # cwd may legitimately be "" for some sessions — bucket
                # them under a stable "(no folder)" pseudo-key so they
                # still appear in the sidebar instead of vanishing.
                key = cwd or "__no_cwd__"
                if key not in projects:
                    label, color = tasks_module.derive_project_label(cwd)
                    projects[key] = {
                        "cwd": cwd,
                        "label": label,
                        "color": color,
                        "count": 0,
                        "alertCount": 0,
                    }
                projects[key]["count"] += 1
                if bkey == "needs":
                    projects[key]["alertCount"] += 1
        ordered = sorted(
            projects.values(),
            # alert-bearing projects first, then alphabetical for stable
            # order across refreshes. Negation flips alertCount to desc.
            key=lambda p: (-p["alertCount"], p["label"].lower()),
        )
        return {
            "projects": ordered,
            "totals": {
                "all": total_all,
                "needsYou": total_needs,
                "today": total_today,
            },
        }

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
            # Right-side tasks drawer: tasks grouped by cwd (the
            # placeholder for projects until the full projects entity
            # ships). JS in kanban.html renders this when the drawer
            # is open. Only computed in kanban mode — list mode is
            # too narrow to host the drawer.
            "drawer": (
                self._build_drawer_data(buckets)
                if self.mode != "list" else None
            ),
            # Left-rail project sidebar (Idea 2.α from the canonical
            # mockup): one entry per cwd-derived project + the two
            # smart views (Needs you, Today). JS uses this to render
            # the rail AND to filter the kanban when the user picks
            # one. Kanban-only — list mode is too narrow.
            "sidebar": (
                self._build_sidebar_data(buckets)
                if self.mode != "list" else None
            ),
            # Last-used new-session launch config, so the start modal can
            # pre-fill its permission-mode / model / continue controls.
            "startConfig": _load_start_config(),
        }

        # If the JS isn't ready yet (initial loadHTMLString hasn't
        # settled), buffer the payload — the navigation-finished JS
        # handshake flushes it via _kanban_web_flush_pending.
        if not self.kanban_web_ready:
            self.kanban_web_pending = payload
        else:
            self._kanban_web_evaluate(payload)

        self._update_mark_all_button()
        # Refresh the Tasks pill's count badge. Cheap (one pass over
        # the already-built drawer payload). Hidden in list mode by
        # the pill itself being hidden — _update_tasks_btn_count is
        # a no-op when self.tasks_btn doesn't exist.
        self._update_tasks_btn_count(payload.get("drawer"))

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
        elif action == "drawerToggled":
            # JS toggled the right-side tasks drawer. Persist + grow
            # the popover so the kanban + sidebar widths stay constant
            # (same pattern as show_dormant). The body grid is already
            # animating its own track from 0fr → 280px; growing the
            # popover by the same 280px keeps the kanban width fixed
            # instead of squeezing it.
            new_state = bool(payload.get("open"))
            if new_state == bool(getattr(self, "drawer_open", False)):
                return  # no-op — JS resends on every render for sync
            self.drawer_open = new_state
            try:
                _write_drawer_open(new_state)
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"[kanban-web] drawer persist: {e!r}\n")
            # Keep the native Tasks toolbar pill in sync. The user
            # may have toggled via the pill (sender.state() already
            # mirrors the new state) OR via a future JS path; sync
            # the button defensively so both surfaces stay aligned.
            tasks_btn = getattr(self, "tasks_btn", None)
            if tasks_btn is not None:
                try:
                    tasks_btn.setState_(1 if new_state else 0)
                except Exception:  # noqa: BLE001
                    pass
            self._apply_popover_size()
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
        elif action == "taskCreateUnattached":
            # Drawer "+ add task" with no explicit session chosen.
            # The task is stored under a per-project pseudo-session id
            # (__unattached__:<cwd>) so existing CRUD code paths work
            # unchanged. The drawer renders these with an "(unattached)
            # — ↗ attach" sub-label that opens the existing picker; on
            # pick, taskAttachSession moves the task out of the pseudo-
            # session into the real one.
            content = pstr("content")
            unattached_cwd = pstr("cwd")
            if not content:
                return
            created = tasks_module.create_unattached_task(unattached_cwd, content)
            if created is None:
                sys.stderr.write(
                    f"[tasks] unattached create rejected cwd={unattached_cwd!r} "
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
        elif action == "taskCopy":
            # JS-side fallback for clipboard writes that fail under
            # WKWebView (the modern Clipboard API can be blocked in
            # non-secure contexts). The payload carries the literal
            # task text; we shell out to pbcopy. Fire-and-forget —
            # no refresh, no error surfacing to the user.
            text = pstr("content")
            if not text:
                return
            self._copy_to_clipboard(text)
        elif action == "taskStart":
            # User edited the prompt in the modal and hit "Start".
            # Spawn a fresh Terminal at the target cwd and run
            # `claude <prompt>` so the new session opens with that
            # prompt as its first user message. The spawn is fully
            # detached (start_new_session=True + DEVNULL stdio) so
            # the new Claude process shares no terminal control or
            # signals with the badge — the badge keeps running and
            # responding to hover normally after Terminal activates.
            prompt = pstr("prompt")
            target_cwd = pstr("cwd")
            tid = pstr("taskId")
            if not prompt:
                return
            # Launch config chosen in the modal (permission mode, model,
            # continue). Persist it as the new last-used so the modal
            # reopens pre-filled, then translate to `claude` CLI flags.
            sc_raw = pstr("startConfigJson")
            raw_cfg = None
            if sc_raw:
                try:
                    raw_cfg = json.loads(sc_raw)
                except (ValueError, TypeError):
                    raw_cfg = None
            if isinstance(raw_cfg, dict):
                start_cfg = _coerce_start_config(raw_cfg)
                _save_start_config(start_cfg)   # persist only the sticky part
                # The chosen resume target is a specific session id — per
                # launch, not persisted. Carry it through for flag-building.
                if start_cfg.get("sessionTarget") == "existing":
                    rid = str(raw_cfg.get("resumeSessionId") or "").strip()
                    if rid:
                        start_cfg["resumeSessionId"] = rid
            else:
                start_cfg = _load_start_config()
            # Decide the session id the launched session will own, and
            # link the task to it now. For a brand-new session (the
            # default, and always for worktrees) we MINT the UUID
            # ourselves and pass it via `claude --session-id`, so the
            # task attaches to that exact id the instant we spawn — no
            # transcript polling, no guessing which freshly-appeared
            # session belongs to this click. For an "existing" resume
            # target the id already exists, so we attach straight to it;
            # for a bare --continue we don't know the id up front, so we
            # leave the task where it is.
            # "Review" (dry-run): paste the command into Terminal without
            # running it. Per-launch only, not persisted.
            review = bool(raw_cfg.get("dryRun")) if isinstance(raw_cfg, dict) else False
            import uuid as _uuid
            if start_cfg.get("location") == "worktree":
                # A fresh git worktree is always a new session — resume/
                # continue don't apply there, so force the new-session flags.
                wt_cfg = dict(start_cfg, sessionTarget="new")
                flags = _start_config_flags(wt_cfg)
                new_sid = str(_uuid.uuid4())
                flags += ["--session-id", new_sid]
                if tid:
                    tasks_module.attach_task_to_new_session(tid, new_sid)
                self._spawn_worktree_terminal(target_cwd, prompt, flags, review=review)
            elif start_cfg.get("sessionTarget") == "existing":
                flags = _start_config_flags(start_cfg)
                resume_sid = str(start_cfg.get("resumeSessionId") or "").strip()
                # Only a specific --resume <id> has a known session id to
                # attach to; a bare --continue resumes "most recent" and
                # we can't name it here, so skip the attach in that case.
                if tid and resume_sid:
                    tasks_module.attach_task_to_new_session(tid, resume_sid)
                self._spawn_new_terminal_with_prompt(target_cwd, prompt, flags, review=review)
            else:
                flags = _start_config_flags(start_cfg)
                new_sid = str(_uuid.uuid4())
                flags += ["--session-id", new_sid]
                if tid:
                    tasks_module.attach_task_to_new_session(tid, new_sid)
                self._spawn_new_terminal_with_prompt(target_cwd, prompt, flags, review=review)
            # Defer the popover close to the next runloop tick. Closing
            # synchronously from inside this script-message handler
            # has caused intermittent stalls (visible as a hover
            # beachball on the badge) when the WKWebView is mid-
            # dispatch and its parent window starts tearing down.
            # performSelector with delay=0 schedules the close after
            # the current event finishes.
            try:
                self.performSelector_withObject_afterDelay_(
                    "deferredClosePopover:", None, 0.0,
                )
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"[taskStart deferred close] {e!r}\n")
        elif action == "taskAttachSession":
            # Move a task from one session to another. Used by the
            # drawer's "change attachment" picker and (eventually) by
            # drag-to-attach. The current session is required so we
            # can find the task in O(1) bucket lookup; the new session
            # must exist (validated by tasks.move_task_to_session).
            tid = pstr("taskId")
            new_sid = pstr("newSessionId")
            # sid here = the task's current session (the resume target
            # field repurposed for this action — JS sends both).
            if not tid or not sid or not new_sid:
                return
            if sid == new_sid:
                return  # no-op
            if not tasks_module.move_task_to_session(sid, tid, new_sid):
                sys.stderr.write(
                    f"[tasks] attach failed sid={sid[:8]} → "
                    f"{new_sid[:8]} tid={tid}\n"
                )
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

        1. Session is alive headless in Claude.app (no controlling TTY)
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
    def _detached_popen_kwargs(self) -> dict:
        """Canonical kwargs for spawning a user-facing helper process
        (osascript, open -a, …) so the child is FULLY decoupled from
        the badge:

          • start_new_session=True   — fresh POSIX session + process
            group, so the child shares no signals, terminal control,
            or job-control state with us. The badge can quit / be
            backgrounded / steal focus without touching the child.
          • stdin / stdout / stderr → /dev/null — no inherited pipes
            that could leak file descriptors or, worse, block the
            child's read/write when its other end isn't being read.
          • close_fds=True — explicit (it's the default on POSIX in
            Python 3, but stating it makes the intent obvious and
            survives future refactors).

        The hover-freeze the user hit after clicking ▶ Start traced
        to the child inheriting state from us; this kwargs bundle is
        the canonical "fire and forget" pattern."""
        return {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "start_new_session": True,
            "close_fds": True,
        }

    @objc.python_method
    def _activate_app(self, app_name: str) -> None:
        try:
            subprocess.Popen(
                ["open", "-a", app_name],
                **self._detached_popen_kwargs(),
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
                **self._detached_popen_kwargs(),
            )
        except (OSError, subprocess.SubprocessError) as e:
            sys.stderr.write(f"[_spawn_new_terminal_session] {e!r}\n")

    @objc.python_method
    def _spawn_new_terminal_with_prompt(
        self, cwd: str, prompt: str, flags: "list[str] | None" = None,
        review: bool = False,
    ) -> None:
        """Open a fresh Terminal window at ``cwd`` and launch
        ``claude [flags] <prompt>`` so the new Claude Code session starts
        with the user's edited task text as its first message and the
        launch options chosen in the modal (permission mode, model,
        continue). Used by the "▶ start" affordance on task rows.

        We pass the prompt as a single positional argument; Claude
        Code's CLI treats argv[1:] as the initial user prompt and
        opens the interactive REPL with that already submitted. cwd,
        every flag, and the prompt are shlex-quoted, then the whole
        shell command is JSON-encoded so AppleScript's `do script` sees
        a single, properly-escaped string — without that the quoted
        shell snippet's embedded quotes break the AppleScript parser."""
        import shlex
        if not cwd:
            cwd = os.path.expanduser("~")
        prompt = (prompt or "").strip()
        if not prompt:
            return
        shell_cmd = (
            f"cd {shlex.quote(cwd)} && {self._claude_invocation(flags, prompt)}"
        )
        self._run_terminal_do_script(shell_cmd, review=review)

    @objc.python_method
    def _claude_invocation(self, flags: "list[str] | None", prompt: str) -> str:
        """Return the shlex-quoted ``claude [flags] <prompt>`` command."""
        import shlex
        parts = ["claude"]
        for f in (flags or []):
            parts.append(shlex.quote(str(f)))
        parts.append(shlex.quote(prompt))
        return " ".join(parts)

    @objc.python_method
    def _run_terminal_do_script(self, shell_cmd: str, review: bool = False) -> None:
        """Open/activate Terminal and run ``shell_cmd`` in a fresh window.
        The command is JSON-encoded so quotes/backslashes inside the prompt
        don't escape. ensure_ascii=False keeps non-ASCII (emoji, accents,
        RTL) intact — AppleScript does NOT decode JSON's \\uXXXX escapes
        inside string literals, so "review the café UX 🚀" would otherwise
        arrive mangled. JSON's other escapes (\\", \\\\, \\n, \\r, \\t)
        coincide with AppleScript's string-literal escapes, so the rest
        stays safe.

        When ``review`` is True the command is NOT executed: it's pushed
        into zsh's line-edit buffer via ``print -z`` so it appears typed at
        the prompt for the user to inspect and press Enter (or edit /
        discard). The full command is wrapped in one zsh-quoted string (each
        ' re-escaped as '\\'') so the embedded shlex quoting survives.
        Requires the Terminal shell to be zsh (the macOS default)."""
        if review:
            esc = "'" + shell_cmd.replace("'", "'\\''") + "'"
            shell_cmd = "print -z -- " + esc
        applescript_str = json.dumps(shell_cmd, ensure_ascii=False)
        try:
            subprocess.Popen(
                [
                    "osascript",
                    "-e", 'tell application "Terminal" to activate',
                    "-e", f'tell application "Terminal" to do script {applescript_str}',
                ],
                **self._detached_popen_kwargs(),
            )
        except (OSError, subprocess.SubprocessError) as e:
            sys.stderr.write(f"[_run_terminal_do_script] {e!r}\n")

    @objc.python_method
    def _spawn_worktree_terminal(
        self, cwd: str, prompt: str, flags: "list[str] | None" = None,
        review: bool = False,
    ) -> None:
        """Create a fresh git worktree off the repo containing ``cwd`` (on a
        new ``claude/<timestamp>`` branch) and launch ``claude [flags]
        <prompt>`` inside it, so the session works on an isolated checkout
        without disturbing the user's current tree. The whole thing runs as
        one shell command in Terminal so the user sees the `git worktree
        add` output (and any error). If ``cwd`` isn't inside a git work
        tree, falls back to a normal same-directory launch."""
        import shlex
        import datetime
        if not cwd:
            cwd = os.path.expanduser("~")
        prompt = (prompt or "").strip()
        if not prompt:
            return
        root = ""
        try:
            r = subprocess.run(
                ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                root = r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            root = ""
        if not root:
            sys.stderr.write(
                "[worktree] cwd is not a git repo; starting in-place instead\n")
            self._spawn_new_terminal_with_prompt(cwd, prompt, flags, review=review)
            return
        root = root.rstrip("/")
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        branch = "claude/" + stamp
        base = os.path.basename(root) or "repo"
        wt_path = os.path.join(os.path.dirname(root), f"{base}-{stamp}")
        shell_cmd = (
            "git -C " + shlex.quote(root)
            + " worktree add -b " + shlex.quote(branch) + " " + shlex.quote(wt_path)
            + " && cd " + shlex.quote(wt_path)
            + " && " + self._claude_invocation(flags, prompt)
        )
        self._run_terminal_do_script(shell_cmd, review=review)

    @objc.python_method
    def _copy_to_clipboard(self, text: str) -> bool:
        """Write ``text`` to the macOS clipboard via pbcopy. Used as a
        fallback when the JS-side navigator.clipboard.writeText path
        rejects (some WKWebView contexts treat themselves as non-
        secure and block the modern Clipboard API). Returns True iff
        pbcopy exited cleanly."""
        if not isinstance(text, str):
            return False
        try:
            p = subprocess.Popen(
                ["pbcopy"], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            p.communicate(input=text.encode("utf-8"), timeout=2.0)
            return p.returncode == 0
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            sys.stderr.write(f"[_copy_to_clipboard] {e!r}\n")
            return False

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
    notify_seeded = objc.ivar("notify_seeded")  # bool — first-tick flood guard

    def initWithPanelMode_(self, panel_mode: str):
        self = objc.super(BadgeController, self).init()
        if self is None:
            return None

        # Build the badge window — borderless, non-activating panel.
        # Sized to the user's chosen badge shape (defaults to "bento").
        x, y = _load_badge_origin()
        badge_style = _read_badge_style()
        bw, bh = _badge_dims(badge_style)
        style = (
            NSWindowStyleMaskBorderless
            | NSWindowStyleMaskNonactivatingPanel
        )
        win = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, bw, bh),
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

        self.badge_window = win
        # Build the content (tiles / flat surface + foreground BadgeView)
        # for the active style. Sets self.badge_view.
        self._build_badge_chrome(badge_style)

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
        # And a back-reference so its settings gear can open + apply the
        # badge-style menu (the controller owns the badge window).
        popover_vc.set_badge_controller(self)
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

        # First refresh seeds the notify baseline (records sessions already
        # waiting at launch) without firing — so starting the badge while
        # things are mid-NEEDS-YOU doesn't dump a burst of banners.
        self.notify_seeded = False

        # Initial paint + timer.
        self.refresh_(None)
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            REFRESH_SECS, self, "refresh:", None, True
        )
        return self

    @objc.python_method
    def _build_badge_chrome(self, style):
        """(Re)build the badge window's content view for `style`. For the
        bento style this lays out three NSVisualEffectView glass tiles
        behind a transparent BadgeView; the flat styles need no tiles —
        the BadgeView paints its own unified dark surface. Replaces
        self.badge_view with a freshly-styled one."""
        w, h = _badge_dims(style)
        container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        container.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)

        if style == "bento":
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
                    t_layer.setBorderWidth_(0.5)
                    t_layer.setBorderColor_(
                        NSColor.separatorColor()
                        .colorWithAlphaComponent_(0.5).CGColor()
                    )
                container.addSubview_(tile)

        # Foreground BadgeView — draws the foreground (and, for flat
        # styles, the whole surface) and captures click/drag/right-click.
        view = BadgeView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        view.set_controller(self)
        view.set_style(style)
        view.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        container.addSubview_(view)

        self.badge_window.setContentView_(container)
        self.badge_view = view

    @objc.python_method
    def _apply_badge_style(self, style):
        """Resize the badge window to the new style's dimensions (keeping
        its on-screen origin) and rebuild its content. Called when the
        user picks a different shape from the settings menu."""
        if style not in BADGE_STYLES:
            return
        w, h = _badge_dims(style)
        win = self.badge_window
        f = win.frame()
        win.setFrame_display_(
            NSMakeRect(f.origin.x, f.origin.y, w, h), True)
        self._build_badge_chrome(style)
        try:
            self.badge_view.set_counts(self._counts())
        except Exception:  # noqa: BLE001
            pass

    def changeBadgeStyle_(self, sender):
        """Menu action — persist + apply the chosen badge style."""
        try:
            style = str(sender.representedObject())
        except Exception:  # noqa: BLE001
            return
        if style not in BADGE_STYLES:
            return
        _write_badge_style(style)
        self._apply_badge_style(style)

    @objc.python_method
    def badge_settings_menu(self):
        """Build the right-click settings menu: a checkmarked list of the
        available floating-button shapes, plus Quit."""
        menu = NSMenu.alloc().init()
        header = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Floating Button Style", None, "")
        header.setEnabled_(False)
        menu.addItem_(header)
        current = _read_badge_style()
        for style in BADGE_STYLES:
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                BADGE_STYLE_LABELS[style], "changeBadgeStyle:", "")
            item.setTarget_(self)
            item.setRepresentedObject_(style)
            item.setState_(1 if style == current else 0)  # NSControlStateValueOn
            menu.addItem_(item)
        menu.addItem_(NSMenuItem.separatorItem())
        # Opt-in proactive notifications when a session enters NEEDS YOU.
        notify_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Notify on NEEDS YOU", "toggleNotify:", "")
        notify_item.setTarget_(self)
        notify_item.setState_(1 if _read_notify_enabled() else 0)
        menu.addItem_(notify_item)
        menu.addItem_(NSMenuItem.separatorItem())
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit", "terminate:", "q")
        menu.addItem_(quit_item)
        return menu

    def toggleNotify_(self, _sender):
        """Flip the proactive-notification opt-in. When turning ON, re-seed
        the baseline so we start fresh from now (no banner for sessions that
        were already waiting before the user opted in)."""
        new_value = not _read_notify_enabled()
        _write_notify_enabled(new_value)
        if new_value:
            self.notify_seeded = False

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
        _install_edit_menu(main)
        NSApp.setMainMenu_(main)

    @objc.python_method
    def _counts(self) -> dict:
        try:
            buckets = _get_buckets()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[badge] buckets failed: {e!r}\n")
            return {"needs": 0, "working": 0, "ready": 0, "dormant": 0}
        return {k: len(v) for k, v in buckets.items()}

    @objc.python_method
    def _buckets(self) -> dict:
        """Full enriched buckets (rows, not just counts). Never raises —
        falls back to empty buckets so a bad parse can't kill the tick."""
        try:
            return _get_buckets()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[badge] buckets failed: {e!r}\n")
            return {"needs": [], "working": [], "ready": [], "dormant": []}

    @objc.python_method
    def _maybe_notify(self, needs_rows: list) -> None:
        """Fire a proactive nudge for sessions that just entered NEEDS YOU.

        Debounced via persisted per-session epochs: a session only nudges
        when a *newer* turn lands it in NEEDS YOU, so one sitting in the
        bucket never re-fires. The first tick after launch (and right after
        the user enables the feature) seeds the baseline silently."""
        if not _read_notify_enabled():
            return
        try:
            state = _load_notify_state()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[notify] load state failed: {e!r}\n")
            return

        # Current NEEDS YOU sessions: sid -> (epoch, title, phase_label).
        current: dict = {}
        for row in needs_rows:
            sess = row.get("s") or {}
            sid = sess.get("sessionId") or ""
            epoch = row.get("lastTurnEpoch")
            if not sid or not isinstance(epoch, (int, float)):
                continue
            current[sid] = (
                float(epoch),
                row.get("title") or "Claude session",
                row.get("phase_label") or "needs you",
            )

        now = time.time()

        # Seeding pass: record what's already waiting, fire nothing.
        if not self.notify_seeded:
            for sid, (epoch, _t, _p) in current.items():
                state[sid] = {"epoch": epoch, "at": now}
            try:
                _save_notify_state(state)
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"[notify] seed save failed: {e!r}\n")
            self.notify_seeded = True
            return

        # We key the debounce purely on lastTurnEpoch monotonicity, NOT on
        # observed bucket transitions: a session re-enters NEEDS YOU only by
        # a new turn landing it there, which advances lastTurnEpoch. The
        # corollary — consistent with the unread/seen machinery — is that a
        # round-trip through WORKING that returns to NEEDS YOU *without* the
        # epoch advancing past epsilon won't re-fire. That only happens when
        # lastTurnEpoch is the fileMtime fallback rather than a real turn,
        # which is rare and not worth the extra per-tick bucket bookkeeping.
        # Old entries for sessions that have left NEEDS YOU are harmless
        # (epoch only ever climbs) and get reaped by the 30-day GC on save.
        fresh: list = []  # (title, phase)
        for sid, (epoch, title, phase) in current.items():
            prev = state.get(sid)
            prev_epoch = prev.get("epoch") if isinstance(prev, dict) else None
            if (not isinstance(prev_epoch, (int, float))
                    or epoch > prev_epoch + _NOTIFY_EPSILON_S):
                fresh.append((title, phase))
                state[sid] = {"epoch": epoch, "at": now}

        if not fresh:
            return

        try:
            _save_notify_state(state)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[notify] save failed: {e!r}\n")

        # Coalesce a simultaneous burst into one banner rather than N.
        if len(fresh) == 1:
            title, phase = fresh[0]
            _post_notification("🔔 A session needs you", title, subtitle=phase)
        else:
            _post_notification(
                "🔔 Sessions need you",
                f"{len(fresh)} sessions are waiting on you")
        self._pulse_badge()

    @objc.python_method
    def _pulse_badge(self) -> None:
        """A brief opacity pulse on the badge to draw the eye toward a new
        NEEDS YOU. Best-effort — any framework hiccup just no-ops so the
        badge keeps rendering normally."""
        try:
            view = self.badge_view
            if view is None:
                return
            view.setWantsLayer_(True)
            layer = view.layer()
            if layer is None:
                return
            try:
                from QuartzCore import CABasicAnimation
            except ImportError:
                from Quartz import CABasicAnimation
            anim = CABasicAnimation.animationWithKeyPath_("opacity")
            anim.setFromValue_(1.0)
            anim.setToValue_(0.4)
            anim.setDuration_(0.45)
            anim.setAutoreverses_(True)
            anim.setRepeatCount_(2.0)
            layer.addAnimation_forKey_(anim, "needsPulse")
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[notify] pulse failed: {e!r}\n")

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
        # Pull buckets once: feed the badge counts AND the notify hook
        # (which needs the NEEDS YOU rows, not just the tally).
        buckets = self._buckets()
        self.badge_view.set_counts(
            {k: len(v) for k, v in buckets.items()})
        self._maybe_notify(buckets.get("needs", []))
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
