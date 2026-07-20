"""Phase timeline — a descriptive read of what the session was *doing*, from
tool NAMES alone. No payload semantics, no LLM.

Its only job is to make the common silent run ("0 violations, pack silent")
informative instead of dead, by narrating the shape of the session. It is
rendered in the report and attached to each finding as its `trigger_class`.

Load-bearing rule: **display it, never gate a rule on it.** A wrong phase label
is a slightly-off story; a wrong phase label gating a check would silently hide
a real violation. Nothing in Phase 2 ever conditions on a phase.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import List, Optional, Tuple

# tool name -> coarse activity bucket
_EXPLORE = {"Read", "Grep", "Glob", "LS", "NotebookRead"}
_IMPLEMENT = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_GIT_RE = re.compile(r"(?:^|[\n;&|]|\bcd\b[^\n;&|]*&&)\s*git\s", re.IGNORECASE)

_NAME = {"explore": "exploring", "implement": "implementing",
         "debug": "debugging", "ship": "shipping"}
_BUCKETS = set(_NAME)

_WINDOW_R = 11     # sliding-window radius (window ~= 23 calls) — smooths interleaving
_DOMINANCE = 0.40  # a bucket must fill this share of the window to claim it
_MIN_RUN = 8       # shorter spans get merged into their neighbour (de-flicker)


@dataclass
class PhaseSpan:
    start_turn: int
    end_turn: int
    phase: str
    count: int  # tool calls in the span

    def to_dict(self) -> dict:
        return {"start_turn": self.start_turn, "end_turn": self.end_turn,
                "phase": self.phase, "count": self.count}


def category(name: str, command: Optional[str]) -> str:
    """Coarse bucket for a single tool call, from its name (+ git detection)."""
    if name in _EXPLORE:
        return "explore"
    if name in _IMPLEMENT:
        return "implement"
    if name == "Bash":
        return "ship" if command and _GIT_RE.search(command) else "debug"
    return "other"


def _label(window: List[str]) -> str:
    counts = Counter(c for c in window if c in _BUCKETS)
    if not counts:
        return "mixed"
    top, n = counts.most_common(1)[0]
    return _NAME[top] if n >= _DOMINANCE * len(window) else "mixed"


def build_phases(seq: List[Tuple[int, str]]) -> List[PhaseSpan]:
    """seq = [(turn, category), ...] in main-path order -> phase spans."""
    n = len(seq)
    if n == 0:
        return []
    cats = [c for _, c in seq]
    # per-call label from a centred sliding window
    labels: List[str] = []
    for i in range(n):
        lo, hi = max(0, i - _WINDOW_R), min(n, i + _WINDOW_R + 1)
        labels.append(_label(cats[lo:hi]))

    # collapse equal-label runs
    runs: List[List[int]] = []  # [start_idx, end_idx]
    for i, lab in enumerate(labels):
        if runs and labels[runs[-1][0]] == lab:
            runs[-1][1] = i
        else:
            runs.append([i, i])

    # merge short runs into the previous run (keeps the story readable)
    merged: List[List[int]] = []
    for r in runs:
        if merged and (r[1] - r[0] + 1) < _MIN_RUN:
            merged[-1][1] = r[1]
        else:
            merged.append(r)

    spans: List[PhaseSpan] = []
    for s, e in merged:
        lab = labels[s]
        if spans and spans[-1].phase == lab:  # re-collapse after merge
            spans[-1].end_turn = seq[e][0]
            spans[-1].count += (e - s + 1)
        else:
            spans.append(PhaseSpan(seq[s][0], seq[e][0], lab, e - s + 1))
    return spans


def phase_at(spans: List[PhaseSpan], turn: int) -> str:
    """The phase covering `turn` (nearest span if it falls between/outside)."""
    if not spans:
        return "mixed"
    for sp in spans:
        if sp.start_turn <= turn <= sp.end_turn:
            return sp.phase
    return min(spans, key=lambda sp: min(abs(turn - sp.start_turn), abs(turn - sp.end_turn))).phase
