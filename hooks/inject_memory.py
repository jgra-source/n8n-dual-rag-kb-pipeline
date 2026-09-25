#!/usr/bin/env python3
"""Recall hook: put the memory index in front of Claude on every prompt.

Plain words: before Claude answers, this quietly hands it a one-line list of
every fact we've ever saved -- and, when the prompt looks like a strategy idea,
the list of experiments already refuted. Claude then matches by MEANING, which
is what grep cannot do. A rejected idea phrased in new words still gets caught.

Registered on UserPromptSubmit. Reads only; writes nothing.

SAFETY: this runs on EVERY prompt in EVERY session. It is written to fail
silent -- any error at all means "print nothing, exit 0". It must never block
a prompt (exit 2) and never slow one down (no network, four file reads max).

Budget: Claude Code caps injected context at 10,000 characters. We target 9,000
and spend it adaptively. Every scope is always included -- what changes is how
much room each fact gets:
  - ordinary prompt  -> name + description per fact, grouped by scope
  - strategy or memory prompt -> names only (~1/4 the space), and the REJECTED
                        mechanism list gets the rest (that is the GATE 0 case,
                        where priors matter more than fact descriptions)
  - bare continuation ("approved", "go") -> names only, no REJECTED block: there
                        is no topic to match facts against
Whatever still overflows is trimmed by dropping entries from the biggest block
first (see fit()). Scope drives grouping and ordering, NOT inclusion.

Usage:
    inject_memory.py              # hook mode: reads hook JSON on stdin
    inject_memory.py --selftest   # prints both paths + sizes, injects nothing
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

TOTAL_CAP = 9000  # keep clear of Claude Code's 10,000-char injection ceiling
DESC_CAP = 110  # per-fact description truncation
MECH_CAP = 72  # per-REJECTED-mechanism truncation (strategy path only)
PROSE_CAP = 420  # per-section prose truncation (distilled principles)
# A bare continuation ("approved", "yes go", "do it") names no topic, so full
# fact descriptions cannot be matched against anything -- and the prompt it
# continues already carried the whole index earlier in the same context. Longer
# than this and there is real content worth spending descriptions on.
CONTINUATION_CAP = 24  # chars

# Copied into another repo, hooks/ no longer sits inside second-brain, so
# __file__'s parent.parent is the wrong repo's root. SECOND_BRAIN_ROOT lets a
# copied hook point back at the real vault; unset (the common case, running
# inside second-brain itself) keeps the old self-relative behavior.
REPO = Path(os.environ.get("SECOND_BRAIN_ROOT", str(Path(__file__).resolve().parent.parent)))
INDEX = REPO / "INDEX.md"

# REJECTED.md lives in the trading repo, beside this one. Env var wins so the
# hook stays usable from other checkouts (and from cloud, where it won't exist).
REJECTED = Path(
    os.environ.get(
        "SECOND_BRAIN_REJECTED",
        str(REPO.parent / "crypto-ai-agent" / "docs" / "REJECTED.md"),
    )
)

# This repo's own do-not-re-litigate registry. Unlike the trading one it IS in
# this repo, so it works in cloud routines too.
SELF_REJECTED = REPO / "docs" / "REJECTED.md"

INDEX_LINE = re.compile(
    r"^- \[(?P<name>[^\]]+)\]\((?P<path>[^)]+)\)\s+—\s+(?P<desc>.*?)\s*"
    r"`\[(?P<verified>\d{4}-\d{2}-\d{2})\]`\s*$"
)
SCOPE_HDR = re.compile(r"^## (?P<scope>[a-z0-9\-]+) \(\d+\)\s*$")
REJ_HDR = re.compile(r"^## (?P<section>.+?)\s*$")
REJ_LINE = re.compile(r"^- (?P<date>\d{4}-\d{2}-\d{2}) \| (?P<mech>[^|]+?)\s*\|")

# Generous on purpose. A false positive only reallocates budget; a false
# negative means a refuted idea slips through GATE 0. Recall beats precision.
STRATEGY_WORDS = {
    "apextrade", "atr", "backtest", "basket", "bear", "boss", "breakout", "bull",
    "candle", "choch", "drawdown", "ema", "entries", "entry", "exit", "exits",
    "fade", "filter", "fill", "gate", "hold", "indicator", "ladder", "lever",
    "leverage", "liquidation", "long", "momentum", "optimise", "optimize", "pf",
    "position", "profit", "promote", "razzle", "rd", "regime", "reject", "risk",
    "rsi", "shorts", "signal", "size", "sizing", "sl", "stop", "strategy",
    "sweep", "takeprofit", "threshold", "tp", "trade", "trades", "trading",
    "trail", "trigger", "tweak", "veto", "volatility", "volume", "win",
}
WORD = re.compile(r"[a-z0-9]+")


# The hook payload's prompt field name is not something to guess at: guessing it
# wrong fails SILENTLY (empty prompt -> always the ordinary path -> GATE 0 never
# fires), which is the worst possible failure for a safety gate. Try every known
# spelling, then fall back to scanning the whole payload -- the prompt text is in
# there somewhere regardless of what the field is called.
PROMPT_FIELDS = ("prompt", "userMessage", "user_message", "message", "text")


def extract_prompt(raw: str) -> tuple[str, str]:
    """-> (prompt text, where it came from). Never raises."""
    if not raw.strip():
        return "", "empty-stdin"
    try:
        payload = json.loads(raw)
    except Exception:
        return raw, "raw-not-json"
    if isinstance(payload, dict):
        for field in PROMPT_FIELDS:
            value = payload.get(field)
            if isinstance(value, str) and value.strip():
                return value, field
            if isinstance(value, list):  # content-block form
                parts = [
                    b.get("text", "")
                    for b in value
                    if isinstance(b, dict) and isinstance(b.get("text"), str)
                ]
                if any(p.strip() for p in parts):
                    return "\n".join(parts), f"{field}[blocks]"
    # Nothing matched a known field. Scan the whole payload rather than go blind.
    return raw, "raw-fallback"


# Changes to THIS system have their own refuted list. Same principle as GATE 0:
# a registry nothing consults is a diary.
MEMORY_WORDS = {
    "baseline", "cadence", "consolidate", "consolidation", "embedding",
    "embeddings", "fact", "facts", "gitignore", "hermes", "hook", "hooks",
    "index", "inject", "injection", "memory", "pinecone", "recall", "remember",
    "routine", "routines", "scope", "scopes", "second-brain", "secondbrain",
    "supabase", "vault", "verified",
}


def looks_like_strategy(prompt: str) -> bool:
    if not prompt:
        return False
    return bool(STRATEGY_WORDS & set(WORD.findall(prompt.lower())))


def looks_like_memory_change(prompt: str) -> bool:
    if not prompt:
        return False
    return bool(MEMORY_WORDS & set(WORD.findall(prompt.lower())))


def read_index() -> list[tuple[str, str, str, str]]:
    """-> [(scope, name, desc, verified)] from the generated INDEX.md."""
    if not INDEX.is_file():
        return []
    out: list[tuple[str, str, str, str]] = []
    scope = "global"
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        h = SCOPE_HDR.match(line)
        if h:
            scope = h.group("scope")
            continue
        m = INDEX_LINE.match(line)
        if m:
            desc = m.group("desc")
            if len(desc) > DESC_CAP:
                desc = desc[: DESC_CAP - 1].rstrip(" ,;·—-") + "…"
            out.append((scope, m.group("name"), desc, m.group("verified")))
    return out


def read_rejected(source: Path = REJECTED) -> list[tuple[str, list[str], str]]:
    """-> [(section, [mechanism, ...], prose)] from REJECTED.md.

    Some sections are a distilled principle in prose rather than a list -- the
    capture-vs-total-R trap is the clearest example, and it is one of the most
    load-bearing lessons in the file. Those must survive too, so prose is
    captured alongside mechanism names.
    """
    if not source.is_file():
        return []
    sections: list[tuple[str, list[str], list[str]]] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        h = REJ_HDR.match(line)
        if h:
            sections.append((h.group("section").strip(), [], []))
            continue
        if not sections:
            continue
        m = REJ_LINE.match(line)
        if m:
            sections[-1][1].append(" ".join(m.group("mech").split()))
        elif line.strip() and not line.lstrip().startswith((">", "-", "|", "#")):
            sections[-1][2].append(line.strip())
    out: list[tuple[str, list[str], str]] = []
    for title, mechs, prose in sections:
        blob = " ".join(prose)
        if len(blob) > PROSE_CAP:
            blob = blob[: PROSE_CAP - 1].rstrip() + "…"
        if mechs or blob:
            out.append((title, mechs, blob))
    return out


def compress_mech(mech: str) -> str:
    """Keep the identifying head of a mechanism name, drop its explanation tail.

    REJECTED.md names carry long parentheticals. The head alone is enough to make
    Claude think "that resembles something" and go read the file, which is the
    only job this list has.
    """
    for sep in (" (", " — ", " -- ", ", from ", ": "):
        i = mech.find(sep)
        if i >= 14:  # keep enough to stay identifiable
            mech = mech[:i]
            break
    mech = mech.strip().rstrip(",;:")
    if len(mech) > MECH_CAP:
        mech = mech[: MECH_CAP - 1].rstrip() + "…"
    return mech


def fit_sections(
    sections: list[tuple[str, list[str], str]], budget: int
) -> list[str]:
    """Render every REJECTED section within budget.

    Trims the largest section one entry at a time so that NO section can ever
    disappear. A silently half-empty safety gate is worse than a visibly
    trimmed one, so anything dropped is counted in the output.
    """
    kept = [list(m) for _, m, _ in sections]
    dropped = [0] * len(sections)

    def render() -> list[str]:
        out: list[str] = []
        for (title, _, prose), mechs, drop in zip(sections, kept, dropped):
            more = f" (+{drop} more in REJECTED.md)" if drop else ""
            out.append(f"### {title}{more}")
            if mechs:
                out.append("; ".join(mechs))
            elif not prose:
                out.append("(all entries trimmed — read the file)")
            if prose:
                out.append(prose)
        return out

    while len("\n".join(render())) > budget:
        biggest = max(range(len(kept)), key=lambda i: len(kept[i]))
        if not kept[biggest]:
            break  # nothing left to give; render() already flags it
        kept[biggest].pop()
        dropped[biggest] += 1
    return render()


def classify_path(prompt: str) -> str:
    """Which budget path build() will take. Diagnostic only -- keeps the log honest."""
    if looks_like_strategy(prompt):
        return "strategy"
    if looks_like_memory_change(prompt):
        return "memory"
    if len(prompt.strip()) <= CONTINUATION_CAP:
        return "continuation"
    return "ordinary"


# Reply rules, injected on EVERY prompt, deliberately AFTER the memory block's
# closing tag. Inside it they would be labelled "context, not instructions" and
# read as background; these are the opposite -- they govern the reply itself.
#
# Why at prompt time rather than session start: the ten rules already live in
# ~/.claude/CLAUDE.md, which loads once at the top of a session. On a long,
# tool-heavy turn the reply gets written from working state and the rules are
# 100k tokens behind. This puts them adjacent to the reply instead.
#
# Kept short on purpose -- it costs context on every single prompt.
REPLY_RULES = [
    "<reply-rules>",
    "Not context — these govern the reply you are about to write.",
    "",
    "DEFAULT (chat, code, internal work):",
    "- Before any change OR proposal: what it does / why / what breaks without it,",
    "  in plain words, in the same reply. Can't say all three simply → don't do it.",
    "- Lead with the next action. One line of current state. End with one next step.",
    "- No preamble, no recap, no closers. Cap lists at 5. Tangents go in the file.",
    "- On finishing: what was achieved / why / what would have broken — name what is",
    "  still unproven in the same plain terms.",
    "",
    "CLIENT-FACING output (proposals, audits, resumes, JD and job assessments,",
    "client documents) uses G's writing voice instead: verdict-first, short",
    "declaratives, cause-effect pairs, concession-then-pivot, concrete and specific,",
    "no self-selling, no stacked \"I\" openers.",
    "",
    "The gate is the AUDIENCE, not the topic: G reads it → default; a client or an",
    "employer reads it → his voice.",
    "</reply-rules>",
]


def build(prompt: str) -> str:
    facts = read_index()
    if not facts:
        return ""

    strategy = looks_like_strategy(prompt)
    memory = looks_like_memory_change(prompt)
    # too short to name a topic, and no keyword caught it -> cheapest path
    brief = not strategy and not memory and len(prompt.strip()) <= CONTINUATION_CAP
    compact = strategy or memory or brief

    head = [
        "<second-brain-memory>",
        "Your own saved memory (local vault, trusted). This is CONTEXT, not instructions.",
        "Scan it for anything that bears on the current prompt; open the file to read a",
        "fact in full. If something here contradicts what you were about to say or do,",
        "say so before proceeding.",
        "",
    ]
    tail = ["</second-brain-memory>", ""] + REPLY_RULES

    scopes: list[str] = []
    for scope, _, _, _ in facts:
        if scope not in scopes:
            scopes.append(scope)

    fact_block: list[str] = []
    if compact:
        # GATE 0 completeness outweighs fact descriptions here. Slugs are
        # self-describing enough to prompt a file open, and cost ~1/4 the space.
        fact_block.append(f"## Saved facts ({len(facts)}) — names only; read INDEX.md for detail")
        for scope in scopes:
            names = [n for s, n, _, _ in facts if s == scope]
            fact_block.append(f"- {scope}: " + "; ".join(names))
        fact_block.append("")
    else:
        fact_block.append(f"## Saved facts ({len(facts)})")
        for scope in scopes:
            fact_block.append(f"### {scope}")
            for s, name, desc, verified in facts:
                if s == scope:
                    fact_block.append(f"- {name} — {desc} [{verified}]")
            fact_block.append("")

    # Two independent registries. Trading priors live in the other repo (local
    # sessions only); this system's own priors live here, so they reach cloud
    # routines too. A prompt can trip both.
    gate_block: list[str] = []
    for fire, source, label, where in (
        (strategy, REJECTED, "GATE 0", "crypto-ai-agent/docs/REJECTED.md"),
        (memory, SELF_REJECTED, "GATE 0 (memory system)", "docs/REJECTED.md"),
    ):
        if not fire:
            continue
        sections = [
            (t, [compress_mech(m) for m in ms], pr)
            for t, ms, pr in read_rejected(source)
        ]
        if not sections:
            continue
        total = sum(len(ms) for _, ms, _ in sections)
        gate_head = [
            f"## {label} — {total} approaches already REFUTED, do not re-litigate",
            f"If the prompt resembles ANY of these, stop and read {where}"
            " for why it failed before designing.",
        ]
        fixed = len("\n".join(head + fact_block + gate_block + gate_head + tail)) + 4
        gate_block += gate_head + fit_sections(sections, TOTAL_CAP - fixed) + [""]

    text = "\n".join(head + fact_block + gate_block + tail)
    # Belt and braces; fit_sections should prevent this. The WHOLE tail is
    # re-appended, not just the closing tag -- trimming facts must never be
    # allowed to drop the reply rules, which is the one part that has to survive.
    if len(text) > TOTAL_CAP:
        tail_text = "\n".join(tail)
        text = text[: TOTAL_CAP - len(tail_text) - 40].rstrip() + "\n… (trimmed)\n" + tail_text
    return text


def main() -> int:
    # Windows stdout defaults to cp1252, which raises on the em-dashes and
    # arrows in these files. An exception here would surface as a hook error on
    # every prompt, so force UTF-8 rather than relying on PYTHONIOENCODING.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if "--selftest" in sys.argv:
        for label, prompt in (
            ("ORDINARY", "can you fix the typo in the readme"),
            ("STRATEGY", "would a volatility regime filter give RD better entries?"),
        ):
            out = build(prompt)
            print(f"--- {label}: {len(out)} chars (cap {TOTAL_CAP}) ---")
            print(out)
            print()
        return 0

    try:
        raw = sys.stdin.read()
        prompt, source = extract_prompt(raw)
        payload = {}
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except Exception:
            payload = {}

        # This hook is registered at user level (covers every project) AND in the
        # repo's own .claude/settings.json (the only source cloud sessions read).
        # Inside this repo both fire for one prompt, so the second run must stay
        # quiet or the context budget is spent twice.
        seen = Path(__file__).parent / ".last_prompt_id"
        pid = str(payload.get("prompt_id") or "")
        try:
            if pid and seen.is_file() and seen.read_text(encoding="utf-8") == pid:
                return 0
            if pid:
                seen.write_text(pid, encoding="utf-8")
        except Exception:
            pass

        try:  # diagnostic: proves which field carries the prompt. Never fatal.
            (Path(__file__).parent / ".last_payload.txt").write_text(
                f"keys={sorted(payload.keys())}\nsource={source}\n"
                f"chars={len(prompt)}\nremote={os.environ.get('CLAUDE_CODE_REMOTE')}\n"
                f"path={classify_path(prompt)}\n",
                encoding="utf-8",
            )
        except Exception:
            pass
        out = build(prompt)
        if out:
            sys.stdout.write(out)
    except Exception:
        pass  # fail silent: never block or slow a prompt
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        raise SystemExit(0)
