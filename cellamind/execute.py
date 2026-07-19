"""Phase 2 — execute compiled checks against a transcript. No LLM calls.

Everything here is deterministic and citable: every violation names a turn, the
line's uuid, the file line number, and the literal evidence, so a human can open
the transcript and see it. That verifiability is the whole point.
"""

from __future__ import annotations

import os
import re
from collections import deque
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Tuple

from .checks import COMMAND, CONTENT, ORDERING, Check
from .code_strip import strip_code
from .transcript import Transcript, ToolCall


@dataclass
class Violation:
    check_id: str
    rule: str
    type: str
    turn: int
    line_id: Optional[str]  # the transcript line's uuid — survives renumbering
    line_no: int  # file line number — grep-verifiable
    evidence: str
    verdict: str = "violated"

    def to_dict(self) -> dict:
        return asdict(self)


def _trim(s: str, n: int = 100) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _safe_regex(pattern: str) -> Optional["re.Pattern"]:
    try:
        return re.compile(pattern)
    except re.error:
        return None


def _basename(path: Optional[str]) -> str:
    return os.path.basename(path) if path else ""


def _applies(path: Optional[str], globs: List[str]) -> bool:
    if not globs:
        return True
    if not path:
        return False
    import fnmatch

    base = os.path.basename(path)
    return any(fnmatch.fnmatch(base, g) or fnmatch.fnmatch(path, g) for g in globs)


def _added_text(tc: ToolCall, created_paths) -> List[Tuple[Optional[str], str]]:
    """Return [(file_path, added_text)] — the agent-authored text to scan.

    Edit: the new_string (added side only). MultiEdit: each edit's new_string.
    Write: the whole file, but ONLY if the transcript shows it created this
    session — otherwise pre-existing violations get blamed on the agent.
    """
    name, inp = tc.name, tc.input
    if name == "Edit":
        ns, fp = inp.get("new_string"), inp.get("file_path")
        if isinstance(ns, str) and fp:
            return [(fp, ns)]
    elif name == "MultiEdit":
        fp = inp.get("file_path")
        out = []
        for e in inp.get("edits") or []:
            if isinstance(e, dict) and isinstance(e.get("new_string"), str):
                out.append((fp, e["new_string"]))
        return out
    elif name == "Write":
        fp, content = inp.get("file_path"), inp.get("content")
        if isinstance(content, str) and fp and fp in created_paths:
            return [(fp, content)]
    return []


def _content_hits(original: str, stripped: str, check: Check, pat) -> List[str]:
    """Match on the comment/string-stripped text; cite the ORIGINAL line."""
    orig_lines = original.splitlines()
    strip_lines = stripped.splitlines()
    hits: List[str] = []
    for i, sline in enumerate(strip_lines):
        matched = any(f in sline for f in check.forbid)
        if not matched and pat is not None and pat.search(sline):
            matched = True
        if matched:
            orig = orig_lines[i] if i < len(orig_lines) else sline
            hits.append(orig.strip())
    return hits


def _command_forbid_hit(cmd: str, check: Check, pat) -> Optional[str]:
    for f in check.forbid:
        # A single bare word (e.g. "grep") is matched on word boundaries so it
        # doesn't fire inside "grepper" or a longer path. Multi-token strings
        # ("cargo test") are matched as-is.
        if " " not in f and re.fullmatch(r"[\w.-]+", f):
            if re.search(r"(?<![\w.-])%s(?![\w.-])" % re.escape(f), cmd):
                return f
        elif f in cmd:
            return f
    if pat is not None:
        m = pat.search(cmd)
        if m:
            return m.group(0)
    return None


def _matches_after(tc: ToolCall, spec: str) -> bool:
    """require_after spec: a tool name, or "Bash: <substr>"."""
    if ":" in spec:
        tool, _, sub = spec.partition(":")
        tool, sub = tool.strip(), sub.strip()
        if tc.name != tool:
            return False
        return sub in (tc.input.get("command") or "") if tool == "Bash" else True
    return tc.name == spec.strip()


