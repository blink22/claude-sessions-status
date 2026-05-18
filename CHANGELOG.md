# Changelog

All notable changes to this project are documented here. Format roughly
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
