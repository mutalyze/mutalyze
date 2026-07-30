"""Mine rules you stated in chat but never wrote down.

The rules that govern an agent are the ones in the rules file. The rules you
*actually* care about include every correction you typed mid-session — "always
use rg, not grep", "never commit straight to main" — which live in a transcript
nobody reads again and are gone the moment the conversation ends. Next session
starts without them.

This module reads past transcripts, pulls the normative sentences out of *your*
messages, drops the ones already covered by the rules file or the store, and
returns what is left as **proposals**. It writes nothing: the caller shows them
and the user approves. A proposal is always cited — session file and line — so
you can go read what you actually said.

Constraints, same as everywhere else in mutalyze:

- **Deterministic and offline.** No LLM. Candidate sentences are classified by
  Phase 1's own :func:`classify`, so a proposal marked checkable really would
  compile into the same check the compiler would produce.
- **Precision over recall.** A junk proposal costs the user trust in the review
  list, so the filters are deliberately strict and a sentence that cannot be
  read as a rule is dropped rather than guessed at.
- **Only your words.** Assistant turns, tool results, and harness-injected text
  are excluded; a rule the *agent* proposed is not a rule *you* set.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Set, Tuple

from .compile_rules import _COMMAND_VERBS, classify, extract_candidates, find_rules_files
from .store import norm_key

# ---------------------------------------------------------------------------
# Reading the human half of a transcript
# ---------------------------------------------------------------------------

# Harness-injected wrappers that appear inside `user` turns but are not typed by
# a human. Anything carrying these is dropped whole — mining them would turn
# tool output and system reminders into "rules the user set".
_INJECTED_MARKERS = (
    "<system-reminder>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-caveat>",
    "Caveat: The messages below were generated",
    "[Request interrupted",
    "This is Claude Code's session transcript",
)

_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL | re.IGNORECASE)


@dataclass
class UserSaid:
    """One block of text a human actually typed."""

    text: str
    line_no: int
    session: str


def user_messages(path: str) -> Iterator[UserSaid]:
    """Yield the human-authored text blocks from a transcript, in file order.

    Deliberately independent of main-path numbering: a rule you stated on a
    branch you later rewound is still a rule you stated.
    """
    session = os.path.basename(path)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line_no, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(obj, dict) or obj.get("type") != "user":
                continue
            if obj.get("isMeta") or obj.get("isSidechain"):
                continue
            message = obj.get("message")
            if not isinstance(message, dict):
                continue

            content = message.get("content")
            chunks: List[str] = []
            if isinstance(content, str):
                chunks.append(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    # tool_result blocks are machine output, never human text
                    if block.get("type") != "text":
                        continue
                    value = block.get("text")
                    if isinstance(value, str):
                        chunks.append(value)

            for chunk in chunks:
                cleaned = _REMINDER_RE.sub(" ", chunk)
                if any(marker in cleaned for marker in _INJECTED_MARKERS):
                    continue
                cleaned = cleaned.strip()
                if cleaned:
                    yield UserSaid(text=cleaned, line_no=line_no, session=session)


# ---------------------------------------------------------------------------
# Pulling rule-shaped sentences out of prose
# ---------------------------------------------------------------------------

# An instruction opens with one of these, or contains one of the paired forms.
_IMPERATIVE_RE = re.compile(
    r"^\s*(?:please\s+)?(?:always|never|dont|don't|do not|avoid|make sure|remember to"
    r"|from now on|stop|only ever|only use|use|prefer|run|switch to|no more)\b",
    re.IGNORECASE,
)
_PAIRED_RE = re.compile(
    r"\b(?:should|must|need to|have to)\s+(?:always|never|not)\b"
    r"|\b(?:always|never)\s+(?:use|run|commit|push|edit|write|call|touch|delete)\b"
    r"|\buse\s+\S+\s+(?:not|instead of|rather than)\s+\S+",
    re.IGNORECASE,
)
# Sentence-ish split: keeps `foo.py` and version numbers intact by requiring the
# terminator to be followed by whitespace/end.
_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|\n+")

# Never mine these: they are about the conversation, not about the code.
_META_RE = re.compile(
    r"\b(?:you (?:said|wrote|told)|earlier|last time|as i said|i meant|instead of that"
    r"|thanks|thank you|sorry|nice|cool|great|perfect|looks? (?:good|terrible|ugly))\b",
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(r"\?\s*$")


# Conversational lead-ins. "also never commit to main" and "never commit to main"
# are the same rule, and only one of them should ever reach the store — otherwise
# the same instruction gets proposed again after it has already been written down.
# "no" is deliberately absent: it is a normative token ("no `print()`").
_FILLER_RE = re.compile(
    r"^(?:also|and|so|ok|okay|btw|oh|hey|well|actually|plus|additionally|then|but)\b[,:;\s]*",
    re.IGNORECASE,
)


def _strip_leading_marks(text: str) -> str:
    """Drop bullet markers, wrapping quotes, and conversational lead-ins."""
    out = text.strip()
    while True:
        stripped = re.sub(r'^\s*(?:[-*+]|\d+[.)])\s+', "", out)
        stripped = _FILLER_RE.sub("", stripped)
        stripped = stripped.strip().strip('"').strip("'").strip()
        if stripped == out:
            return out
        out = stripped


def _sentences(text: str) -> List[str]:
    out: List[str] = []
    for raw in _SPLIT_RE.split(text):
        s = " ".join(raw.split())
        if s:
            out.append(s)
    return out


# Command names that are also ordinary English words. "don't try to make it work"
# is not a rule about the `make` build tool, and wrapping it as one produced a
# confident, checkable, completely wrong proposal — so these are never wrapped
# on the strength of the word alone.
_AMBIGUOUS_COMMANDS = {
    "make", "find", "sort", "touch", "head", "tail", "cat", "echo", "go", "kill",
    "ps", "tee", "node", "black", "bun", "sh", "tar", "test", "ag", "cd", "ln",
}

# A bare command word only becomes a rule token when something in the sentence
# marks it as a *tool being chosen or rejected*.
_CUE_WORDS = {
    "use", "uses", "using", "used", "run", "runs", "running", "prefer", "prefers",
    "invoke", "call", "via", "with", "not", "instead", "than", "of", "switch",
}


def _backtick_commands(sentence: str) -> str:
    """Wrap bare command words in backticks so the sentence can compile.

    Chat rarely uses backticks ("always use rg not grep") but the classifier
    keys off them. A word is only wrapped when it is a known command AND the
    preceding word marks it as a tool choice — never on the word alone, and
    never when the user already formatted the sentence themselves.
    """
    if "`" in sentence:
        return sentence

    tokens = re.split(r"(\W+)", sentence)  # keeps separators, so joining restores the text
    prev_word = ""
    for i, tok in enumerate(tokens):
        if not tok or not re.match(r"^[A-Za-z][\w.\-]*$", tok):
            continue
        low = tok.lower()
        if low in _COMMAND_VERBS and low not in _AMBIGUOUS_COMMANDS and prev_word in _CUE_WORDS:
            tokens[i] = "`%s`" % tok
        prev_word = low
    return "".join(tokens)


def rule_candidates(text: str) -> List[str]:
    """Rule-shaped sentences in one block of user prose, normalized for storage.

    Bulleted rules pasted into chat are handled by Phase 1's own bullet
    extractor; loose prose goes through the imperative filters.
    """
    found: List[str] = []

    # a pasted list of rules is exactly what the compiler already understands
    for bullet in extract_candidates(text):
        cleaned = _strip_leading_marks(bullet)
        if _IMPERATIVE_RE.match(cleaned) or _PAIRED_RE.search(cleaned):
            found.append(cleaned)

    for raw_sentence in _sentences(text):
        sentence = _strip_leading_marks(raw_sentence)
        if not (12 <= len(sentence) <= 200):
            continue
        if _QUESTION_RE.search(sentence) or _META_RE.search(sentence):
            continue
        if not (_IMPERATIVE_RE.match(sentence) or _PAIRED_RE.search(sentence)):
            continue
        candidate = _backtick_commands(sentence).rstrip(",;")
        # Require something concrete to act on; "always be careful" is not a rule.
        # A backticked token qualifies, and so does anything the classifier can
        # compile on its own — "never commit directly to main" needs no backticks
        # and is exactly the rule people most want carried forward.
        if "`" not in candidate and classify(candidate)[0] is None:
            continue
        found.append(candidate)

    # de-dup within this block, preserving order
    seen: Set[str] = set()
    out: List[str] = []
    for c in found:
        key = norm_key(c)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------

@dataclass
class Proposal:
    text: str  # normalized, storable rule text
    said: str  # what the user actually typed (verbatim sentence)
    count: int = 1  # times restated across the corpus — repetition is signal
    sessions: List[str] = field(default_factory=list)
    citations: List[Tuple[str, int]] = field(default_factory=list)  # (session, line)
    checkable: bool = False
    check_type: str = ""
    reason: str = ""  # why it is not checkable, when it isn't

    def cite(self) -> str:
        if not self.citations:
            return ""
        session, line = self.citations[0]
        extra = " +%d more" % (len(self.citations) - 1) if len(self.citations) > 1 else ""
        return "%s:%d%s" % (session, line, extra)


@dataclass
class MineResult:
    proposals: List[Proposal] = field(default_factory=list)
    sessions_scanned: int = 0
    already_known: int = 0  # candidates dropped as already covered


def known_rule_keys(repo_root: str, store_rules: Optional[List[str]] = None) -> Set[str]:
    """Normalized keys for rules already written down — the rules file plus,
    optionally, the store. A proposal is only interesting if it is *unsaid*."""
    keys: Set[str] = set()
    for path in find_rules_files(repo_root):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for rule in extract_candidates(text):
            keys.add(norm_key(rule))
    for text in store_rules or []:
        keys.add(norm_key(text))
    return keys


def mine_sessions(paths: List[str], known: Optional[Set[str]] = None) -> MineResult:
    """Scan transcripts and return deduped, cited, ranked proposals."""
    known = known or set()
    result = MineResult()
    by_key: Dict[str, Proposal] = {}

    for path in paths:
        if not os.path.exists(path):
            continue
        result.sessions_scanned += 1
        for said in user_messages(path):
            for candidate in rule_candidates(said.text):
                key = norm_key(candidate)
                if key in known:
                    result.already_known += 1
                    continue
                existing = by_key.get(key)
                if existing is not None:
                    existing.count += 1
                    existing.citations.append((said.session, said.line_no))
                    if said.session not in existing.sessions:
                        existing.sessions.append(said.session)
                    continue
                check, reason = classify(candidate)
                by_key[key] = Proposal(
                    text=candidate,
                    said=candidate,
                    count=1,
                    sessions=[said.session],
                    citations=[(said.session, said.line_no)],
                    checkable=check is not None,
                    check_type=check.type if check is not None else "",
                    reason="" if check is not None else (reason or "not mechanically verifiable"),
                )

    # most-repeated first, then checkable ahead of unsupported, then stable
    result.proposals = sorted(
        by_key.values(),
        key=lambda p: (-p.count, not p.checkable, p.text.lower()),
    )
    return result
