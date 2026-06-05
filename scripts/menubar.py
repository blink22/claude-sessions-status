#!/usr/bin/env python3
# <bitbar.title>Claude Sessions Status</bitbar.title>
# <bitbar.author>claude-sessions-status</bitbar.author>
# <bitbar.author.github>claude-sessions-status</bitbar.author.github>
# <bitbar.desc>Live Claude Code session dashboard in the macOS menu bar.</bitbar.desc>
# <bitbar.dependencies>python3</bitbar.dependencies>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.refreshOnClick>true</swiftbar.refreshOnClick>
"""SwiftBar plugin: live Claude Code session status in the macOS menu bar.

Visual design:
- Menu bar TITLE shows one SF Symbol + count per non-empty bucket:
    NEEDS YOU   :bell.badge.fill:  red
    WORKING     :gearshape.2.fill: amber
    FINISHED    :tray.fill:        green
- Menu BODY groups sessions under colored, uppercase headers, with a
  DORMANT section at the bottom for stale/closed sessions.
- Each session shows its title, an icon + phase + AI-generated gist +
  age line, the project folder, and (for FINISHED / NEEDS YOU) a literal
  snippet of the most recent output.

Reuses session/phase/state detection from dashboard.py.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from dashboard import (  # noqa: E402
    find_sessions,
    recent_sessions,
    transcript_meta,
    infer_phase,
    state_for,
    next_action,
    format_ago,
    resolve_title,
    live_session_ids,
    session_runtime_status,
    runtime_status_for,
    pid_files_present,
    is_dormant,
    desktop_titles,
    session_gist,
    subagent_summary,
    subagents_for_session,
    SUBAGENT_MAX_DISPLAY,
    SUBAGENT_RUNNING,
    classify as _classify_canonical,
    BUCKET_NEEDS as _CANON_NEEDS,
    BUCKET_WORKING as _CANON_WORKING,
    BUCKET_READY as _CANON_READY,
    BUCKET_DORMANT as _CANON_DORMANT,
)

# ---------- sanitization ----------
# `|` is the SwiftBar parameter separator and newlines split lines, so
# both must be replaced in user-supplied text. Colons are NOT mangled —
# instead we disable symbol parsing on body rows via `symbolize=false`,
# which preserves text like "running tool: Bash" verbatim.
_SANITIZE = str.maketrans({"|": "／", "\n": " ", "\r": " "})


def _clean(s: str, limit: int = 80) -> str:
    s = (s or "").translate(_SANITIZE).strip()
    if len(s) <= limit:
        return s
    return s[: max(1, limit - 1)] + "…"


def _abbrev_home(path: str) -> str:
    home = os.path.expanduser("~")
    if path.startswith(home):
        return "~" + path[len(home) :]
    return path


# ---------- buckets + colors ----------
# Re-export the canonical bucket identifiers from dashboard.py so this
# file's existing references keep working without changes elsewhere.
BUCKET_NEEDS = _CANON_NEEDS
BUCKET_WORKING = _CANON_WORKING
BUCKET_READY = _CANON_READY
BUCKET_DORMANT = _CANON_DORMANT

# Active buckets — shown prominently. DORMANT is rendered separately at
# the bottom with reduced visual weight.
BUCKET_SPEC = [
    # key,          header label,  SF symbol,           color
    (BUCKET_NEEDS,   "NEEDS YOU",   "bell.badge.fill",   "#ff5555"),
    (BUCKET_WORKING, "WORKING",     "gearshape.2.fill",  "#d29922"),
    (BUCKET_READY,   "FINISHED",    "tray.fill",         "#3fb950"),
]
DORMANT_HEADER = "DORMANT"
DORMANT_COLOR = "#6e7681"   # dim gray, matches subtle text elsewhere


# Delegate to dashboard.classify so menubar and floating can't drift apart.
def classify(state: str, phase_label: str, runtime_status: str | None = None) -> str:
    return _classify_canonical(state, phase_label, runtime_status)


def urgency_key(row: dict) -> tuple:
    """Within NEEDS YOU: stuck > asking > plan, then by recency."""
    state = row["state"]
    phase = row["phase_label"]
    if state == "Maybe stuck":
        u = 0
    elif phase == "Asking you":
        u = 1
    elif phase == "Proposing a plan":
        u = 2
    else:
        u = 3
    return (u, row["ago_s"])


def _needs_lead_sf(row: dict) -> str:
    """Pick the SF Symbol that best represents the most urgent NEEDS YOU
    item — bell.badge is generic, but a triangle/question/clipboard makes
    the kind of attention clearer at a glance."""
    if row["state"] == "Maybe stuck":
        return "exclamationmark.triangle.fill"
    if row["phase_label"] == "Asking you":
        return "questionmark.bubble.fill"
    if row["phase_label"] == "Proposing a plan":
        return "list.bullet.clipboard.fill"
    return "bell.badge.fill"


# ---------- title rendering ----------
def render_title(buckets: dict) -> None:
    """Single-line title showing all non-empty buckets simultaneously.

    SwiftBar can only paint one color per title line, so we trade
    per-bucket tinting for "always visible". The three SF Symbol shapes
    (bell/gears/tray) carry the category signal even without color.
    Counts of zero are omitted so the bar stays compact.

    Color is left unspecified so the title adapts to light/dark menu-bar
    appearance the same way Apple's own status items do."""
    parts: list[str] = []
    for key, _, default_sf, _color in BUCKET_SPEC:
        rows = buckets[key]
        if not rows:
            continue
        sf = _needs_lead_sf(rows[0]) if key == BUCKET_NEEDS else default_sf
        parts.append(f":{sf}: {len(rows)}")
    if not parts:
        # Quiet state — a single muted icon so it's clear the app is alive
        # but nothing requires attention.
        print(":moon.zzz.fill: | color=#6e7681 sfsize=13")
        return
    print("  ".join(parts) + " | sfsize=14")


