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
from .code_strip import is_prose_path, strip_code
from .compile_rules import RULE_FILENAMES
from .phases import PhaseSpan, build_phases, category, phase_at
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
    phase: str = "mixed"  # descriptive session phase at this turn (trigger_class)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Unresolved:
    """A finding we cannot stand behind: the forbidden token is present, but the
    command's location was built from a shell variable so we can't tell whether
    it ran inside the governed repo. Neither violated nor held — reported apart,
    and the user can still open the turn and look."""

    check_id: str
    rule: str
    turn: int
    line_id: Optional[str]
    line_no: int
    evidence: str
    reason: str
    phase: str = "mixed"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExecResult:
    violations: List["Violation"]
    unresolved: List["Unresolved"]
    phases: List["PhaseSpan"] = None  # descriptive session shape (display only)


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


def _is_rules_file(path: Optional[str]) -> bool:
    """A rules file states the rules; quoting a forbidden token there is the rule
    being written down, not the rule being broken."""
    return _basename(path).lower() in {n.lower() for n in RULE_FILENAMES}


def _under(path: Optional[str], root: Optional[str]) -> bool:
    """True if `path` is inside `root` (or we have no root to scope by).

    A CLAUDE.md governs its own repo. Activity on files/commands outside that
    repo — another checkout, a scratch dir — isn't bound by these rules, so we
    scope to it rather than blame the agent for out-of-repo work.
    """
    if not root or not path:
        return True  # nothing to scope by, or no path — don't suppress
    root = os.path.normpath(root)
    p = os.path.normpath(path)
    return p == root or p.startswith(root + os.sep)


def _command_location(cmd_stripped: str, tc_cwd: Optional[str], root: Optional[str]) -> str:
    """Where did this command actually run, relative to the governed repo?

    Returns "in_repo", "out_repo", or "unresolved". Location is the invocation
    directory (recorded cwd, adjusted by a resolvable `cd`), NOT what the command
    happens to touch — invoking `grep` in the repo breaks a "use rg" rule even if
    it greps a /tmp file. A `cd` into a shell variable is unresolved: we don't
    guess, and we don't silently drop it either.
    """
    if not root:
        return "in_repo"  # nothing to scope by — evaluate
    cds = re.findall(r"(?:^|[\n;&|])\s*(?:cd|pushd)\s+([^\s;&|]+)", cmd_stripped)
    resolvable = []
    for t in cds:
        t = t.strip().strip("\"'")
        if t == "-" or "$" in t or "`" in t:
            return "unresolved"
        resolvable.append(t)
    if any(_cd_resolves_outside(t, root) for t in resolvable):
        return "out_repo"
    if tc_cwd is None:
        return "unresolved"
    return "in_repo" if _under(tc_cwd, root) else "out_repo"


def _cd_resolves_outside(target: str, root: Optional[str]) -> bool:
    """True if a `cd <target>` provably lands outside the governed repo.

    Absolute or ~-anchored targets are resolved and checked; relative targets
    (`cd public`) are assumed to stay inside the repo, and unresolvable ones
    (`cd "$DIR"`) are left to evaluate rather than guessed away.
    """
    if not root:
        return False
    t = target.strip().strip("\"'")
    if t.startswith("~"):
        t = os.path.expanduser(t)
    if not os.path.isabs(t):
        return False
    return not _under(t, root)


def _applies(path: Optional[str], globs: List[str], root: Optional[str] = None) -> bool:
    if not globs:
        return True
    if not path:
        return False
    import fnmatch

    base = os.path.basename(path)
    # A directory-scope glob ("mutalyze/*") is matched against the repo-relative
    # path, so it scopes to that subtree and does NOT collide with a repo that
    # happens to share the directory's name (every file under ~/mutalyze has
    # "mutalyze/" in its absolute path — relative matching avoids that).
    rel = None
    if root:
        try:
            r = os.path.relpath(os.path.normpath(path), os.path.normpath(root))
        except ValueError:
            r = None
        if r and not r.startswith(".."):  # only when the file is under the repo
            rel = r
    for g in globs:
        if fnmatch.fnmatch(base, g) or fnmatch.fnmatch(path, g):
            return True
        if rel is not None and fnmatch.fnmatch(rel, g):
            return True
    return False


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


