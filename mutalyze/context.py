"""Re-assert the rules that matter right now — the compaction survivor.

A long session gets compacted: the harness squeezes the conversation to fit the
window, and whatever the agent was told 200 turns ago can go with it. The rules
file is the first casualty precisely because it was read once, at the start.

This module answers one question — *which of my rules should be put back in
front of the agent at this moment?* — and answers it deterministically:

- **Relevance is computed from scope, not guessed.** A compiled check already
  knows what it governs (``applies_to`` globs, forbidden command tokens, an
  ordering trigger). Matching that against what the agent just did, or against
  the prompt the user opened with, is arithmetic, not judgement. No LLM.
- **Trimming is the only thing relevance decides.** When every rule fits in the
  budget they are all re-asserted; ranking only chooses what to keep when they
  do not. Silently dropping a rule the agent still needs is the failure mode
  that matters, so the cut is reported, never hidden.
- **Read-only.** This inspects a transcript and prints text. It never edits a
  session, and nothing leaves the machine.

The output is plain Markdown by default so it is useful to any agent; a hook
envelope for Claude Code is available with ``--format claude-hook``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .checks import CONTENT, ORDERING, Check
from .compile_rules import classify, compile_rules

# Tool inputs whose value is a path worth taking an extension from.
_PATH_KEYS = ("file_path", "path", "notebook_path")
_COMPACT_MARKERS = ('"compact_boundary"', '"isCompactSummary":true', '"isCompactSummary": true')


# ---------------------------------------------------------------------------
# What the agent has been doing lately
# ---------------------------------------------------------------------------

@dataclass
class Activity:
    """Signals drawn from the tail of a session."""

    extensions: Set[str] = field(default_factory=set)   # {"ts", "py"}
    basenames: Set[str] = field(default_factory=set)
    commands: str = ""                                   # recent command text, lowercased
    tools: Set[str] = field(default_factory=set)         # {"Edit", "Bash"}
    turns_seen: int = 0
    compactions: int = 0

    def has_signal(self) -> bool:
        return bool(self.extensions or self.commands or self.tools)


def count_compactions(path: str) -> int:
    """How many times this session has been compacted.

    Counted by scanning for the boundary marker rather than parsing the whole
    file: the caller is usually a hook on the hot path, and this is a hint for
    the reminder header, not an audit result.
    """
    total = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                if any(marker in raw for marker in _COMPACT_MARKERS):
                    total += 1
    except OSError:
        return 0
    return total


def recent_activity(transcript_path: str, window: int = 12) -> Activity:
    """Summarize the last `window` turns of tool use."""
    from .transcript import Transcript

    activity = Activity()
    if not os.path.exists(transcript_path):
        return activity
    activity.compactions = count_compactions(transcript_path)

    transcript = Transcript(transcript_path)
    calls = list(transcript.tool_calls())
    if not calls:
        return activity
    last_turn = max(c.turn for c in calls)
    cutoff = last_turn - window
    command_bits: List[str] = []

    for call in calls:
        if call.turn < cutoff:
            continue
        activity.turns_seen += 1
        activity.tools.add(call.name)
        for key in _PATH_KEYS:
            value = call.input.get(key)
            if isinstance(value, str) and value:
                base = os.path.basename(value)
                activity.basenames.add(base)
                ext = os.path.splitext(base)[1].lstrip(".").lower()
                if ext:
                    activity.extensions.add(ext)
        command = call.input.get("command")
        if isinstance(command, str) and command:
            command_bits.append(command.lower())
    activity.commands = "\n".join(command_bits)
    return activity


# ---------------------------------------------------------------------------
# The rules available to re-assert
# ---------------------------------------------------------------------------

@dataclass
class RuleEntry:
    text: str
    origin: str                    # "CLAUDE.md" / "store:base"
    check: Optional[Check] = None  # scope metadata, when the rule compiles


def gather_rules(
    repo_root: str,
    include_store: bool = True,
    bundles: Optional[List[str]] = None,
) -> List[RuleEntry]:
    """Every rule that could be re-asserted: the repo's rules file, plus the
    store's bundles. Duplicates across sources collapse to the first seen."""
    from .store import load_store, norm_key

    entries: List[RuleEntry] = []
    seen: Set[str] = set()

    doc = compile_rules(repo_root)
    if doc is not None:
        for check in doc.checks:
            key = norm_key(check.rule)
            if key in seen:
                continue
            seen.add(key)
            entries.append(RuleEntry(text=check.rule, origin=doc.source, check=check))
        for item in doc.unsupported:
            text = item.get("rule", "")
            key = norm_key(text)
            if not text or key in seen:
                continue
            seen.add(key)
            entries.append(RuleEntry(text=text, origin=doc.source, check=None))

    if include_store:
        store = load_store()
        for rule in store.active():
            if bundles and rule.bundle not in bundles:
                continue
            key = norm_key(rule.text)
            if key in seen:
                continue
            seen.add(key)
            check, _reason = classify(rule.text)
            entries.append(
                RuleEntry(text=rule.text, origin="store:%s" % rule.bundle, check=check)
            )
    return entries


# ---------------------------------------------------------------------------
# Relevance
# ---------------------------------------------------------------------------

