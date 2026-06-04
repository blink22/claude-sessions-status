# claude-sessions-status

<p>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-blue.svg" /></a>
  <img alt="macOS only" src="https://img.shields.io/badge/macOS-13%2B-lightgrey?logo=apple" />
  <img alt="Python" src="https://img.shields.io/badge/Python-3.9%2B-blue?logo=python&logoColor=white" />
  <img alt="Status: v0.5.0" src="https://img.shields.io/badge/status-v0.5.0%20preview-orange" />
</p>

> **A glance at every Claude Code session you've got running** — in your menu bar, a floating badge, or a full terminal dashboard.

When you're running several Claude Code sessions in parallel it's easy to lose track of which one is asking you a question, which is still working, and which finished an hour ago. `claude-sessions-status` reads Claude's local transcript files and shows you the state of every session at a glance — no Anthropic API key required, no daemon, no plugins to register inside Claude.

![Kanban popover — NEEDS YOU · WORKING · FINISHED columns](docs/screenshots/popover-kanban.png)

---

## 🚀 Quick start

```bash
curl -fsSL https://raw.githubusercontent.com/blink22/claude-sessions-status/main/install.sh | bash
```

The installer clones the repo, wires up SwiftBar (the menu-bar host), and walks you through optional setup (API key, Login Items). After it finishes, the menu-bar icon appears within ~5 seconds and you're done.

---

## What it shows

Sessions are sorted into four buckets:

| Bucket | Meaning |
|---|---|
| 🔔 **NEEDS YOU** | Claude asked a question, proposed a plan, or has been stuck for > 5 min. |
| ⚙️ **WORKING** | Claude is actively running tools — leave it alone. |
| 📥 **FINISHED** | Claude finished its turn and is waiting for your reply. |
| 💤 **DORMANT** | No real activity for > 30 min, or the Claude process has exited. |

---

## Four ways to view sessions

### 1. Menu bar (automatic after install)

Three live counts in your top-right. Click to drop down a grouped list of all sessions.

![SwiftBar dropdown showing sessions grouped by bucket](docs/screenshots/menubar-dropdown.png)

---

### 2. Floating glass badge + popover

```bash
claude-sessions-status badge
```

A small always-on-top capsule you can park anywhere on screen. Click it and a popover slides out attached to the badge.

![Floating glass badge — three tinted bucket counts](docs/screenshots/badge.png)

**Inside the popover:**

- **List ↔ Kanban toggle** — flip between a vertical list and a 3-column board.
- **Density picker** — `Glance` (title only) / `Focus` (standard card) / `Detail` (adds assistant preview). Persists across launches.

  <img src="docs/screenshots/density-dropdown.png" alt="Glance / Focus / Detail" width="120" />

- **Show Older** — adds a 4th DORMANT column for sessions you've drifted away from.

  ![Kanban with DORMANT column expanded](docs/screenshots/popover-kanban-dormant.png)

- **Click any card** to jump straight to that session — focuses the existing Terminal tab if live, or opens `claude --resume <id>` if not.
- **Mark all as read** — clears the unread indicator on NEEDS-YOU cards.

---

### 3. Always-on-top panel

```bash
claude-sessions-status panel              # list mode
claude-sessions-status panel --kanban     # kanban mode
claude-sessions-status panel --quit       # close
```

Same data as the popover, in a standalone draggable/resizable window pinned in front of everything.

---

### 4. Terminal dashboard

```bash
claude-sessions-status-dashboard                    # list
claude-sessions-status-dashboard --kanban           # 3-column kanban
claude-sessions-status-dashboard --show-dormant     # include dormant sessions
```

![Terminal kanban — three columns with live hotkey bar](docs/screenshots/terminal-kanban.png)

**Live hotkeys:**

| Key | Action |
|---|---|
| `k` | Kanban view |
| `l` | List view |
| `d` | Toggle dormant sessions |
| `r` | Force refresh |
| `q` | Quit |
| `1`–`9` | Resume the Nth session in a new Terminal window |

---

## Install options

**Option A — Shell installer (recommended)**

```bash
curl -fsSL https://raw.githubusercontent.com/blink22/claude-sessions-status/main/install.sh | bash
```

To pin a version: `CSS_REF=v0.2.0 bash <(curl -fsSL …/install.sh)`. To upgrade later: re-run the same command.

**Option B — Homebrew (coming soon)**

```bash
brew install blink22/claude-sessions-status/claude-sessions-status
claude-sessions-status install
```

The tap isn't published yet — use Option A in the meantime.

**Option C — Developer install**

```bash
git clone https://github.com/Blink22/claude-sessions-status ~/code/claude-sessions-status
cd ~/code/claude-sessions-status
./scripts/install.py setup --from-source
```

SwiftBar re-runs the script every 5 s — edits appear immediately, no reinstall needed.

---

## Subcommands

```
claude-sessions-status install          # interactive setup
claude-sessions-status uninstall        # reverse install
claude-sessions-status doctor           # verify wiring
claude-sessions-status logs             # tail the log file

claude-sessions-status badge            # floating capsule
claude-sessions-status badge --quit

claude-sessions-status panel            # always-on-top window
claude-sessions-status panel --kanban
claude-sessions-status panel --quit

claude-sessions-status-dashboard        # terminal view
claude-sessions-status-dashboard --kanban
claude-sessions-status-dashboard --show-dormant
claude-sessions-status-dashboard --kanban --save   # persist view choice
```

---

<details>
<summary><b>⚙️ Configuration</b></summary>

### Environment variables (`~/.claude-sessions-status.env`)

