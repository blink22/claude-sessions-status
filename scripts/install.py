#!/usr/bin/env python3
"""claude-sessions-status — install / uninstall / doctor / logs.

This is the user-facing entry point for the OSS tool. Run after `brew
install` (or directly from a checkout) to wire up the SwiftBar plugin,
write the config file, optionally collect an API key, and add SwiftBar
to macOS Login Items so it survives reboots.

Usage:
  claude-sessions-status install [--from-source]   # interactive setup
  claude-sessions-status uninstall                  # reverse it
  claude-sessions-status doctor                     # verify the wiring
  claude-sessions-status logs                       # tail the log file
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ---------- shared constants ----------
HOME = Path(os.path.expanduser("~"))
ENV_FILE = HOME / ".claude-sessions-status.env"
LOG_FILE = HOME / ".claude-sessions-status.log"
CACHE_FILE = HOME / ".claude-sessions-status-cache.json"
SOURCE_MARKER = HOME / ".claude-sessions-status-source"
MUTE_FILE = HOME / ".claude-sessions-status-off"
SWIFTBAR_PLUGINS = HOME / "Library/Application Support/SwiftBar/Plugins"
SWIFTBAR_SYMLINK = SWIFTBAR_PLUGINS / "claude-sessions-status.5s.py"
SCRIPT_DIR = Path(__file__).resolve().parent       # …/scripts/
MENUBAR_PY = SCRIPT_DIR / "menubar.py"
FLOATING_PY = SCRIPT_DIR / "floating.py"

# Dedicated venv for the floating-panel script. PyObjC is the only
# third-party requirement in the project; isolating it keeps the rest
# of the tool stdlib-only.
VENV_DIR = HOME / ".claude-sessions-status-venv"
VENV_PYTHON = VENV_DIR / "bin/python"

# State files written by floating.py — listed here so uninstall can
# clean them up.
PANEL_PID_FILE = HOME / ".claude-sessions-status-panel.pid"
PANEL_WINDOW_FILE = HOME / ".claude-sessions-status-window.json"
PANEL_MODE_FILE = HOME / ".claude-sessions-status-panel-mode"

MIN_PY = (3, 9)   # all scripts use `from __future__ import annotations`,
                  # so PEP-604 / PEP-585 syntax works on 3.9.


# ---------- terminal output helpers ----------
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def info(msg: str) -> None:
    print(f"  {msg}")


def step(msg: str) -> None:
    print(f"{BOLD}→{RESET} {msg}")


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}⚠{RESET} {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}✗{RESET} {msg}")


def prompt_yes(question: str, default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        try:
            ans = input(f"  {question}{suffix}").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            return False
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("  Please answer y or n.")


def prompt_str(question: str, *, secret: bool = False, allow_empty: bool = True) -> str:
    suffix = " (press Enter to skip): " if allow_empty else ": "
    if secret:
        import getpass
        try:
            return getpass.getpass(f"  {question}{suffix}").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            return ""
    try:
        return input(f"  {question}{suffix}").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return ""


# ---------- detection helpers ----------
def has_command(name: str) -> bool:
    return shutil.which(name) is not None


def swiftbar_app_installed() -> bool:
    return (
        Path("/Applications/SwiftBar.app").exists()
        or (HOME / "Applications/SwiftBar.app").exists()
    )


def swiftbar_is_running() -> bool:
    """Use AppleScript to ask System Events whether SwiftBar is running."""
    try:
        result = subprocess.run(
            [
                "osascript", "-e",
                'tell application "System Events" to count '
                '(every process whose name is "SwiftBar")',
            ],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() not in ("0", "")
    except (OSError, subprocess.SubprocessError):
        return False


def python_version_ok() -> bool:
    return sys.version_info >= MIN_PY


# ---------- Floating-panel venv ----------
def pick_venv_seed_python() -> str | None:
    """Find a Python interpreter capable of creating a venv. Apple's
    stock /usr/bin/python3 is too old/restricted on recent macOS for
    PyObjC; prefer Homebrew's Python if available."""
    candidates = [
        "/opt/homebrew/bin/python3.13",
        "/opt/homebrew/bin/python3.12",
        "/opt/homebrew/bin/python3.11",
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3.13",
        "/usr/local/bin/python3.12",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    return None


def venv_has_pyobjc() -> bool:
    """Check whether the floating-panel venv exists AND has PyObjC."""
    if not VENV_PYTHON.exists():
        return False
    res = subprocess.run(
        [str(VENV_PYTHON), "-c", "import AppKit, Foundation, objc"],
        capture_output=True, check=False,
    )
    return res.returncode == 0


def ensure_panel_venv() -> bool:
    """Create the dedicated venv and install PyObjC. Returns True on success."""
    if venv_has_pyobjc():
        ok(f"Floating-panel venv ready at {VENV_DIR}")
        return True
    seed = pick_venv_seed_python()
    if not seed:
        fail("No usable python3 found. Install Python 3.10+ via Homebrew first.")
        return False
    if not VENV_DIR.exists():
        step(f"Creating venv at {VENV_DIR} (using {seed})…")
        res = subprocess.run([seed, "-m", "venv", str(VENV_DIR)], check=False)
        if res.returncode != 0:
            fail("Failed to create venv.")
            return False
    step("Installing pyobjc-framework-Cocoa into the venv (one-time)…")
    res = subprocess.run(
        [str(VENV_PYTHON), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
        check=False,
    )
    res = subprocess.run(
        [str(VENV_PYTHON), "-m", "pip", "install", "--quiet", "pyobjc-framework-Cocoa"],
        check=False,
    )
    if res.returncode != 0 or not venv_has_pyobjc():
        fail("PyObjC install failed. The floating panel will be unavailable.")
        return False
    ok("Floating-panel venv ready")
    return True


def panel_is_running() -> int | None:
    """Return the PID of a running floating panel, or None."""
    if not PANEL_PID_FILE.exists():
        return None
    try:
        pid = int(PANEL_PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        # Stale pidfile
        try:
            PANEL_PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return None


# ---------- install steps ----------
def install_swiftbar_if_missing() -> bool:
    """Returns True if SwiftBar is installed (now or already)."""
    if swiftbar_app_installed():
        ok("SwiftBar is already installed")
        return True
    if not has_command("brew"):
        warn(
            "SwiftBar isn't installed and Homebrew isn't available either.\n"
            "    Install SwiftBar manually from https://swiftbar.app and re-run setup."
        )
        return False
    if not prompt_yes("SwiftBar is not installed. Install it via Homebrew now?"):
        warn("Skipping SwiftBar install. The menu bar won't appear until SwiftBar is running.")
        return False
    step("Installing SwiftBar via Homebrew (this may take a minute)…")
    res = subprocess.run(["brew", "install", "--cask", "swiftbar"], check=False)
    if res.returncode != 0:
        fail("brew install failed. Install SwiftBar manually and re-run setup.")
        return False
    ok("SwiftBar installed")
    return True


def write_env_file(api_key: str) -> None:
    """Create or update ~/.claude-sessions-status.env, preserving any
    other lines the user added."""
    lines: list[str] = []
    if ENV_FILE.exists():
        try:
            lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
    # Remove existing ANTHROPIC_API_KEY lines so we don't duplicate.
    lines = [
        ln for ln in lines
        if not ln.strip().startswith("ANTHROPIC_API_KEY=")
    ]
    if api_key:
        lines.insert(0, f"ANTHROPIC_API_KEY={api_key}")
    body = "\n".join(lines).rstrip() + "\n"
    try:
        ENV_FILE.write_text(body, encoding="utf-8")
        os.chmod(ENV_FILE, 0o600)
        ok(f"Wrote {ENV_FILE} (chmod 600)")
    except OSError as e:
        fail(f"Couldn't write env file: {e}")


def link_swiftbar_plugin(menubar_target: Path) -> None:
    """Create the symlink at ~/Library/Application Support/SwiftBar/Plugins/."""
    SWIFTBAR_PLUGINS.mkdir(parents=True, exist_ok=True)
    if SWIFTBAR_SYMLINK.is_symlink() or SWIFTBAR_SYMLINK.exists():
        try:
            SWIFTBAR_SYMLINK.unlink()
        except OSError as e:
            fail(f"Couldn't remove existing symlink: {e}")
            return
    try:
        os.symlink(menubar_target, SWIFTBAR_SYMLINK)
        ok(f"Symlinked {SWIFTBAR_SYMLINK.name} → {menubar_target}")
    except OSError as e:
        fail(f"Couldn't create symlink: {e}")


def launch_swiftbar() -> None:
    if swiftbar_is_running():
        ok("SwiftBar is already running")
        return
    step("Launching SwiftBar…")
    subprocess.run(["open", "-a", "SwiftBar"], check=False)


LOGIN_ITEM_NAME = "SwiftBar"
LOGIN_ITEM_PATH = "/Applications/SwiftBar.app"


def add_swiftbar_to_login_items() -> None:
    """Use AppleScript to add SwiftBar to macOS Login Items so it
    auto-starts after reboot. Idempotent — does nothing if already present."""
    if not Path(LOGIN_ITEM_PATH).exists():
        warn(
            f"{LOGIN_ITEM_PATH} not found — skipping Login Items setup."
        )
        return
    if not prompt_yes(
        "Add SwiftBar to macOS Login Items so the menu bar auto-starts after reboot?"
    ):
        return
    script = (
        f'tell application "System Events" to '
        f'if not (exists login item "{LOGIN_ITEM_NAME}") then '
        f'make login item at end with properties '
        f'{{name:"{LOGIN_ITEM_NAME}", path:"{LOGIN_ITEM_PATH}", hidden:false}}'
    )
    res = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, check=False
    )
    if res.returncode == 0:
        ok("SwiftBar added to Login Items (or was already present)")
    else:
        warn(f"Couldn't add to Login Items: {res.stderr.strip()}")


def remove_swiftbar_from_login_items() -> None:
    """Reverse of add_swiftbar_to_login_items()."""
    script = (
        f'tell application "System Events" to '
        f'if (exists login item "{LOGIN_ITEM_NAME}") then '
        f'delete login item "{LOGIN_ITEM_NAME}"'
    )
    subprocess.run(["osascript", "-e", script], check=False)


# ---------- subcommands ----------
def cmd_setup(args: argparse.Namespace) -> int:
    print(f"{BOLD}claude-sessions-status — interactive setup{RESET}\n")

    if not python_version_ok():
        fail(
            f"Python {MIN_PY[0]}.{MIN_PY[1]}+ is required (you have "
            f"{sys.version_info.major}.{sys.version_info.minor})."
        )
        return 1

    # Step 1: SwiftBar
    step("Checking SwiftBar…")
    install_swiftbar_if_missing()

    # Step 2: API key (optional)
    print()
    step("Anthropic API key (optional)")
    info(
        "If you provide one, the menu bar shows AI-generated phrases like\n"
        "  'Fixing bottom sheet padding bug' instead of raw user prompts.\n"
        "  Get one at https://console.anthropic.com/settings/keys.\n"
        "  Press Enter to skip and use the free heuristic."
    )
    existing_key = ""
    if ENV_FILE.exists():
        for ln in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if ln.startswith("ANTHROPIC_API_KEY="):
                existing_key = ln.partition("=")[2].strip()
                break
    if existing_key:
        info(f"(Found an existing key — leave blank to keep it.)")
    api_key = prompt_str("ANTHROPIC_API_KEY", secret=True)
    if not api_key:
        api_key = existing_key

    print()
    step("Writing config…")
    write_env_file(api_key)

    # Step 3: SwiftBar plugin symlink
    print()
    step("Wiring the SwiftBar plugin…")
    if args.from_source:
        info(f"Developer install: using {MENUBAR_PY} (from your local checkout).")
        # Write a marker file so doctor/uninstall know it's a dev install.
        try:
            SOURCE_MARKER.write_text(str(SCRIPT_DIR.parent) + "\n", encoding="utf-8")
            ok(f"Wrote source marker {SOURCE_MARKER}")
        except OSError as e:
            warn(f"Couldn't write source marker: {e}")
    link_swiftbar_plugin(MENUBAR_PY)

    # Step 4: Launch SwiftBar
    print()
    step("Launching SwiftBar…")
    launch_swiftbar()

    # Step 5: Login items
    print()
    add_swiftbar_to_login_items()

    # Done
    print(
        f"\n{GREEN}✓ Setup complete.{RESET}\n"
        f"  • Look at your menu bar — the icon should appear within ~5 seconds.\n"
        f"  • Send a message in Claude Code and watch the menu update.\n"
        f"  • Run {BOLD}claude-sessions-status doctor{RESET} to verify wiring.\n"
        f"  • Run {BOLD}claude-sessions-status-dashboard{RESET} for the full terminal view.\n"
    )
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    print(f"{BOLD}claude-sessions-status — uninstall{RESET}\n")

    if SOURCE_MARKER.exists():
        info(f"Detected developer install ({SOURCE_MARKER.read_text(encoding='utf-8').strip()})")

    if prompt_yes("Remove SwiftBar plugin symlink?"):
        if SWIFTBAR_SYMLINK.exists() or SWIFTBAR_SYMLINK.is_symlink():
            try:
                SWIFTBAR_SYMLINK.unlink()
                ok(f"Removed {SWIFTBAR_SYMLINK}")
            except OSError as e:
                fail(f"Couldn't remove symlink: {e}")
        else:
            info("(symlink was already gone)")

    if prompt_yes(f"Delete config file {ENV_FILE}?", default=False):
        try:
            ENV_FILE.unlink(missing_ok=True)
            ok(f"Removed {ENV_FILE}")
        except OSError as e:
            fail(f"Couldn't remove env file: {e}")

    if prompt_yes(f"Delete AI gist cache {CACHE_FILE}?"):
        try:
            CACHE_FILE.unlink(missing_ok=True)
            ok(f"Removed {CACHE_FILE}")
        except OSError as e:
            warn(str(e))

    if prompt_yes(f"Delete log file {LOG_FILE}?"):
        try:
            LOG_FILE.unlink(missing_ok=True)
            ok(f"Removed {LOG_FILE}")
        except OSError as e:
            warn(str(e))

    if SOURCE_MARKER.exists():
        try:
            SOURCE_MARKER.unlink()
            ok(f"Removed {SOURCE_MARKER}")
        except OSError as e:
            warn(str(e))

    if prompt_yes("Remove SwiftBar from macOS Login Items?", default=False):
        remove_swiftbar_from_login_items()
        ok("Login Items entry removed (if it was present)")

    # Floating panel cleanup — uses the same file-flag protocol as
    # `panel --quit` so the window saves position before exiting.
    existing = panel_is_running()
    if existing:
        if prompt_yes(f"Quit the running floating panel (pid {existing})?"):
            try:
                (HOME / ".claude-sessions-status-panel-quit").touch()
                ok(f"Asked pid {existing} to quit (within ~5s)")
            except OSError as e:
                warn(str(e))

    if VENV_DIR.exists():
        if prompt_yes(f"Delete the floating-panel venv at {VENV_DIR}?", default=False):
            import shutil as _sh
            try:
                _sh.rmtree(VENV_DIR)
                ok(f"Removed {VENV_DIR}")
            except OSError as e:
                warn(str(e))

    for f in (PANEL_PID_FILE, PANEL_WINDOW_FILE, PANEL_MODE_FILE):
        if f.exists():
            try:
                f.unlink(missing_ok=True)
                ok(f"Removed {f}")
            except OSError as e:
                warn(str(e))

    print(f"\n{GREEN}Uninstall complete.{RESET}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"{BOLD}claude-sessions-status — doctor{RESET}\n")

    failures = 0

    # Python version
    if python_version_ok():
        ok(f"Python {sys.version_info.major}.{sys.version_info.minor} (OK)")
    else:
        fail(f"Python {sys.version_info.major}.{sys.version_info.minor} (need ≥ 3.10)")
        failures += 1

    # SwiftBar install + running
    if swiftbar_app_installed():
        ok("SwiftBar is installed")
    else:
        fail("SwiftBar is not installed")
        failures += 1
    if swiftbar_is_running():
        ok("SwiftBar is running")
    else:
        fail("SwiftBar is not running (run `open -a SwiftBar`)")
        failures += 1

    # Plugin symlink
    if SWIFTBAR_SYMLINK.is_symlink():
        target = os.readlink(SWIFTBAR_SYMLINK)
        if Path(target).exists():
            ok(f"SwiftBar plugin linked → {target}")
        else:
            fail(f"SwiftBar plugin symlink points to a missing file: {target}")
            failures += 1
    elif SWIFTBAR_SYMLINK.exists():
        warn(f"SwiftBar plugin exists but isn't a symlink: {SWIFTBAR_SYMLINK}")
    else:
        fail(f"SwiftBar plugin symlink is missing: {SWIFTBAR_SYMLINK}")
        failures += 1

    # Env file
    if ENV_FILE.exists():
        mode = oct(ENV_FILE.stat().st_mode)[-3:]
        if mode == "600":
            ok(f"Env file present ({ENV_FILE}, mode 600)")
        else:
            warn(f"Env file mode is {mode}, recommend 600 — run: chmod 600 {ENV_FILE}")
    else:
        warn(f"Env file missing ({ENV_FILE}) — AI gist mode will be disabled")

    # Source marker (dev install)
    if SOURCE_MARKER.exists():
        src = SOURCE_MARKER.read_text(encoding="utf-8").strip()
        info(f"Developer install detected (source: {src})")
        if has_command("brew") and Path("/opt/homebrew/bin/claude-sessions-status").exists():
            warn(
                "Both a Homebrew install and a developer install are present.\n"
                "  Pick one and uninstall the other to avoid surprises."
            )

    # Mute touchfile
    if MUTE_FILE.exists():
        warn(f"Menu bar is muted ({MUTE_FILE}). Remove the file to re-enable.")

    # Floating panel — optional, not failure-counted.
    if venv_has_pyobjc():
        ok(f"Floating-panel venv ready ({VENV_DIR})")
        existing_pid = panel_is_running()
        if existing_pid:
            ok(f"Floating panel is running (pid {existing_pid})")
        else:
            info("Floating panel is not currently running "
                 "(launch with `claude-sessions-status panel`).")
    else:
        info(
            "Floating panel is not set up (optional). "
            "Run `claude-sessions-status panel` to install on demand."
        )

    print()
    if failures == 0:
        print(f"{GREEN}{BOLD}All checks passed.{RESET}")
        return 0
    print(f"{RED}{BOLD}{failures} check(s) failed.{RESET}")
    return 1


def cmd_logs(args: argparse.Namespace) -> int:
    if not LOG_FILE.exists():
        info(f"No log file at {LOG_FILE} yet. Nothing to show.")
        return 0
    # Just exec `tail -f` and stay attached.
    try:
        subprocess.run(["tail", "-n", "50", "-f", str(LOG_FILE)])
    except KeyboardInterrupt:
        pass
    return 0


def cmd_panel(args: argparse.Namespace) -> int:
    """Launch the always-on-top floating panel.

    Architecture: the panel is a separate Python process running in a
    dedicated venv that has PyObjC installed (the rest of the project is
    stdlib-only, so we keep the third-party dep isolated). We spawn it
    detached so the user's shell returns immediately."""
    if args.quit:
        pid = panel_is_running()
        if not pid:
            info("No floating panel is running.")
            return 0
        # Ask the panel to quit via the file-flag protocol; POSIX signal
        # delivery inside PyObjC's NSApp.run() runloop is unreliable.
        quit_flag = HOME / ".claude-sessions-status-panel-quit"
        try:
            quit_flag.touch()
            ok(f"Asked floating panel (pid {pid}) to quit "
               f"(will exit within ~5s on its next refresh tick).")
        except OSError as e:
            fail(f"Couldn't request quit: {e}")
            return 1
        return 0

    # Ensure venv + PyObjC are in place.
    if not ensure_panel_venv():
        return 1

    # Already running? Optionally show how to kill it.
    existing = panel_is_running()
    if existing:
        info(
            f"Floating panel is already running (pid {existing}).\n"
            f"  Quit it first with `claude-sessions-status panel --quit`."
        )
        return 0

    # Build the command line for floating.py.
    cmd = [str(VENV_PYTHON), str(FLOATING_PY)]
    if args.kanban:
        cmd.append("--kanban")
    elif args.list_mode:
        cmd.append("--list")

    # Spawn detached so this command returns immediately.
    step("Launching floating panel…")
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        fail(f"Couldn't spawn floating panel: {e}")
        return 1
    ok("Floating panel launched. Look for the 'Claude Sessions' window.")
    info("Toggle list/kanban: ⌘⇧K inside the window. Quit: ⌘Q.")
    return 0


def cmd_badge(args: argparse.Namespace) -> int:
    """Launch the small circular floating badge. Click it to toggle the
    detail panel (list or kanban). Reuses the same venv + floating.py
    process as `panel` — they're mutually exclusive (one process at a
    time)."""
    if args.quit:
        if not panel_is_running():
            info("No floating badge/panel is running.")
            return 0
        pid = panel_is_running()
        try:
            (HOME / ".claude-sessions-status-panel-quit").touch()
            ok(f"Asked floating process (pid {pid}) to quit (within ~5s).")
        except OSError as e:
            fail(f"Couldn't request quit: {e}")
            return 1
        return 0

    if not ensure_panel_venv():
        return 1
    existing = panel_is_running()
    if existing:
        info(
            f"Floating process is already running (pid {existing}).\n"
            f"  Quit it first with `claude-sessions-status badge --quit`."
        )
        return 0

    cmd = [str(VENV_PYTHON), str(FLOATING_PY), "--badge"]
    if args.kanban:
        cmd.append("--kanban")
    elif args.list_mode:
        cmd.append("--list")

    step("Launching floating badge…")
    try:
        subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        fail(f"Couldn't spawn badge: {e}")
        return 1
    ok("Badge launched. Click it to open/close the detail panel.")
    info("Drag the badge to reposition. Quit: `… badge --quit` or ⌘Q on the badge.")
    return 0


# ---------- entry ----------
def main() -> int:
    parser = argparse.ArgumentParser(
        prog="claude-sessions-status",
        description="Install / configure / inspect claude-sessions-status.",
    )
    sub = parser.add_subparsers(dest="cmd")

    p_setup = sub.add_parser("install", help="Interactive setup")
    p_setup.add_argument(
        "--from-source", action="store_true",
        help="Developer install: symlink from your local clone instead of the system location.",
    )
    p_setup.set_defaults(func=cmd_setup)

    # 'setup' as an alias of 'install' for parity with shell installers.
    p_setup2 = sub.add_parser("setup", help="Alias of `install`")
    p_setup2.add_argument("--from-source", action="store_true")
    p_setup2.set_defaults(func=cmd_setup)

    p_un = sub.add_parser("uninstall", help="Reverse setup interactively")
    p_un.set_defaults(func=cmd_uninstall)

    p_doc = sub.add_parser("doctor", help="Verify install state")
    p_doc.set_defaults(func=cmd_doctor)

    p_log = sub.add_parser("logs", help="Tail ~/.claude-sessions-status.log")
    p_log.set_defaults(func=cmd_logs)

    p_panel = sub.add_parser(
        "panel", help="Launch the always-on-top floating panel"
    )
    p_panel.add_argument("--kanban", action="store_true",
                         help="Open in kanban (three-column) mode")
    p_panel.add_argument("--list", dest="list_mode", action="store_true",
                         help="Open in vertical-list mode (default)")
    p_panel.add_argument("--quit", action="store_true",
                         help="Quit any running floating panel")
    p_panel.set_defaults(func=cmd_panel)

    p_badge = sub.add_parser(
        "badge",
        help="Launch the small circular floating badge "
             "(click it to expand into the panel)",
    )
    p_badge.add_argument("--kanban", action="store_true",
                         help="Panel opens in kanban mode when clicked")
    p_badge.add_argument("--list", dest="list_mode", action="store_true",
                         help="Panel opens in list mode when clicked (default)")
    p_badge.add_argument("--quit", action="store_true",
                         help="Quit any running floating badge")
    p_badge.set_defaults(func=cmd_badge)

    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