# ---------- session row (flat, no submenu) ----------
def _emit_session(row: dict, accent: str) -> None:
    """Render one session as 3 lines for WORKING, 4 lines for FINISHED /
    NEEDS YOU. The status line carries icon + phase + gist + time together
    so it's the visual focal point; the folder gets demoted to its own
    dim line below; the literal snippet is shown only where the actual
    content matters (preview the answer / read the question)."""
    s = row["s"]
    meta = row["meta"]
    title = _clean(row["title"], 60)
    ago = format_ago(row["ago_s"])
    project_full = meta.get("cwd") or ""
    project_abbrev = _abbrev_home(project_full) if project_full else ""
    emoji = row.get("emoji") or ""
    phase_label = row.get("phase_label") or ""
    gist = _clean(row.get("gist") or "", 80)
    snippet = _clean(meta.get("lastAction") or "", 110)
    bucket = row["bucket"]

    # Line 1: title only — phase emoji moved off this line to avoid being
    # shown twice (it also lives on the status line below). Clicking the
    # title opens the project in Finder.
    title_params = ["size=13", "trim=false", "symbolize=false"]
    if project_full:
        title_params += ["shell=open", f"param1={project_full}", "terminal=false"]
    print(f"{title} | {' '.join(title_params)}")

    # Line 2: the focal status line — phase icon + phase label + gist + age,
    # tinted by the bucket accent. This is the row a user reads first.
    bits: list[str] = []
    if phase_label:
        bits.append(phase_label)
    if gist:
        bits.append(gist)
    bits.append(ago)
    emoji_part = f"{emoji} " if emoji else ""
    status_text = f"{emoji_part}{'  ·  '.join(bits)}"
    print(
        f"   {_clean(status_text, 130)} | "
        f"size=11 color={accent} symbolize=false"
    )

    # Line 3: folder path on its own line (dim). Also clickable as a
    # second affordance for opening the project.
    if project_abbrev:
        path_params = ["size=10", "color=#8b949e", "symbolize=false"]
        if project_full:
            path_params += ["shell=open", f"param1={project_full}", "terminal=false"]
        print(f"   {project_abbrev} | {' '.join(path_params)}")

    # Line 4: literal snippet — only for FINISHED and NEEDS YOU buckets,
    # where the actual content (assistant's reply / Claude's question) is
    # what you want to preview before deciding to open the session.
    # WORKING gets nothing here — the gist already describes the action.
    if bucket in (BUCKET_READY, BUCKET_NEEDS) and snippet:
        print(f"   ↳ {snippet} | size=10 color=#a0a8b0 symbolize=false")

    # Lines 5+ (optional): one teal line per actively-running sub-agent,
    # using its .meta.json description as a brief ("◐ <type> · <desc>").
    # Done / interrupted children intentionally produce no lines — the
    # menu bar surface only reports what's happening RIGHT NOW.
    subs = row.get("subagents") or []
    running_subs = [s for s in subs if s.get("state") == SUBAGENT_RUNNING]
    if running_subs:
        teal = "#39c5cf"
        shown = running_subs[:SUBAGENT_MAX_DISPLAY]
        for sub in shown:
            atype = (sub.get("agent_type") or "").strip()
            desc = (sub.get("name") or "agent").strip()
            line = f"◐ {atype} · {desc}" if atype else f"◐ {desc}"
            print(f"   {_clean(line, 130)} | size=10 color={teal} symbolize=false")
        extra = len(running_subs) - len(shown)
        if extra > 0:
            print(
                f"   + {extra} more working | "
                f"size=10 color=#a0a8b0 symbolize=false"
            )


def _emit_dormant(row: dict) -> None:
    """Compact one-line representation for a dormant session. No hint or
    snippet — just title + age + project, all dim. Clicking opens the
    project in Finder so resuming the session is one click away."""
    s = row["s"]
    meta = row["meta"]
    title = _clean(row["title"], 50)
    ago = format_ago(row["ago_s"])
    project_full = meta.get("cwd") or ""
    project_abbrev = _abbrev_home(project_full) if project_full else ""
    bits = [title]
    if project_abbrev:
        bits.append(project_abbrev)
    bits.append(ago)
    text = _clean("  ·  ".join(bits), 110)
    params = [f"size=11", f"color={DORMANT_COLOR}", "symbolize=false"]
    if project_full:
        params += ["shell=open", f"param1={project_full}", "terminal=false"]
    print(f"{text} | {' '.join(params)}")


