# Changelog

All notable changes to this project are documented here. Format roughly
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.6.1 — 2026-06-06

### Fixed — exited sessions stuck in WORKING

- **Sessions you've exited no longer hang in the WORKING column.**
  Status was inferred purely from the transcript tail plus PID
  liveness, which wedged two ways: (1) a Claude Desktop session's
  PID belongs to a long-lived helper process that outlives the
  conversation, so liveness never cleared it; and (2) when you exit
  a task mid-turn the last transcript entry is your own message, which
  the heuristic reads as "Working…". Combined, an exited session could
  show WORKING for up to the 4-hour dormancy ceiling (or the 10-minute
  grace window). Now the app reads the authoritative `status`
  (`busy`/`idle`) field Claude ≥ 2.1.158 writes into
  `~/.claude/sessions/<pid>.json`, and derives an `exited` signal when
  a session has no live process and this Claude version manages PID
  files (it removes the file on quit). An idle or exited session is
  never classified as WORKING. Sessions from older Claude versions
  (no `status` field) fall back to the existing transcript heuristic
  unchanged. Applied consistently across the terminal dashboard,
  floating panel, and menubar so all three views agree.

## 0.6.0 — 2026-06-05

Multi-feature release on top of 0.5.0. Schema is fully backward-
compatible — existing `~/.claude-sessions-status-tasks.json` files
load unchanged; no migration required. The popover gains a left
project sidebar, a right tasks drawer, a unified top-bar pill
control group, unattached tasks, per-task copy/start affordances,
and a fix for the sub-agent dispatch mis-classification that put
working sessions in NEEDS YOU.

### Added — project sidebar (Idea 2.α)

- **Slim project rail (left side).** Kanban mode now opens with a
  200-px sidebar listing every project alongside the kanban columns.
  Projects are derived from each session's `cwd` (basename = display
  name, stable md5-hashed accent color from the six-color palette) —
  the same derivation already used by the tasks drawer, so projects
  show with consistent colors across both surfaces. Project items
  show a session count, or an alert count (red) if any session in
  that project is in NEEDS YOU.
- **Smart views.** The rail's bottom section adds two cross-project
  filters: *Needs you* (filters the kanban down to NEEDS YOU sessions
  across every project) and *Today* (sessions touched in the last 24
  hours). Both run as client-side filters — no Python round-trip —
  so toggling them is instant.
- **Project search.** A small input at the top of the rail filters
  the visible project list as you type (substring, case-insensitive).
  Esc clears the query. State persists in localStorage so the next
  popover open remembers your last view + search.
- **Sub-agent dispatch classification fix.** Sessions whose parent
  assistant just dispatched a Task/Agent sub-agent and is waiting on
  it no longer get pulled into NEEDS YOU by the "long structured
  text → looks like a plan" heuristic. A new `Awaiting sub-agent`
  phase routes those to the WORKING bucket directly, and stays
  stable across the existing 60-second sub-agent mtime grace window.

### Added — task management

- **Inline edit.** Click any user-authored task's text and the row
  swaps to an `<input>` with the caret at the end of the existing
  content. Enter saves, Esc / blur on unchanged value cancels.
  Suggested tasks stay read-only until approved. The task's id,
  createdAt, status, and source fields are preserved across the
  edit — only `content` plus a new optional `updatedAt` timestamp
  are touched.
- **Drag-to-reorder.** Open user-authored task rows are now
  HTML5-draggable. Pick a row up, see a blue insertion line on the
  top or bottom edge of any other row, drop to commit. Done tasks
  (`●`) and pending suggestions (`⋄`) stay sort-locked by their
  bucket rules — only the open user-authored bucket reorders.
  Backed by a new optional `position` integer per task; pre-existing
  tasks (no position) sort by `createdAt` so historical data renders
  identically.
- **Hover tooltip.** Hovering any task row briefly (~220ms) brings
  up a small dark tooltip near the cursor showing the full task
  description. Useful when a long task is truncated in a narrow
  kanban column, but renders for every task so the behavior is
  consistent. Position flips to avoid the viewport edge; hides
  during refresh ticks so it never orphans.

### Changed

- Tasks block on dormant cards stays fully visible (previously
  collapsed to just the `(done/total)` fraction). User feedback was
  that paused sessions still have unfinished work worth seeing.
- Cards stop visually jumping on the 5-second refresh tick when
  nothing changed in their data — identical-payload dedupe + per-
  card content diff replaces the wholesale `#app.innerHTML` rebuild
  with targeted innerHTML swaps that preserve cardEl DOM identity
  (and hover state).

### Fixed

- Edit Enter now reliably commits across IME-style keyboards. The
  keydown handler detects Enter via `key` / `keyCode` / `which`, plus
  a `keyup` fallback for layouts that only emit Enter on keyup. A
  `committed` flag prevents the two-event path from double-firing.
- Restored the `×` delete affordance and `✓/✗` suggestion controls
  that were accidentally dropped from the task-row render during
  the edit-feature refactor.