| Variable | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | _(unset)_ | Enables AI-generated task gists via Claude Haiku. Without it, the latest user prompt is shown verbatim. |
| `CLAUDE_SESSIONS_AI` | `0` | Set to `1` to turn on AI gist mode (requires the key above). |
| `CLAUDE_SESSIONS_HOURS` | `24` | How far back to show sessions (hours). |
| `CLAUDE_SESSIONS_LIMIT` | `12` | Max sessions to show. |
| `CLAUDE_SESSIONS_REFRESH` | `5` | Badge / terminal refresh interval (seconds). Menu-bar refresh is controlled by the SwiftBar filename suffix — rename the symlink to `claude-sessions-status.10s.py` to slow it down. |

### Override files in `~`

| File | Effect |
|---|---|
| `~/.claude-sessions-status.env` | Env-var config above. |
| `~/.claude-sessions-status-off` | Touchfile — mutes the menu bar (handy during screen recordings). |
| `~/.claude-sessions-status-prompt.txt` | _(reserved)_ Custom prompt for AI gist generation. |

### State files (auto-managed)

Delete one to reset that preference.

| File | Stores |
|---|---|
| `~/.claude-sessions-status-density` | Popover density: `glance` / `focus` / `detail` |
| `~/.claude-sessions-status-popover-mode` | Badge popover layout: `list` or `kanban` |
| `~/.claude-sessions-status-panel-mode` | Panel layout: `list` or `kanban` |
| `~/.claude-sessions-status-dashboard-mode` | Terminal layout: `list` or `kanban` |
| `~/.claude-sessions-status-show-dormant` | Touchfile — show DORMANT column in popover kanban |
| `~/.claude-sessions-status-seen.json` | Acknowledged NEEDS-YOU sessions |
| `~/.claude-sessions-status-badge.json` | Badge position |
| `~/.claude-sessions-status-window.json` | Panel window bounds |
| `~/.claude-sessions-status-cache.json` | Cached AI gist phrases |

### AI gist cost / privacy

AI gist mode calls Claude Haiku once per real conversation turn, per session — result cached until the transcript grows. Typical cost: single-digit cents/month. Without an API key the tool uses a free heuristic (latest user prompt, truncated) — no cloud calls.

</details>

---

<details>
<summary><b>🔧 Troubleshooting</b></summary>

**Start here:**

```bash
claude-sessions-status doctor
```

**Menu bar icon doesn't appear**
- SwiftBar not running → `open -a SwiftBar`
- Plugin symlink missing → `claude-sessions-status install` again
- Wrong plugin folder → SwiftBar Preferences → Plugin folder → `~/Library/Application Support/SwiftBar/Plugins`

**Menu bar shows stale data**

```bash
killall -USR1 SwiftBar
```

If the symlink lost its `.5s.py` suffix, SwiftBar treats the plugin as manual-refresh-only. Re-run the installer to fix.

**AI gist shows raw prompt instead of a phrase**

```bash
claude-sessions-status logs
```

Look for `CLAUDE_SESSIONS_AI is on but ANTHROPIC_API_KEY is empty` — the env file isn't being read.

**Floating badge doesn't appear**

```bash
claude-sessions-status badge --quit
claude-sessions-status badge
```

First launch installs `pyobjc-framework-Cocoa` into a venv (~10 s one-time setup).

**Badge is off-screen / invisible**

```bash
rm ~/.claude-sessions-status-badge.json
claude-sessions-status badge --quit && claude-sessions-status badge
```

**Terminal kanban looks broken**

Kanban needs ≥ 60 terminal columns. Below that it auto-falls back to list view. Resize wider or press `l` to switch manually.

</details>

---

<details>
<summary><b>❓ FAQ</b></summary>

**Does this work on Linux / Windows?**
No — macOS only. It depends on SwiftBar, AppleScript, and Claude for Desktop's local data paths.

**Do I need an Anthropic API key?**
No. Without a key you get the free heuristic (truncated latest prompt). With a key you get natural-language phrases like *"Fixing bottom sheet padding bug"* — cached aggressively.

**Will it slow down my machine?**
No. Each view polls Claude's local files on a 5-second tick, reading only file tails. No daemon. No network unless AI gist is on.

**My menu bar is already full of icons.**
Switch to `claude-sessions-status badge` and hide the menu-bar plugin, or use [Ice](https://github.com/jordanbaird/Ice) to tuck overflow icons behind a toggle.

**Why does the AI gist feel "behind"?**
It's cached per session, keyed on transcript file size — only regenerates on a real new turn. If Claude is thinking, the gist describes the previous turn. Intentional.

**Can the badge and menu bar run at the same time?**
Yes — they're independent views of the same data.

</details>

---

<details>
<summary><b>🏗 How it works</b></summary>

```
   ~/.claude/projects/*/<sess>.jsonl      Claude Code transcripts
   ~/Library/.../Claude/.../local_*.json  GUI-set session titles
              │
              ▼
   ┌──────────────────────────┐
   │   dashboard.py           │   shared core: phases / buckets /
   │   (session parser)       │   AI gist / title resolution
   └──────────────────────────┘
              │
   ┌──────────┼───────────┐
   ▼          ▼           ▼
  menubar.py  floating.py  dashboard.py
  (SwiftBar)  (PyObjC      (terminal)
              badge +
              panel +
              popover)
```

All three views are thin presentation layers over the same parser. No daemon — each view polls on a 5-second tick. Pure stdlib Python on the menu-bar / terminal side; the floating badge and panel use PyObjC (installed into `~/.claude-sessions-status-venv/`). macOS-only by design.

</details>

---

## Contributing

Patches welcome. Good starter areas: test coverage, GitHub Actions for ruff + smoke test, more phase-detection heuristics, Linux compatibility for the terminal dashboard.

Open an issue or PR — for larger changes, please open an issue first to discuss direction.

---

## License

MIT — see [LICENSE](./LICENSE).

Built and maintained at **Blink22**.