@dataclass
class ScoredRule:
    entry: RuleEntry
    score: int = 0
    reasons: List[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return self.entry.text


def _globs_match_ext(globs: List[str], extensions: Set[str]) -> Optional[str]:
    for glob in globs:
        stem = glob.lstrip("*.").lower()
        if stem and stem in extensions:
            return glob
    return None


def _forbidden_literals(check: Check) -> List[str]:
    out = [f.lower() for f in check.forbid if f]
    if check.require_instead:
        out.append(check.require_instead.lower())
    return out


def score_rules(
    entries: List[RuleEntry],
    activity: Optional[Activity] = None,
    prompt: Optional[str] = None,
) -> List[ScoredRule]:
    """Rank rules by how much they bear on what is happening right now."""
    prompt_low = (prompt or "").lower()
    scored: List[ScoredRule] = []

    for entry in entries:
        s = ScoredRule(entry=entry)
        check = entry.check

        if prompt_low:
            # the user's own words are the strongest signal available
            if check is not None:
                for literal in _forbidden_literals(check):
                    if literal and literal in prompt_low:
                        s.score += 3
                        s.reasons.append("prompt mentions '%s'" % literal)
                        break
                if check.type == CONTENT and check.applies_to:
                    hit = _globs_match_ext(check.applies_to, _exts_in(prompt_low))
                    if hit:
                        s.score += 3
                        s.reasons.append("prompt mentions %s files" % hit)
            for word in _keywords(entry.text):
                if word in prompt_low:
                    s.score += 1
                    s.reasons.append("prompt mentions '%s'" % word)
                    break

        if activity is not None and activity.has_signal() and check is not None:
            if check.type == CONTENT and check.applies_to:
                hit = _globs_match_ext(check.applies_to, activity.extensions)
                if hit:
                    s.score += 2
                    s.reasons.append("editing %s" % hit)
            if activity.commands:
                for literal in _forbidden_literals(check):
                    if literal and literal in activity.commands:
                        s.score += 2
                        s.reasons.append("recent command uses '%s'" % literal)
                        break
                if check.forbid_pattern:
                    try:
                        if re.search(check.forbid_pattern, activity.commands):
                            s.score += 2
                            s.reasons.append("recent command matches this rule")
                    except re.error:
                        pass
            if check.type == ORDERING and check.trigger and check.trigger in activity.tools:
                s.score += 2
                s.reasons.append("just used %s" % check.trigger)

        scored.append(s)

    scored.sort(key=lambda x: (-x.score, x.text.lower()))
    return scored


def _exts_in(text: str) -> Set[str]:
    return {m.group(1).lower() for m in re.finditer(r"\.([A-Za-z0-9]{1,5})\b", text)}


_STOPWORDS = {
    "the", "a", "an", "and", "or", "not", "for", "with", "into", "from", "this",
    "that", "your", "you", "use", "using", "run", "never", "always", "must",
    "every", "any", "all", "only", "before", "after", "then", "when", "file",
    "files", "code", "make", "sure", "keep", "them", "they", "it", "is", "are",
    "in", "on", "of", "to", "do", "no", "be", "by", "at", "as", "if", "so",
}


def _keywords(text: str) -> List[str]:
    """Content words from a rule, longest first — a cheap topical fallback for
    rules that carry no compiled scope at all."""
    words = {w.lower() for w in re.findall(r"[A-Za-z][\w.\-/]{3,}", text)}
    return sorted((w for w in words if w not in _STOPWORDS), key=len, reverse=True)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

@dataclass
class Reminder:
    rules: List[ScoredRule] = field(default_factory=list)
    dropped: int = 0
    compactions: int = 0
    targeted: bool = False  # True when relevance actually reordered anything

    def as_markdown(self, title: str = "Project rules still in force") -> str:
        lines = ["# %s" % title, ""]
        note = "<!-- mutalyze: re-asserted from your rules file and rule store"
        if self.compactions:
            note += "; this session has compacted %d time(s)" % self.compactions
        note += " -->"
        lines.append(note)
        lines.append("")
        for s in self.rules:
            lines.append("- %s" % s.text)
        if self.dropped:
            lines.append("")
            lines.append("<!-- %d further rule(s) not shown (budget) — "
                         "run `mutalyze context --max 0` for all -->" % self.dropped)
        return "\n".join(lines).rstrip() + "\n"

    def as_json(self) -> str:
        return json.dumps(
            {
                "rules": [
                    {"text": s.text, "origin": s.entry.origin, "score": s.score,
                     "why": s.reasons}
                    for s in self.rules
                ],
                "dropped": self.dropped,
                "compactions": self.compactions,
            },
            indent=2,
        )

    def as_claude_hook(self, event: str = "UserPromptSubmit") -> str:
        """Claude Code hook envelope. Plain stdout also works for the context-
        injecting events; this form is explicit about what it is."""
        return json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": self.as_markdown(),
                }
            }
        )


def build_reminder(
    repo_root: str,
    session: Optional[str] = None,
    prompt: Optional[str] = None,
    max_rules: int = 12,
    include_store: bool = True,
    bundles: Optional[List[str]] = None,
) -> Reminder:
    entries = gather_rules(repo_root, include_store=include_store, bundles=bundles)
    activity = recent_activity(session) if session else None
    scored = score_rules(entries, activity=activity, prompt=prompt)

    reminder = Reminder(
        compactions=activity.compactions if activity else 0,
        targeted=any(s.score > 0 for s in scored),
    )
    if max_rules and max_rules > 0 and len(scored) > max_rules:
        reminder.rules = scored[:max_rules]
        reminder.dropped = len(scored) - max_rules
    else:
        reminder.rules = scored
    return reminder