def execute(checks: List[Check], transcript: Transcript) -> List[Violation]:
    command_checks = [c for c in checks if c.type == COMMAND]
    content_checks = [c for c in checks if c.type == CONTENT]
    ordering_checks = [c for c in checks if c.type == ORDERING]

    # Pre-compile regexes once.
    pat_of: Dict[str, Optional[re.Pattern]] = {}
    for c in checks:
        pat_of[c.id] = _safe_regex(c.forbid_pattern) if c.forbid_pattern else None

    violations: List[Violation] = []

    # command: require_present is a session-level check.
    present_met = {c.id: False for c in command_checks if c.require_present}

    # ordering: lookback history for require_before. within_turns<=0 means
    # unbounded ("have I ever seen this file"), so keep full history then.
    windows = [c.within_turns for c in ordering_checks if c.require_before]
    unbounded = any(w <= 0 for w in windows)
    max_window = 0 if unbounded else max(windows + [0])
    history: "deque[Tuple[int, int, str, Optional[str]]]" = deque()  # (turn, seq, name, path)

    # ordering: require_after (scope session_end) — track the last open trigger.
    after_state: Dict[str, dict] = {}

    last_turn = 0
    last_line_id: Optional[str] = None
    last_line_no = 0

    for tc in transcript.tool_calls():
        last_turn, last_line_id, last_line_no = tc.turn, tc.line_id, tc.line_no
        path = tc.input.get("file_path") if isinstance(tc.input.get("file_path"), str) else None

        # ---------- command ----------
        if tc.name == "Bash":
            cmd = tc.input.get("command") or ""
            # Match against a string-stripped copy so a forbidden token that
            # only appears inside a quoted search pattern / heredoc doesn't fire;
            # cite the original command as evidence.
            cmd_stripped = strip_code(cmd, "sh")
            # A command that changes directory (or targets another repo) makes
            # the recorded branch unreliable for its git ops — do not guess.
            cd_away = bool(re.search(r"(?:^|[\n;&|])\s*(?:cd|pushd)\s+\S", cmd_stripped)) \
                or "git -C" in cmd_stripped or "git --git-dir" in cmd_stripped
            for c in command_checks:
                if c.when_branch:
                    if tc.git_branch != c.when_branch:
                        continue  # branch doesn't match (or unknown) — do not guess
                    if cd_away:
                        continue  # cwd changed — recorded branch no longer applies
                if c.forbid or c.forbid_pattern:
                    hit = _command_forbid_hit(cmd_stripped, c, pat_of[c.id])
                    if hit:
                        violations.append(Violation(
                            c.id, c.rule, c.type, tc.turn, tc.line_id, tc.line_no,
                            "Bash → %s" % _trim(cmd),
                        ))
                if c.require_present and c.require_present in cmd:
                    present_met[c.id] = True

        # ---------- content ----------
        added = _added_text(tc, transcript.created_paths)
        for fpath, text in added:
            ext = os.path.splitext(fpath or "")[1].lstrip(".")
            for c in content_checks:
                if not (c.forbid or c.forbid_pattern):
                    continue
                if not _applies(fpath, c.applies_to):
                    continue
                stripped = strip_code(text, ext)
                hits = _content_hits(text, stripped, c, pat_of[c.id])
                if hits:
                    violations.append(Violation(
                        c.id, c.rule, c.type, tc.turn, tc.line_id, tc.line_no,
                        "%s → %s:  %s" % (tc.name, _basename(fpath), _trim(hits[0], 80)),
                    ))

        # ---------- ordering ----------
        if ordering_checks:
            if max_window:  # finite window — trim; unbounded keeps full history
                while history and history[0][0] < tc.turn - max_window:
                    history.popleft()

            for c in ordering_checks:
                # require_before: this trigger must have a prior `require_before`
                if c.require_before and tc.name == c.trigger:
                    if not _has_before(history, c, tc.turn, path):
                        tail = " of the same file" if c.same_path else ""
                        violations.append(Violation(
                            c.id, c.rule, c.type, tc.turn, tc.line_id, tc.line_no,
                            "%s → %s with no preceding %s%s within %d turns"
                            % (tc.name, _basename(path) or "(no path)", c.require_before, tail, c.within_turns),
                        ))
                # require_after: remember the latest trigger; satisfy on later match
                if c.require_after:
                    if tc.name == c.trigger:
                        after_state[c.id] = {
                            "turn": tc.turn, "seq": tc.seq, "line_id": tc.line_id,
                            "line_no": tc.line_no, "path": path, "satisfied": False,
                        }
                    st = after_state.get(c.id)
                    if st and not st["satisfied"] and tc.seq > st["seq"] and _matches_after(tc, c.require_after):
                        st["satisfied"] = True

            history.append((tc.turn, tc.seq, tc.name, path))

    # ---------- session-end checks ----------
    for c in command_checks:
        if c.require_present and not present_met[c.id]:
            violations.append(Violation(
                c.id, c.rule, c.type, last_turn, last_line_id, last_line_no,
                "no Bash command matched `%s` anywhere in the session" % c.require_present,
                verdict="violated",
            ))

    for c in ordering_checks:
        if c.require_after and c.scope == "session_end":
            st = after_state.get(c.id)
            if st and not st["satisfied"]:
                violations.append(Violation(
                    c.id, c.rule, c.type, st["turn"], st["line_id"], st["line_no"],
                    "last %s (turn %d) was not followed by %s" % (c.trigger, st["turn"], c.require_after),
                ))

    violations.sort(key=lambda v: v.turn)
    return violations


def _has_before(history, check: Check, trigger_turn: int, trigger_path: Optional[str]) -> bool:
    if check.same_path and not trigger_path:
        return True  # can't evaluate same_path without a path — don't false-alarm
    accepted = {t.strip() for t in (check.require_before or "").split(",") if t.strip()}
    unbounded = check.within_turns <= 0
    lo = trigger_turn - check.within_turns
    for turn, _seq, name, path in history:
        if name not in accepted:
            continue
        if not unbounded and turn < lo:
            continue
        if check.same_path and path != trigger_path:
            continue
        return True
    return False