def execute(checks: List[Check], transcript: Transcript,
            repo_root: Optional[str] = None) -> ExecResult:
    command_checks = [c for c in checks if c.type == COMMAND]
    content_checks = [c for c in checks if c.type == CONTENT]
    ordering_checks = [c for c in checks if c.type == ORDERING]

    # Scope every check to the repo the rules govern. Fall back to the session's
    # recorded working directory when no explicit root is given.
    root = repo_root or transcript.session_cwd

    # Pre-compile regexes once.
    pat_of: Dict[str, Optional[re.Pattern]] = {}
    for c in checks:
        pat_of[c.id] = _safe_regex(c.forbid_pattern) if c.forbid_pattern else None

    violations: List[Violation] = []
    unresolved: List[Unresolved] = []

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

    # ordering: raw Bash commands seen so far, to spot files the agent authored
    # via a heredoc/redirect (`cat > x.py`, a python heredoc that opens 'x.py').
    # Such a file has no Read/Write/Edit tool call to credit, but writing it IS
    # seeing it — so a later Edit isn't "edited blind". We can't cleanly
    # adjudicate that, so it goes to `unresolved`, not `violated`.
    prior_bash: List[str] = []

    last_turn = 0
    last_line_id: Optional[str] = None
    last_line_no = 0
    phase_seq: List[Tuple[int, str]] = []  # (turn, activity category) for the timeline

    for tc in transcript.tool_calls():
        last_turn, last_line_id, last_line_no = tc.turn, tc.line_id, tc.line_no
        path = tc.input.get("file_path") if isinstance(tc.input.get("file_path"), str) else None
        phase_seq.append((tc.turn, category(tc.name, tc.input.get("command") if tc.name == "Bash" else None)))

        # ---------- command ----------
        if tc.name == "Bash":
            cmd = tc.input.get("command") or ""
            prior_bash.append(cmd)  # raw: a heredoc-authored path lives in the body
            cmd_stripped = strip_code(cmd, "sh")
            # Any cd makes the recorded branch unreliable for git ops.
            cd_away = bool(re.search(r"(?:^|[\n;&|])\s*(?:cd|pushd)\s+\S", cmd_stripped)) \
                or "git -C" in cmd_stripped or "git --git-dir" in cmd_stripped
            # location detection reads the RAW command: string-stripping would
            # blank a "$VAR" inside quotes and hide that it's unresolvable.
            location = _command_location(cmd, tc.cwd, root)  # in_repo|out_repo|unresolved
            for c in command_checks:
                if c.when_branch:
                    if tc.git_branch != c.when_branch:
                        continue  # branch doesn't match (or unknown) — do not guess
                    if cd_away:
                        continue  # cwd changed — recorded branch no longer applies
                if c.forbid or c.forbid_pattern:
                    hit = _command_forbid_hit(cmd_stripped, c, pat_of[c.id])
                    if hit:
                        # scope=session: applies everywhere. scope=repo: only if
                        # invoked in-repo; unresolved location -> the third bucket.
                        if c.scope == "session" or location == "in_repo":
                            violations.append(Violation(
                                c.id, c.rule, c.type, tc.turn, tc.line_id, tc.line_no,
                                "Bash → %s" % _trim(cmd),
                            ))
                        elif location == "unresolved":
                            unresolved.append(Unresolved(
                                c.id, c.rule, tc.turn, tc.line_id, tc.line_no,
                                "Bash → %s" % _trim(cmd),
                                "command location built from a shell variable — may or may not be in-repo",
                            ))
                        # location == "out_repo": rule is about this repo, work
                        # happened elsewhere -> not applicable.
                if c.require_present and c.require_present in cmd:
                    present_met[c.id] = True

        # ---------- content ----------
        added = _added_text(tc, transcript.created_paths)
        for fpath, text in added:
            in_repo = _under(fpath, root)
            ext = os.path.splitext(fpath or "")[1].lstrip(".")
            for c in content_checks:
                if not (c.forbid or c.forbid_pattern):
                    continue
                # repo-scoped checks only count in-repo; session-scoped (e.g. a
                # secret-to-disk check) fire wherever the file was written.
                if c.scope != "session" and not in_repo:
                    continue
                if not _applies(fpath, c.applies_to, root):
                    continue
                if _is_rules_file(fpath):
                    # A rules file naming its own forbidden token IS the rule,
                    # never a breach of it. Unconditional: it holds even when the
                    # rule explicitly scopes to markdown.
                    continue
                if not c.applies_to and is_prose_path(fpath or ""):
                    # A code-token rule that named no language says nothing about
                    # prose. Without this, documenting a rule violates it.
                    continue
                if fpath and any(x in fpath for x in c.exclude_paths):
                    continue  # example / test / fixture / doc path — benign by destination
                stripped = strip_code(text, ext)
                hits = _content_hits(text, stripped, c, pat_of[c.id])
                if hits:
                    violations.append(Violation(
                        c.id, c.rule, c.type, tc.turn, tc.line_id, tc.line_no,
                        "%s → %s:  %s" % (tc.name, _basename(fpath), _trim(hits[0], 80)),
                    ))

        # ---------- ordering ----------
        # Only track/trigger ordering for files inside the governed repo, so a
        # path-bearing tool call (Read/Edit/Write) on an out-of-repo file
        # neither fires nor counts as a prior "sight" of a file.
        if ordering_checks and (path is None or _under(path, root)):
            if max_window:  # finite window — trim; unbounded keeps full history
                while history and history[0][0] < tc.turn - max_window:
                    history.popleft()

            for c in ordering_checks:
                # require_before: this trigger must have a prior `require_before`
                if c.require_before and tc.name == c.trigger:
                    if not _has_before(history, c, tc.turn, path):
                        base = _basename(path)
                        if base and any(base in bc for bc in prior_bash):
                            # authored/touched by an earlier shell command — can't
                            # cleanly call this "edited without reading".
                            unresolved.append(Unresolved(
                                c.id, c.rule, tc.turn, tc.line_id, tc.line_no,
                                "%s → %s" % (tc.name, base),
                                "no preceding Read/Write/Edit, but '%s' appears in an earlier shell "
                                "command — likely authored via a heredoc/redirect, so it was not "
                                "edited sight-unseen" % base,
                            ))
                        else:
                            tail = " of the same file" if c.same_path else ""
                            violations.append(Violation(
                                c.id, c.rule, c.type, tc.turn, tc.line_id, tc.line_no,
                                "%s → %s with no preceding %s%s within %d turns"
                                % (tc.name, base or "(no path)", c.require_before, tail, c.within_turns),
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
        if c.require_after:  # "require_after" is session-end by construction
            st = after_state.get(c.id)
            if st and not st["satisfied"]:
                violations.append(Violation(
                    c.id, c.rule, c.type, st["turn"], st["line_id"], st["line_no"],
                    "last %s (turn %d) was not followed by %s" % (c.trigger, st["turn"], c.require_after),
                ))

    violations.sort(key=lambda v: v.turn)
    unresolved.sort(key=lambda u: u.turn)

    # Descriptive phase timeline — attached to each finding as trigger_class,
    # rendered in the report. Never used to decide any verdict above.
    spans = build_phases(phase_seq)
    for v in violations:
        v.phase = phase_at(spans, v.turn)
    for u in unresolved:
        u.phase = phase_at(spans, u.turn)
    return ExecResult(violations=violations, unresolved=unresolved, phases=spans)


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