# ---------- main render ----------
def render_menubar() -> None:
    sessions = recent_sessions(find_sessions())
    now = time.time()

    live_ids = live_session_ids()
    status_map = session_runtime_status()
    tracking = pid_files_present()
    # Authoritative titles set in the Desktop GUI live outside the JSONL.
    desktop_idx = desktop_titles()

    enriched: list[dict] = []
    for s in sessions:
        full_path = s.get("fullPath", "")
        meta = transcript_meta(full_path)
        # Prefer the real conversation timestamp; fall back to the file's
        # mtime only if no user/assistant entry has a timestamp.
        last_epoch = meta.get("lastTurnEpoch")
        if last_epoch is None:
            last_epoch = s.get("fileMtime", 0) / 1000
        ago_s = now - last_epoch
        sid = s.get("sessionId") or ""
        runtime_status = runtime_status_for(sid, live_ids, status_map, tracking)
        emoji, phase_label = infer_phase(meta)
        state, _ = state_for(meta, ago_s, runtime_status)
        hint = next_action(phase_label, meta.get("lastRole"), ago_s)
        if state == "Working…" and meta.get("lastRole") == "assistant":
            hint = "Wait — Claude is mid-tool"
        # Classify into an active bucket first, then check if it should
        # be demoted to DORMANT. The original bucket informs the dormant
        # check (FINISHED ages out faster than NEEDS YOU / WORKING).
        active_bucket = classify(state, phase_label, runtime_status)
        if is_dormant(sid, ago_s, live_ids, active_bucket):
            bucket = BUCKET_DORMANT
        else:
            bucket = active_bucket
        subs = subagents_for_session(full_path, now)
        sub_sum = subagent_summary(subs)
        # Promote to WORKING when any sub-agent is actively running.
        # Mirrors dashboard.py:_prepare_row + floating.py:_get_buckets.
        if sub_sum.get("running"):
            bucket = BUCKET_WORKING
            state = "Working…"
        enriched.append({
            "s": s, "meta": meta, "ago_s": ago_s, "emoji": emoji,
            "phase_label": phase_label, "state": state,
            "hint": hint, "bucket": bucket, "live": sid in live_ids,
            # Resolve title once here so all renderers get the same answer
            # and we don't re-scan the Desktop index per session.
            "title": resolve_title(s, meta, desktop_idx),
            # Gist: short phrase like "Fixing bottom sheet padding". Free
            # heuristic by default; Haiku when TALK_BACK_DASH_AI=1.
            # Skipped for DORMANT to avoid spending API tokens on stale.
            "gist": session_gist(s, meta, bucket),
            # Sub-agents: full list + summary so _emit_session can list
            # each actively-running one with a brief.
            "subagents": subs,
            "subagent_summary": sub_sum,
        })

    buckets: dict[str, list[dict]] = {k: [] for k, *_ in BUCKET_SPEC}
    buckets[BUCKET_DORMANT] = []
    for row in enriched:
        buckets[row["bucket"]].append(row)
    for key in buckets:
        if key == BUCKET_NEEDS:
            buckets[key].sort(key=urgency_key)
        else:
            buckets[key].sort(key=lambda r: r["ago_s"])

    # ---- Title (cycling) ----
    render_title(buckets)
    print("---")

    if not enriched:
        print("No active sessions in the last 24h | color=#6e7681 sfimage=moon.zzz")
        print("---")
        print("Refresh now | refresh=true sfimage=arrow.clockwise")
        return

    # ---- Body: active groups + collapsed session rows ----
    first_group = True
    for key, label, _sf, accent in BUCKET_SPEC:
        rows = buckets[key]
        if not rows:
            continue
        if not first_group:
            print("---")
        first_group = False
        # Group header: uppercase, colored, with count chip.
        print(f"{label}  ·  {len(rows)} | size=10 color={accent}")
        for row in rows:
            _emit_session(row, accent)

    # ---- Dormant section (visually de-emphasized, at the bottom) ----
    dormant_rows = buckets[BUCKET_DORMANT]
    if dormant_rows:
        print("---")
        print(
            f"{DORMANT_HEADER}  ·  {len(dormant_rows)} | "
            f"size=10 color={DORMANT_COLOR}"
        )
        for row in dormant_rows:
            _emit_dormant(row)

    # ---- Footer ----
    print("---")
    print("Refresh now | refresh=true sfimage=arrow.clockwise")
    dash_path = str(SCRIPT_DIR / "dashboard.py")
    ascript = (
        f'tell app "Terminal" to do script "{dash_path}"'
    )
    print(
        f"Open full terminal dashboard | sfimage=rectangle.split.3x1 "
        f"shell=osascript param1=-e param2={ascript!r} terminal=false"
    )


if __name__ == "__main__":
    render_menubar()
