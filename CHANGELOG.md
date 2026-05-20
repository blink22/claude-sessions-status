# Changelog

All notable changes to this project are documented here. Format roughly
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.2.0 — 2026-05-20

This release is mostly about the **floating glass badge popover** and the
**terminal dashboard** catching up to each other in feature parity —
kanban view, density modes, and click-to-resume now work everywhere.

### Added

- **Density modes** in the badge popover: `glance` (title only),
  `focus` (4-line card, default), `detail` (richer assistant follow-on
  line). Chosen via an `NSPopUpButton` in the popover's top-left,
  persisted to `~/.claude-sessions-status-density`.
- **Kanban view in the popover**: 3 columns (`NEEDS YOU` / `WORKING` /
  `FINISHED`) toggled via an `NSSegmentedControl` in the popover top bar.
  Choice persists to `~/.claude-sessions-status-popover-mode`.
- **Optional 4th DORMANT column** in popover kanban, controlled by a
  "Show Older" checkbox. Renders to the right of FINISHED (not below it).
  State persists via the `~/.claude-sessions-status-show-dormant` touchfile.
- **"✓ Mark all N as read" button** in the kanban top-right that clears
  the unread indicator on NEEDS-YOU cards. Acknowledgements persist in
  `~/.claude-sessions-status-seen.json`.
- **Click-to-resume on cards and list rows**: clicking a session focuses
  its existing Terminal tab if live, otherwise spawns a new tab running
  `claude --resume <session-id>` in the session's `cwd`.
- **Badge visible on every Space**, including alongside full-screen apps,
  via `NSWindowCollectionBehavior` flags `canJoinAllSpaces | stationary |
  fullScreenAuxiliary`.
- **First-click on the popover is honored** — no more clicking twice to
  hit a card inside an inactive popover.
- **Terminal dashboard kanban view** with `--kanban`, `--list`,
  `--show-dormant`, and `--save` CLI flags. Falls back to list view
  with a banner when terminal width < 60 cols.
- **Live terminal hotkeys**: `k` (kanban), `l` (list), `d` (toggle
  dormant), `r` (force refresh), `q` (quit). `k`/`l` auto-persist.
- **Digit hotkeys `1`–`9` in the terminal dashboard** select the Nth
  visible session in display order and resume that Claude Code session in
  a new Terminal window via `claude --resume <session-id>`.
- **Terminal mode persistence** independent of the GUI views, in
  `~/.claude-sessions-status-dashboard-mode` — so the badge popover and
  the terminal can use different layouts without fighting.
- New state files documented in the README: `~/.claude-sessions-status-density`,
  `~/.claude-sessions-status-popover-mode`, `~/.claude-sessions-status-dashboard-mode`,
  `~/.claude-sessions-status-seen.json`, `~/.claude-sessions-status-badge.json`.

### Changed

- Badge position is now saved/restored across launches
  (`~/.claude-sessions-status-badge.json`); drag the badge anywhere and
  it stays put next time you launch.
- README restructured: "Three ways to view sessions" → "Four ways to view
  sessions" (the always-on-top panel got its own section), with the
  popover/terminal/panel kanban behavior documented in full.
- Popover layout auto-resizes between list / kanban / kanban-with-dormant
  widths so the kanban columns always have enough room.

### Fixed

- Kanban cards now respond to the very first click after the popover
  opens, instead of requiring a second click to activate the window first.
- Top-bar overlap in list mode (density popup vs. segmented control)
  resolved.
- Kanban "Mark all read" was missing in kanban mode in an earlier build —
  now present both as a footer link (list mode) and a top-bar button
  (kanban mode).
- Dormant column placement: previously stacked below FINISHED, now
  renders as a proper 4th column to the right.

## [0.1.0] — Initial public release

First open-source release. Three ways to view Claude Code sessions:

### Menu bar (SwiftBar plugin)

- Three-icon menu bar indicator (NEEDS YOU / WORKING / FINISHED) with live counts
- Dropdown groups sessions by bucket with full per-row detail
- Refreshes every 5 seconds; no daemon, just polling the local transcript files

### Floating glass badge (modern PyObjC widget)

- Always-on-top horizontal capsule (~150×36px) with three tinted-numeral counters
- Frosted-glass background via `NSVisualEffectView` (Popover material)
- Click to open an attached macOS-native `NSPopover` with the session list
- **Inside the popover: a List ↔ Kanban toggle** (NSSegmentedControl at the top)
  — choice persists across launches at `~/.claude-sessions-status-popover-mode`,
  popover auto-resizes between the two layouts (360→720 px wide)
- Draggable; position auto-saves
- P3 colors (Linear-grade urgency reds/amber/cyan-green), SF Pro Rounded
  tabular figures, hairline separator, hand-tuned typography hierarchy

### Terminal dashboard

- Full-screen grouped view (`claude-sessions-status-dashboard`)
- Same data as menu bar / badge — three modes share the same parser

### Core features (all three views)

- Per-session **AI gist** via Claude Haiku (optional, opt-in via `CLAUDE_SESSIONS_AI=1`)
  — caches results per session keyed on transcript file size, so each
  real turn triggers at most one API call
- Reads user-set session titles from Claude for Desktop's GUI metadata
  (not just JSONL fallbacks)
- Real conversation timestamps (filters out metadata-only writes that
  bump file mtime without representing actual progress)
- Detects "stuck" sessions (> 5 min no real activity) → routes to NEEDS YOU
- Detects `AskUserQuestion` and `ExitPlanMode` tool calls and routes those
  sessions to NEEDS YOU with the right hint

### Tooling

- Interactive `claude-sessions-status install` helper:
  - Auto-installs SwiftBar via Homebrew
  - Auto-bootstraps the PyObjC venv on first `badge` / `panel` launch
  - Writes config with `chmod 600`
  - Adds SwiftBar to macOS Login Items (optional, prompted)
- `doctor` subcommand to verify install state
- `uninstall` reverses each step with confirmation prompts
- `logs` tails `~/.claude-sessions-status.log`

### Three install paths

- **Homebrew tap**: `brew install Blink22/tap/claude-sessions-status`
- **Shell installer**: `curl -fsSL .../install.sh | bash`
- **Developer install**: `git clone` + `install.py setup --from-source`
  (symlinks SwiftBar to your local checkout so edits take effect on
  the next SwiftBar tick)