- Empty kanban cards now render a quiet `+ add task` affordance so
  users can create the first task without needing to discover an
  expand target that doesn't exist yet. Collapsed kanban cards that
  already have tasks also gain a small `+ add task` link below the
  preview rows so adding a second/third task is one click away.

### Notes

- Schema additions: `position` (int) and `updatedAt` (epoch) — both
  optional. The on-disk file from 0.5.0 loads without changes and
  the new fields are populated only on the first edit/reorder of
  each task.
- `tests/test_tasks_module.py` grew from 8 to 10 cases;
  `tests/test_bridge_dispatch.py` from 10 to 12 — all green. Run
  with `~/.claude-sessions-status-venv/bin/python3 tests/test_*.py`.

## 0.5.0 — 2026-06-04

The popover gains **per-session tasks** — a user-owned checklist per
Claude Code session, surfaced inline on each card. The juggler use case:
when you click into a session that's been dormant for an hour, you
immediately see what *you* asked it to do and what's left, instead of
having to scroll the transcript to remember. Tasks are user-authored
(not derived from the model's `TodoWrite`), persist across turns, and
travel with the session.

### Added

- **User-curated tasks per session.** Click `+ add task` on any card,
  type a task, hit Enter — it persists, survives badge restarts, and
  reappears whenever you open the popover. Click the glyph (`○`/`●`)
  to flip open/done; hover and click `×` to delete.
- **Inline kanban expansion.** In kanban mode, clicking the
  `tasks (n/m)` label expands the card in place to show the full task
  list + an input row. Click again (or click anywhere outside the
  tasks area) to collapse. Sibling cards in the column reflow down.
- **Always-on input in list mode.** The list view is 360px wide —
  plenty of room — so every card shows the full task list and the
  `+ add task` input by default. No click-to-expand needed.
- **Discoverable add-task affordance on populated kanban cards.** A
  small `+ add task` link sits below the preview rows on collapsed
  cards that already have tasks, so adding a second/third task
  doesn't require discovering the expand-the-label flow.
- **Tasks visible on dormant cards.** Dormant sessions no longer
  collapse their tasks to just a `(8/30)` fraction — the full row
  preview shows just like active cards, since paused work still has
  a to-do list worth seeing.
- **Persistence sidecar.** New file at
  `~/.claude-sessions-status-tasks.json`, atomic-replace writes, in-
  memory mtime cache, automatic recovery from corruption (backed up
  to `.corrupt-<ms-timestamp>`), automatic sweep of orphaned `.tmp`
  files left by a SIGKILL mid-write.
- **Schema-versioned forward-compat fields.** Each task carries a
  `source` (`user`/`suggested`) and `approved` flag. v0.5 only ever
  emits user-authored tasks; a future release will add an opt-in AI
  suggestion layer that uses the same shape without a migration.
- **Approve / reject UI** for AI-suggested tasks already wired in
  (dashed-glyph rendering, ✓/✗ controls). The Haiku sweeper that
  populates suggestions ships in a follow-up release — the
  interaction surface is ready when it lands.
- **`tests/` directory.** Two pure-Python regression suites:
  `test_tasks_module.py` (8 tests — CRUD, validation, unicode,
  10-thread concurrency, atomic writes, render order, corruption
  recovery) and `test_bridge_dispatch.py` (10 tests — every
  WKScriptMessageHandler action with synthetic JS payloads,
  malformed-input rejection, NSString-like duck types). Run with
  `~/.claude-sessions-status-venv/bin/python3 tests/test_*.py`.

### Changed — performance

- **Cards no longer jump on refresh ticks.** Two layered render
  optimizations: identical-payload dedupe skips no-op refreshes
  entirely; same-shape refreshes update only changed cards' inner
  HTML (cardEl divs stay in place, hover state preserved). The
  whole popover only repaints when sessions actually move buckets
  or appear/disappear.
- **Hot-reload extended.** Editing `scripts/kanban.html` already
  triggered a reload; that path now coexists cleanly with the new
  per-card render path.

### Notes

- **Backward compatibility:** existing user data in
  `~/.claude-sessions-status-*` files (seen state, badge position,
  density, mode preference, etc.) is unchanged. The new tasks file
  is additive — if it doesn't exist, the badge creates it on first
  task creation. No migration required from 0.4.0.
- **Data treated as production:** writes go through `_atomic_write_json`
  with tempfile + `os.replace`, single in-process lock, and corrupt
  files are quarantined rather than discarded.
- **No new runtime dependencies.** Same `pyobjc-framework-WebKit`
  introduced in 0.4.0.

## 0.4.0 — 2026-06-03

The popover gets a from-scratch redesign. Kanban and list views both
now render through a single WKWebView from `scripts/kanban.html`, with
a Linear-inspired dark theme: status circle on title, tinted phase
chip, wrapping gist + sub-agent descriptions, sticky column headers,
layered surfaces. List view finally matches the kanban's visual
language — same cards, same dark theme, just stacked vertically.

### Changed

- **Popover content now renders via WKWebView.** One HTML/CSS/JS
  template (`scripts/kanban.html`) drives both kanban and list
  modes; CSS branches on `body.mode-{kanban,list}` for layout. Clicks
  bridge back to Python via a `WKScriptMessageHandler` named `kanban`,
  routing to the same resume / mark-as-read handlers as before.
- **List view shares the kanban theme.** The old flat
  attributed-string list is replaced with the same dark cards used
  in kanban — status circle, phase chip, wrapping descriptions,
  hover affordance, click-to-resume — laid out as a vertical stack
  of sections with a single outer scroller.
- **Status circle on every title** (○ Needs You / ◐ Working / ●
  Finished / ○ Dormant). The bucket color rides on the circle, so
  the rest of the card stays calm.
- **Phase is a tinted pill chip** in the bucket color (red /
  amber / green / gray), with a middot separator before the gist
  text. Replaces the previous all-caps phase prefix.
- **Descriptions wrap, no longer truncate.** The session gist and
  per-sub-agent descriptions both flow onto multiple lines instead
  of clipping with an ellipsis, so long entries are visible at a
  glance.
- **Sticky per-column headers in kanban mode.** Cards scroll under
  the header; the header stays put with a hairline rule beneath.
- **Unread indicator is a small blue dot** in the card's top-right
  corner — click it to mark-as-read. Replaces the previous `✓` button.
- **Column headers in sentence case** with tabular counts:
  "Needs You 3", "Working 1", "Finished 12", "Dormant 4". Quieter
  than the previous all-caps colored headers; the column color
  signal moved to the cards.
- **Layered surfaces** — columns sit on a 3-4% lighter wash than
  the popover background so they read as distinct panes.

### Added

- New runtime dependency: `pyobjc-framework-WebKit`. The shell
  installer and venv bootstrap pick it up automatically; existing
  installs get it on the next launch.
- **Hot-reload of `kanban.html`** while the badge is running. On
  every refresh the badge stat()s the file and reloads if its mtime
  advanced — edit the CSS, save, see the change in ~5 seconds, no
  restart needed.

### Removed

- The native NSStackView + KanbanCardView + KanbanColumnDocView
  rendering path used by the popover (~610 lines of layer-backed
  AppKit drawing). The always-on-top panel (`floating.py` without
  `--badge`) is unaffected — it still uses its own native
  NSTextView-based rendering.

### Notes

- The popover top bar (segmented control, density picker, dormant
  toggle, mark-all-read) stays native AppKit — only the content
  area is the WebView.
- The resume-existing-host flow (Claude.app focus or Terminal tab
  focus by TTY match, falling back to spawning a new Terminal) is
  unchanged — the WebView routes clicks back to the same Python
  handler.

## 0.3.0 — 2026-05-31

Sessions can now see one level deeper: the **sub-agents** a parent
dispatches via Claude Code's `Task` / `Agent` tool show up on the same
card the parent does, so an "agent team" stops being invisible from
the dashboard's POV.

### Added

- **Sub-agent discovery.** The parser now reads
  `<projects>/<proj>/<sess-uuid>/subagents/agent-*.jsonl` (paired with
  the tiny `agent-*.meta.json`) instead of skipping nested transcripts.
  Each session row carries a `subagents` list + `subagent_summary`.
- **Per-running-agent brief on every card.** When a session has at
  least one sub-agent currently working, the card shows one line per
  active child: `◐ <agent_type> · <description>` — the description
  comes from the original `Task` call's `.meta.json`. Capped at 5
  visible rows with `+ N more working` if exceeded.
- **Three-state classifier** per sub-agent — `running` (file mtime
  inside a 60-second grace window), `interrupted` (last JSONL entry
  is the literal `[Request interrupted…]` user marker), `done`
  (otherwise). Cached per agent-jsonl path; terminal states are
  sticky and never re-read.

### Changed

- **A session with a running sub-agent is now classified as WORKING**,
  even if the parent transcript's own tail looks idle or has fallen
  into DORMANT by age. This rescues parent sessions that fire off
  long-running children and then quietly wait — without this override
  they would drift out of the WORKING column.
- **Done and interrupted sub-agents render invisibly.** The card only
  ever surfaces what's actively running; finished children carry no
  noise. This is a deliberate "right now" signal, not a history view.
  If a session has only finished children, the card renders exactly
  as it did pre-0.3.0.
- Consistent rendering across all five surfaces: terminal list,
  terminal kanban, badge popover (Focus + Detail), always-on-top
  panel, SwiftBar menu-bar dropdown. Cyan (terminal) / teal (popover
  + panel) / `#39c5cf` (SwiftBar).

### Docs

- README restructured for scannability: install + four views moved to
  the top, configuration / FAQ / troubleshooting / architecture
  collapsed into `<details>` toggles.
- Six in-tree screenshots wired into the README at the right sections.
- `install.sh` promoted to the primary install path; Homebrew tap
  marked "coming soon" until the tap repo is published.

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
