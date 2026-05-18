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
import signal
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
        NSBox,
        NSColor,
        NSEvent,
        NSFont,
        NSFontAttributeName,
        NSForegroundColorAttributeName,
        NSFontWeightMedium,
        NSFontWeightRegular,
        NSFontWeightSemibold,
        NSGradient,
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
    import objc

    NSFloatingWindowLevel = 5  # AppKit constant — not always exported by PyObjC.
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
from dashboard import (  # noqa: E402
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
    transcript_meta,
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
    try:
        POPOVER_MODE_FILE.write_text(mode, encoding="utf-8")
    except OSError:
        pass


# ---------- Unread / seen state ----------
# Per-session "last seen" timestamp lives in ~/.claude-sessions-status-seen.json.
# A session is *unread* when its current lastTurnEpoch (real conversation
# activity) is later than the saved seen epoch. This is opt-in: sessions
# the user has never marked-as-read are simply not tracked, so a fresh
# install doesn't dump dozens of unread dots on day one.
SEEN_FILE = HOME / ".claude-sessions-status-seen.json"


def _load_seen() -> dict:
    if not SEEN_FILE.exists():
        return {}
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_seen(seen: dict) -> None:
    try:
        SEEN_FILE.write_text(json.dumps(seen), encoding="utf-8")
    except OSError:
        pass


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
# Tile order on the bento: 3 active buckets + 1 dormant tile (muted).
# Dormant is integrated as a peer tile so the count is always visible
# at a glance, but it's drawn in tertiaryLabel grays so it doesn't
# compete with the active counts for attention.
TILE_KEYS = ("needs", "working", "ready", "dormant")
TILE_ICONS = {
    "needs":   "bell.badge.fill",
    "working": "gearshape.2.fill",
    "ready":   "checkmark.seal.fill",
    "dormant": "moon.zzz.fill",
}
NUM_TILES = len(TILE_KEYS)
BADGE_WIDTH = TILE_SIZE * NUM_TILES + TILE_GAP * (NUM_TILES - 1)   # 242
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
    `color`, at the given point size. Falls back silently if the system
    doesn't have that symbol (older macOS, typo, etc.)."""
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if img is None:
        return
    try:
        # Build size + weight config first.
        size_cfg = NSImageSymbolConfiguration.configurationWithPointSize_weight_scale_(
            point_size, NSFontWeightSemibold, 2,  # scale=Medium
        )
        # Then a hierarchical-color config (tint).
        color_cfg = NSImageSymbolConfiguration.configurationWithHierarchicalColor_(color)
        combined = size_cfg.configurationByApplyingConfiguration_(color_cfg)
        img = img.imageWithSymbolConfiguration_(combined)
    except Exception:  # noqa: BLE001
        # Older macOS / different PyObjC API path — fall back to drawing
        # the symbol untinted at its default point size.
        pass
    sz = img.size()
    rect = NSMakeRect(
        center_x - sz.width / 2.0,
        center_y - sz.height / 2.0,
        sz.width, sz.height,
    )
    # NSCompositingOperationSourceOver = 2.
    img.drawInRect_fromRect_operation_fraction_(rect, NSMakeRect(0, 0, 0, 0), 2, 1.0)


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
    try:
        WINDOW_STATE_FILE.write_text(
            json.dumps({"x": x, "y": y, "w": w, "h": h}), encoding="utf-8"
        )
    except OSError:
        pass


def _read_mode() -> str:
    try:
        v = MODE_FILE.read_text(encoding="utf-8").strip()
        if v in ("list", "kanban"):
            return v
    except OSError:
        pass
    return "list"


def _write_mode(mode: str) -> None:
    try:
        MODE_FILE.write_text(mode, encoding="utf-8")
    except OSError:
        pass


# ---------- Session aggregation (mirrors menubar.py) ----------
def _classify(state: str, phase_label: str) -> str:
    if state == "Maybe stuck":
        return "needs"
    if phase_label in ("Asking you", "Proposing a plan"):
        return "needs"
    if state == "Working…":
        return "working"
    if state == "Waiting on you":
        return "ready"
    return "ready"


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
                        from AppKit import NSMutableAttributedString as _M
                        full = _M.alloc().initWithAttributedString_(content)
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
    try:
        BADGE_STATE_FILE.write_text(json.dumps({"x": x, "y": y}), encoding="utf-8")
    except OSError:
        pass


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
        return self

    @objc.python_method
    def set_counts(self, counts: dict) -> None:
        self.counts = counts
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


class PopoverVC(NSViewController):
    """Popover content view controller with a List ↔ Kanban segmented
    control at the top. Choice persists to ~/.claude-sessions-status-popover-mode.

    Both layouts use the same rendering primitives (NSScrollView +
    NSTextView + NSAttributedString) so we keep one proven text-stack
    instead of mixing custom auto-layout views."""

    mode = objc.ivar("mode")                  # "list" or "kanban"
    segmented = objc.ivar("segmented")
    content_host = objc.ivar("content_host")
    list_scroll = objc.ivar("list_scroll")
    list_text_view = objc.ivar("list_text_view")
    kanban_stack = objc.ivar("kanban_stack")
    kanban_text_views = objc.ivar("kanban_text_views")
    popover_ref = objc.ivar("popover_ref")    # NSPopover, set by BadgeController
    last_rendered_rows = objc.ivar("last_rendered_rows")  # for mark-all-read

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
        # Restore last-used mode.
        self.mode = _read_popover_mode()
        size = POPOVER_KANBAN_SIZE if self.mode == "kanban" else POPOVER_LIST_SIZE
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
        seg_w = 160.0
        seg.setFrame_(NSMakeRect((w - seg_w) / 2, h - TOP_BAR_HEIGHT + 4, seg_w, 22))
        seg.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxXMargin | NSViewMinYMargin)
        container.addSubview_(seg)
        self.segmented = seg

        # ---- Content host below top bar ----
        host = NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, w, h - TOP_BAR_HEIGHT)
        )
        host.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        container.addSubview_(host)
        self.content_host = host

        # Build BOTH layouts upfront; only the current one is in the host.
        self._build_list_views()
        self._build_kanban_views()
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

    @objc.python_method
    def _build_kanban_views(self):
        """3 NSScrollView+NSTextView columns side by side via NSStackView."""
        stack = NSStackView.alloc().initWithFrame_(self.content_host.bounds())
        stack.setOrientation_(NS_USER_INTERFACE_LAYOUT_ORIENTATION_HORIZONTAL)
        stack.setSpacing_(4.0)
        stack.setDistribution_(NS_STACK_VIEW_DISTRIBUTION_FILL_EQUALLY)
        stack.setEdgeInsets_((4.0, 4.0, 4.0, 4.0))
        stack.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)

        tvs: list = []
        for _ in range(3):
            col_scroll = NSScrollView.alloc().init()
            col_scroll.setHasVerticalScroller_(True)
            col_scroll.setBorderType_(0)
            col_scroll.setDrawsBackground_(False)
            col_scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)

            col_tv = NSTextView.alloc().init()
            col_tv.setEditable_(False)
            col_tv.setSelectable_(True)
            col_tv.setRichText_(True)
            col_tv.setHorizontallyResizable_(False)
            col_tv.setVerticallyResizable_(True)
            col_tv.setAutoresizingMask_(NSViewWidthSizable)
            col_tv.textContainer().setWidthTracksTextView_(True)
            col_tv.setDrawsBackground_(False)
            col_tv.setTextContainerInset_(NSMakeSize(0, 4))

            col_scroll.setDocumentView_(col_tv)
            stack.addArrangedSubview_(col_scroll)
            tvs.append(col_tv)

        self.kanban_stack = stack
        self.kanban_text_views = tvs

    @objc.python_method
    def _install_layout(self):
        """Swap whichever content view is in the host based on self.mode."""
        if self.content_host is None:
            return
        for sub in list(self.content_host.subviews()):
            sub.removeFromSuperview()
        if self.mode == "kanban":
            self.kanban_stack.setFrame_(self.content_host.bounds())
            self.content_host.addSubview_(self.kanban_stack)
        else:
            self.list_scroll.setFrame_(self.content_host.bounds())
            self.content_host.addSubview_(self.list_scroll)

    def segmentChanged_(self, sender):
        try:
            idx = sender.selectedSegment()
            new_mode = "kanban" if idx == 1 else "list"
            if new_mode == self.mode:
                return
            self.mode = new_mode
            _write_popover_mode(new_mode)
            # Resize the popover content frame to fit the new mode.
            new_size = (
                POPOVER_KANBAN_SIZE if new_mode == "kanban" else POPOVER_LIST_SIZE
            )
            if self.popover_ref is not None:
                try:
                    self.popover_ref.setContentSize_(NSMakeSize(*new_size))
                except Exception as e:  # noqa: BLE001
                    sys.stderr.write(f"[segmentChanged] resize failed: {e!r}\n")
            self._install_layout()
            self.refresh()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[segmentChanged_] {e!r}\n")
            import traceback
            traceback.print_exc(file=sys.stderr)

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

        if self.mode == "kanban":
            self._render_kanban(buckets)
        else:
            self._render_list(buckets)

        # Make sure the text views forward link clicks to us.
        if self.list_text_view is not None:
            self.list_text_view.setDelegate_(self)
        if self.kanban_text_views is not None:
            for tv in self.kanban_text_views:
                tv.setDelegate_(self)

    @objc.python_method
    def _render_list(self, buckets):
        if self.list_text_view is None:
            return
        mas = self._build_attributed(buckets)
        self.list_text_view.textStorage().setAttributedString_(mas)

    @objc.python_method
    def _render_kanban(self, buckets):
        if self.kanban_text_views is None:
            return
        cols = ("needs", "working", "ready")
        for tv, key in zip(self.kanban_text_views, cols):
            rows = buckets.get(key) or []
            # On the right column, append DORMANT rows below FINISHED so
            # they remain visible without overflowing screen real-estate.
            extra = (
                ("dormant", buckets.get("dormant") or [])
                if key == "ready" else None
            )
            mas = self._build_single_bucket_attributed(key, rows, extra=extra)
            tv.textStorage().setAttributedString_(mas)

    @objc.python_method
    def _build_single_bucket_attributed(
        self,
        key: str,
        rows: list,
        extra: tuple | None = None,
    ) -> NSAttributedString:
        """Build attributed content for one kanban column."""
        header_font = NSFont.systemFontOfSize_weight_(10, NSFontWeightSemibold)
        title_font = NSFont.systemFontOfSize_weight_(13, NSFontWeightSemibold)
        gist_font = NSFont.systemFontOfSize_(12)
        meta_font = _rounded_tabular_font(11.0, NSFontWeightRegular)
        bar_font = NSFont.systemFontOfSize_(15)
        dim = NSColor.secondaryLabelColor()
        very_dim = NSColor.tertiaryLabelColor()

        out = NSMutableAttributedString.alloc().init()

        def append(text: str, attrs: dict) -> None:
            out.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(text, attrs)
            )

        color = _bucket_tint(key)

        # Header.
        append(f"  {LABELS[key]}  ·  {len(rows)}\n", {
            NSFontAttributeName: header_font,
            NSForegroundColorAttributeName: color,
            NSKernAttributeName: 0.8,
        })
        if not rows:
            append("  —\n", {
                NSFontAttributeName: gist_font,
                NSForegroundColorAttributeName: dim,
            })

        # Kanban columns are narrower than list mode — set the tab stop
        # closer in so the age sits at the column's right edge.
        col_right_edge = (POPOVER_KANBAN_SIZE[0] / 3.0) - 32

        for row in rows:
            self._append_row(
                out, row, color, title_font, gist_font, meta_font, bar_font,
                dim, very_dim,
                right_edge=col_right_edge,
                show_preview=(key != "dormant"),
            )

        if extra:
            extra_key, extra_rows = extra
            if extra_rows:
                ex_color = NSColor.tertiaryLabelColor()
                out.appendAttributedString_(
                    NSAttributedString.alloc().initWithString_attributes_(
                        f"\n  {LABELS[extra_key]}  ·  {len(extra_rows)}\n",
                        {
                            NSFontAttributeName: header_font,
                            NSForegroundColorAttributeName: ex_color,
                            NSKernAttributeName: 0.8,
                        },
                    )
                )
                for row in extra_rows:
                    self._append_row(
                        out, row, ex_color, title_font, gist_font,
                        meta_font, bar_font, dim, very_dim,
                        right_edge=col_right_edge,
                        show_preview=False,
                    )
        return out

    @objc.python_method
    def _row_paragraph_style(self, right_edge: float):
        """Paragraph style with a right tab stop, used so the age aligns
        to the right edge of the row."""
        ps = NSMutableParagraphStyle.alloc().init()
        tab = NSTextTab.alloc().initWithTextAlignment_location_options_(
            2,             # NSTextAlignmentRight
            right_edge,
            {},
        )
        ps.setTabStops_([tab])
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
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[textView_clickedOnLink] {e!r}\n")
            import traceback
            traceback.print_exc(file=sys.stderr)
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
                    show_preview: bool = True):
        """Render one session row with strong visual hierarchy:
          1. Big bold title + right-aligned age (via tab stop)
          2. Phase + gist line (secondary text)
          3. Quoted preview of Claude's most recent text (the actual
             content you'd want to read for context-switching)
          4. Project path (tertiary, smallest)
        Followed by a blank line to give the next row breathing room.
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
        preview = ""
        if show_preview and isinstance(meta, dict):
            raw = meta.get("lastAssistantText") or meta.get("lastAction") or ""
            if isinstance(raw, str) and raw.strip():
                # Collapse whitespace, truncate. 120 chars is enough to
                # tell you "what's going on" without flooding the popover.
                p = " ".join(raw.split())
                preview = p[:120] + ("…" if len(p) > 120 else "")

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
        append(f" {title}", {
            NSFontAttributeName: title_font,
            NSForegroundColorAttributeName: NSColor.labelColor(),
            NSParagraphStyleAttributeName: ps,
        })
        # Tab then right-aligned age in tertiary color.
        age_attrs = {
            NSFontAttributeName: meta_font,
            NSForegroundColorAttributeName: very_dim,
            NSParagraphStyleAttributeName: ps,
        }
        if unread and sid:
            # Append a clickable ✓ link AFTER the age. This is the
            # mark-as-read affordance. NSTextView's link handling fires
            # textView:clickedOnLink:atIndex: when the user clicks it.
            append(f"\t{ago}  ", age_attrs)
            append("✓", {
                NSFontAttributeName: title_font,
                NSForegroundColorAttributeName: accent,
                NSLinkAttributeName: NSURL.URLWithString_(f"cssread://{sid}"),
                NSParagraphStyleAttributeName: ps,
            })
            append("\n", age_attrs)
        else:
            append(f"\t{ago}\n", age_attrs)

        # ---- Line 2: phase + gist ----
        bits = []
        if phase:
            bits.append(phase)
        if gist:
            bits.append(gist)
        if bits:
            append(f"     {'  ·  '.join(bits)}\n", {
                NSFontAttributeName: gist_font,
                NSForegroundColorAttributeName: dim,
            })

        # ---- Line 3: quoted preview of Claude's actual content ----
        if preview:
            # Curly quotes + italic-ish styling so it reads as a snippet.
            quote_font = NSFont.systemFontOfSize_(12)
            append(f"     “{preview}”\n", {
                NSFontAttributeName: quote_font,
                NSForegroundColorAttributeName: dim,
            })

        # ---- Line 4: project path ----
        if cwd:
            append(f"     {cwd}\n", {
                NSFontAttributeName: meta_font,
                NSForegroundColorAttributeName: very_dim,
            })

        # Blank spacer line so rows don't run together.
        append("\n", {NSFontAttributeName: gist_font})

    @objc.python_method
    def _build_attributed(self, buckets: dict) -> NSAttributedString:
        """Render bucket headers + session rows into one attributed string
        for the list view. Hierarchy is: bigger bold title with the age
        right-aligned via a tab stop, secondary phase/gist line, a
        quoted preview of Claude's actual recent text (the context you'd
        want for switching back into the session), and a tertiary path."""
        header_font = NSFont.systemFontOfSize_weight_(10, NSFontWeightSemibold)
        title_font = NSFont.systemFontOfSize_weight_(14, NSFontWeightSemibold)
        gist_font = NSFont.systemFontOfSize_(12)
        meta_font = _rounded_tabular_font(11.0, NSFontWeightRegular)
        bar_font = NSFont.systemFontOfSize_(16)  # slightly taller for the ▎ glyph

        dim = NSColor.secondaryLabelColor()
        very_dim = NSColor.tertiaryLabelColor()

        # Right tab stop x position — slightly less than the popover
        # content width so the age sits with a small right margin.
        list_right_edge = POPOVER_LIST_SIZE[0] - 28

        out = NSMutableAttributedString.alloc().init()
        any_rows = False

        section_pairs = [(k, _bucket_tint(k)) for k in ("needs", "working", "ready")]
        section_pairs.append(("dormant", NSColor.tertiaryLabelColor()))

        for key, color in section_pairs:
            rows = buckets.get(key) or []
            if not rows:
                continue
            any_rows = True

            # Bucket header — uppercase, tinted, generous tracking.
            out.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(
                    f"  {LABELS[key]}  ·  {len(rows)}\n",
                    {
                        NSFontAttributeName: header_font,
                        NSForegroundColorAttributeName: color,
                        NSKernAttributeName: 0.8,
                    },
                )
            )

            for row in rows:
                self._append_row(
                    out, row, color,
                    title_font, gist_font, meta_font, bar_font,
                    dim, very_dim,
                    right_edge=list_right_edge,
                    # Dormant rows don't need the literal preview —
                    # they're stale by definition, save vertical space.
                    show_preview=(key != "dormant"),
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
        # don't pay setup cost on first click. Behavior=Transient means
        # macOS auto-dismisses on click-outside / Esc — exactly what we
        # want for a popover anchored to a small UI target.
        self.popover = NSPopover.alloc().init()
        self.popover.setBehavior_(NSPopoverBehaviorTransient)
        self.popover.setAnimates_(True)
        popover_vc = PopoverVC.alloc().init()
        # Hand the VC a reference to the popover so it can resize itself
        # when the user toggles between list and kanban.
        popover_vc.set_popover(self.popover)
        self.popover.setContentViewController_(popover_vc)
        # Initial content size matches the saved popover mode.
        initial_mode = _read_popover_mode()
        init_size = POPOVER_KANBAN_SIZE if initial_mode == "kanban" else POPOVER_LIST_SIZE
        self.popover.setContentSize_(NSMakeSize(*init_size))

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
            try:
                PID_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            NSApp.terminate_(None)
            return
        # Update badge counts.
        self.badge_view.set_counts(self._counts())
        # If popover is open, re-render its content too.
        if self.popover is not None and self.popover.isShown():
            vc = self.popover.contentViewController()
            if vc is not None and hasattr(vc, "refresh"):
                vc.refresh()

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
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[togglePanel] {e!r}\n")
            import traceback
            traceback.print_exc(file=sys.stderr)


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
