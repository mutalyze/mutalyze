"""Phase 1 — compile a natural-language rules file into executable checks.

This is deliberately deterministic and offline: no LLM calls. It follows the
extraction heuristics validated on 128 real rules files, then classifies each
kept line into command / content / ordering with a precision-first bias. When a
line cannot be classified with confidence it is recorded under ``unsupported``
with a reason — never silently dropped, and never turned into a shaky check.

A wrongly-compiled rule produces a false violation, and false violations
destroy trust faster than missed rules do. So: when in doubt, mark unsupported.
"""

from __future__ import annotations

import os
import re
from typing import List, Optional, Tuple

from .checks import COMMAND, CONTENT, ORDERING, Check, CompiledDoc

# ---------------------------------------------------------------------------
# Rules-file discovery (handle symlink stubs; dedupe CLAUDE.md / AGENTS.md)
# ---------------------------------------------------------------------------

RULE_FILENAMES = ("CLAUDE.md", "AGENTS.md")


def _resolve_stub(path: str) -> str:
    """CLAUDE.md is frequently a tiny symlink (or a stub file whose entire
    content is the target filename). Resolve either form to the real file."""
    if os.path.islink(path):
        target = os.path.join(os.path.dirname(path), os.readlink(path))
        if os.path.exists(target):
            return target
    try:
        if os.path.getsize(path) <= 64:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                body = fh.read().strip()
            if body and "\n" not in body and body.lower().endswith(".md"):
                candidate = os.path.join(os.path.dirname(path), body)
                if os.path.exists(candidate):
                    return candidate
    except OSError:
        pass
    return path


def find_rules_files(repo_root: str) -> List[str]:
    resolved: List[str] = []
    seen_real: set = set()
    for name in RULE_FILENAMES:
        p = os.path.join(repo_root, name)
        if not os.path.exists(p):
            continue
        real = os.path.realpath(_resolve_stub(p))
        if real in seen_real:
            continue
        seen_real.add(real)
        resolved.append(real)
    return resolved


# ---------------------------------------------------------------------------
# Extraction — pull candidate normative lines out of the prose
# ---------------------------------------------------------------------------

_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_BACKTICK_RE = re.compile(r"`([^`]+)`")

# Normative tokens (trailing spaces on some are intentional — cut false hits).
_NORMATIVE = [
    "must", "never", "always", "do not", "don't", "no ", "only",
    "required", "forbidden", "use ", "run ", "avoid", "ensure",
]
# The subset that signals a genuinely prescriptive rule (used to rescue a
# descriptive "`x` — desc" line only when a strong verb is present).
_STRONG = ["must", "never", "always", "do not", "don't", "avoid", "forbidden", "required", "only"]

_DESCRIPTIVE_RE = re.compile(r"^\s*`[^`]+`\s*[-—:]")


def _accept_candidate(raw: str, out: List[str]) -> None:
    """Apply the keep/drop heuristics to one assembled bullet body."""
    body = re.sub(r"\s+", " ", raw).strip()
    if not (12 <= len(body) <= 300):
        return
    low = body.lower()
    # Ordering rules ("read a file before editing it") often carry no
    # normative verb, so also keep lines that express a "before" sequence.
    if not any(tok in low for tok in _NORMATIVE) and " before " not in low:
        return
    # Drop descriptive "`thing` — description" lines unless a strong verb
    # makes them genuinely prescriptive.
    if _DESCRIPTIVE_RE.match(body) and not any(s in low for s in _STRONG):
        return
    out.append(body)


def extract_candidates(text: str) -> List[str]:
    """Return the bullet/numbered rules, each joined across its wrapped lines.

    A rule authored across two lines —

        - Phase 2 (`execute.py`) must never call an LLM or the network. Every
          violation stays deterministic and citable.

    is one rule, not half a rule. Indented continuation lines are folded into
    the bullet before the heuristics run; truncating at the newline used to cut
    rules mid-sentence, which then reached the classifier (and the rule store)
    as fragments. Continuations must be *indented* — unindented prose after a
    list stays a separate paragraph, so following text is never swallowed.
    """
    out: List[str] = []
    in_fence = False
    buf: Optional[str] = None

    for line in text.splitlines():
        if _FENCE_RE.match(line):
            if buf is not None:
                _accept_candidate(buf, out)
                buf = None
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        m = _BULLET_RE.match(line)
        if m:
            if buf is not None:
                _accept_candidate(buf, out)
            buf = m.group(1).strip()  # a nested bullet is its own rule, not a continuation
            continue

        stripped = line.strip()
        is_continuation = (
            buf is not None
            and stripped
            and line[:1].isspace()          # indented → belongs to the bullet
            and not stripped.startswith("#")
        )
        if is_continuation:
            buf = "%s %s" % (buf, stripped)
            continue

        if buf is not None:
            _accept_candidate(buf, out)
            buf = None

    if buf is not None:
        _accept_candidate(buf, out)
    return out


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

# Leading tokens that make a backticked span look like a shell command.
_COMMAND_VERBS = {
    "git", "npm", "pnpm", "yarn", "npx", "node", "bun", "deno",
    "cargo", "rustc", "go", "python", "python3", "pip", "pip3", "uv",
    "poetry", "pytest", "tox", "make", "cmake", "bazel", "gradle", "mvn",
    "docker", "docker-compose", "kubectl", "helm", "terraform",
    "ruff", "black", "isort", "flake8", "mypy", "pyright", "eslint",
    "prettier", "tsc", "vitest", "jest", "rspec", "rake", "bundle",
    "gcc", "clang", "sh", "bash", "curl", "cd", "rm", "cp", "mv", "sudo",
    "grep", "rg", "ag", "find", "sed", "awk", "cat", "ls", "echo", "mkdir",
    "touch", "chmod", "chown", "ln", "tar", "ssh", "scp", "wget", "jq",
    "head", "tail", "sort", "uniq", "xargs", "tee", "kill", "ps", "git-lfs",
}

_NEG_RE = re.compile(
    r"\b(never|do not|don't|dont|avoid|forbidden|must not|shouldn't|should not"
    r"|instead of|rather than|not|no)\b", re.IGNORECASE
)
_RUN_RE = re.compile(r"\b(always|must|ensure|run|execute)\b", re.IGNORECASE)

_CONDITION_RE = re.compile(r"\b(without|unless|except|when not|if not)\b", re.IGNORECASE)

# Tokens that live in code comments (TODO, FIXME, …). Content matching strips
# comments FIRST, so a rule forbidding one can never fire — compiling it would be
# a null that reads as "held". Refuse instead of emitting a check that lies.
_COMMENT_MARKERS = {"TODO", "FIXME", "XXX", "HACK", "WIP", "BUG", "NOTE", "OPTIMIZE", "TEMP"}

# Language / extension mentions -> file globs for content `applies_to`.
_LANG_GLOBS = {
    "typescript": ["*.ts", "*.tsx"], "ts": ["*.ts", "*.tsx"], "tsx": ["*.tsx"],
    "javascript": ["*.js", "*.jsx"], "js": ["*.js", "*.jsx"],
    "python": ["*.py"], "py": ["*.py"],
    "rust": ["*.rs"], "go": ["*.go"], "golang": ["*.go"],
    "java": ["*.java"], "ruby": ["*.rb"], "css": ["*.css"], "html": ["*.html"],
}

# Words (and common inflections) that map onto a concrete tool name, for
# ordering checks. Whole-word matched, so inflections must be listed.
_TOOL_WORDS = {
    "grep": "Grep", "grepping": "Grep", "search": "Grep", "searching": "Grep", "ripgrep": "Grep",
    "read": "Read", "reading": "Read", "open": "Read", "opening": "Read",
    "edit": "Edit", "editing": "Edit", "modify": "Edit", "modifying": "Edit",
    "change": "Edit", "changing": "Edit",
    "write": "Write", "writing": "Write", "create": "Write", "creating": "Write",
}


def _looks_like_command(span: str) -> bool:
    span = span.strip()
    if not span:
        return False
    first = span.split()[0].lower()
    if first in _COMMAND_VERBS:
        return True
    # subcommand shape like "cargo nextest run" already covered by first-token;
    # also accept an explicit flag which is command-ish and never valid code.
    return bool(re.match(r"^[a-z][\w.-]*\s+(?:[a-z]|-)", span)) and first in _COMMAND_VERBS


# Source-file extensions: a backticked `execute.py` in a rule is a *reference*
# to a file, not a forbidden code token — forbidding it would fire on imports.
_FILENAME_RE = re.compile(
    r"^[\w./-]+\.(py|js|jsx|ts|tsx|go|rs|rb|java|c|cc|cpp|h|hpp|cs|md|json|ya?ml|"
    r"toml|txt|html|css|scss|sh)$", re.IGNORECASE
)


def _is_code_token(span: str) -> bool:
    """A short code-like span suitable for a content forbid (not prose)."""
    span = span.strip()
    if not span or len(span) > 40:
        return False
    if _looks_like_command(span):
        return False
    if _FILENAME_RE.match(span):
        return False  # a filename reference, not forbidden code
    if span.endswith("/"):
        return False  # a directory/location (e.g. `mutalyze/`), not a code token
    # identifier, dotted/namespaced name, call, decorator, or short symbol.
    # Parens are allowed so a function call like `print()` is recognized as the
    # forbidden token — without them it was skipped, and a co-occurring path span
    # ("No `print()` in `mutalyze/`") got forbidden instead, firing on any line
    # that merely mentioned the path.
    return bool(re.match(r"^[@#]?[\w.:<>()\-\[\]/ ]+$", span)) and span.count(" ") <= 1


def _globs_for(low: str) -> List[str]:
    for word, globs in _LANG_GLOBS.items():
        if re.search(r"\b" + re.escape(word) + r"\b", low):
            return list(globs)
    return []


# A location scope a rule names for a content rule: "... in `mutalyze/`",
# "under `/app/legacy/`", "inside `src/`". The forbidden token comes first and
# is skipped; this captures the *place*.
_SCOPE_RE = re.compile(r"\b(?:in|under|inside|within)\b[^`]*`([^`]+)`", re.I)


def _dir_globs(span: str) -> List[str]:
    """A directory span -> a repo-relative glob. `mutalyze/` -> `mutalyze/*`;
    `/app/legacy/` -> `app/legacy/*`. Matched against the repo-relative path in
    execute._applies, so it scopes to that subtree without baking an absolute,
    machine-specific path into the (shareable, hand-editable) checks.yaml."""
    d = span.strip().strip("/")
    return [d + "/*"] if d else []


def _content_pattern(token: str) -> Tuple[Optional[str], List[str]]:
    """Turn a forbidden code token into (forbid_pattern, forbid_literals).

    Alphanumeric identifiers get a word-boundary regex (so `any` does not match
    `many`); anything with symbols is matched as a literal substring.
    """
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token):
        return r"\b" + re.escape(token) + r"\b", []
    # A function-call token — `print()`, `console.log()` — means "any call to it".
    # Compile to `\bname\(` so it matches real calls WITH arguments (print("x")),
    # not just the empty-paren literal `print()`. A rule that can only match
    # `print()` reports HELD against code full of `print(x)` — a null that reads
    # as a pass. `\b` keeps it from matching `sprint(`/`fingerprint(`; comment and
    # string stripping handles the rest.
    m = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_.]*)\(\)", token)
    if m:
        return r"\b" + re.escape(m.group(1)) + r"\(", []
    return None, [token]


# ---------------------------------------------------------------------------
# Per-line classification
# ---------------------------------------------------------------------------

def classify(rule: str) -> Tuple[Optional[Check], Optional[str]]:
    """Return (Check, None) if classifiable, else (None, reason-unsupported)."""
    low = rule.lower()
    spans = _BACKTICK_RE.findall(rule)
    negative = bool(_NEG_RE.search(rule))

    # A rule conditioned on something we can't verify ("... without a venv",
    # "... unless reviewed", "... except in tests") must NOT become a blanket
    # forbid — that over-fires on the allowed case. Refuse rather than cry wolf.
    cond = _CONDITION_RE.search(rule)
    if negative and cond:
        return (None, "conditional on \"%s\" — not machine-checkable; a blanket check would "
                      "over-fire, so it's unsupported. Set it up in .mutalyze/checks.yaml by hand"
                      % cond.group(1).lower())

    # 1) git branch-protection: "never commit/push directly to main"
    if negative and re.search(r"\b(main|master)\b", low):
        op = None
        for verb in ("commit", "push", "merge", "force-push", "force push"):
            if verb in low:
                op = "commit" if verb == "commit" else ("push" if "push" in verb else verb)
                break
        if op:
            branch = "main" if "main" in low else "master"
            pat = {"commit": r"git\s+commit", "push": r"git\s+push", "merge": r"git\s+merge"}.get(op, r"git\s+" + re.escape(op))
            return (
                Check(id="", rule=rule, type=COMMAND, forbid_pattern=pat, when_branch=branch),
                None,
            )

    # 2) "use `X` (not `Y`)" / "prefer `X` over `Y`" / "`Y` instead" — forbid Y,
    #    the REJECTED alternative, never X the sanctioned one. Y counts as a
    #    command if it looks like one OR if X does: "use `pytest`, not `unittest`"
    #    makes `unittest` a peer tool even though it isn't in the verb list.
    #    Getting this backwards forbids the very tool the user was told to use.
    m_not = re.search(r"(?:not|instead of|rather than|don't use|never use|avoid|over)\s+`([^`]+)`", rule, re.IGNORECASE)
    m_use = re.search(r"(?:use|prefer|run|switch to)\s+`([^`]+)`", rule, re.IGNORECASE)
    if m_not:
        y = m_not.group(1).strip()
        x = m_use.group(1).strip() if m_use else None
        y_token = bool(re.fullmatch(r"[\w.\-/]+", y))
        if _looks_like_command(y) or (x and x != y and _looks_like_command(x) and y_token):
            return (
                Check(id="", rule=rule, type=COMMAND, forbid=[y],
                      require_instead=(x if x and x != y else None)),
                None,
            )

    # 3) generic forbidden command: "never run `X`" / "avoid `X`"
    if negative:
        for span in spans:
            if _looks_like_command(span):
                return (Check(id="", rule=rule, type=COMMAND, forbid=[span.strip()]), None)

    # 4) ordering: "<toolword> ... before ... <toolword>" (e.g. "read a file
    #    before editing it"). Both sides must map to concrete tool names, or it
    #    goes unsupported — Phase 2 has no LLM to judge intent.
    if " before " in low:
        left, right = low.split(" before ", 1)  # "X before Y": X first, then Y
        require_before = _match_tool_word(left)  # X — action that must come first
        trigger = _match_tool_word(right)  # Y — action gated on it
        if require_before and trigger and require_before != trigger:
            same_path = trigger in ("Edit", "Write") and require_before in ("Read", "Edit")
            # "Read before Edit": having created (Write) or already Edited the
            # file also means you've seen it — accept any of them, so only a
            # genuinely sight-unseen first edit is flagged.
            within = 200
            if trigger == "Edit" and require_before == "Read":
                require_before = "Read,Write,Edit"
                within = 0  # "have I ever seen this file" is inherently unbounded
            return (
                Check(
                    id="", rule=rule, type=ORDERING,
                    trigger=trigger, require_before=require_before,
                    same_path=same_path, within_turns=within,
                ),
                None,
            )

    # 5) content: forbidden code token in written output, optionally scoped to a
    #    directory the rule names ("... in `mutalyze/`"). Dropping that scope
    #    turns "no print() in mutalyze/" into "no print() anywhere" — a check
    #    that flags tests/ while the rule plainly says otherwise, wrong in a way
    #    anyone spots at a glance. So honor the scope when it's an unambiguous
    #    directory path, and REFUSE (mark unsupported — counted and listed) when
    #    it isn't, rather than silently widen it. Refuse-rather-than-report,
    #    applied to compilation.
    if negative:
        scope_m = _SCOPE_RE.search(rule)
        scope_span = scope_m.group(1).strip() if scope_m else None
        for span in spans:
            s = span.strip()
            if s == scope_span:
                continue  # the location, not the forbidden token
            if _is_code_token(s):
                if s.upper() in _COMMENT_MARKERS or re.search(r"\bcomments?\b", low):
                    return (None, "targets code comments, which are stripped before content "
                                  "matching — not checkable without a comment-aware mode")
                pat, literals = _content_pattern(s)
                if scope_span:
                    if "/" in scope_span:
                        applies = _dir_globs(scope_span)  # unambiguous directory — honor it
                    else:
                        return (None, "rule scopes to `%s` but that isn't an unambiguous "
                                      "directory path; scope not honored — set applies_to in "
                                      ".mutalyze/checks.yaml by hand" % scope_span)
                else:
                    applies = _globs_for(low)
                return (
                    Check(
                        id="", rule=rule, type=CONTENT,
                        forbid=literals, forbid_pattern=pat,
                        applies_to=applies,
                    ),
                    None,
                )

    # "always run `X`" / "ensure `X` passes" is a session-absence check: it can
    # only ever cite "end of session", not a real turn with literal evidence,
    # and it usually hinges on a fuzzy scope ("before finishing/committing").
    # That fails the every-violation-cites-a-turn contract, so we don't compile
    # it — mark unsupported and let a human turn it into an ordering rule.
    if not negative and _RUN_RE.search(rule):
        for span in spans:
            if _looks_like_command(span):
                return (None, "'always run X' is a session-absence check with no turn to cite; "
                              "rewrite as an ordering rule (e.g. trigger before commit) by hand")

    # Unclassifiable with confidence.
    if spans:
        return (None, "no confident mechanical mapping for the referenced token(s)")
    return (None, "not mechanically verifiable")


def _match_tool_word(text: str) -> Optional[str]:
    for word, tool in _TOOL_WORDS.items():
        if re.search(r"\b" + re.escape(word) + r"\b", text):
            return tool
    return None


# NOTE: scope is NEVER inferred from user prose. A compiled rule is always
# scope=repo; only a human writing `scope: session` in checks.yaml, or an
# authored safety-pack check, gets session scope. Guessing intent from a
# sentence and then handing it the widest blast radius is exactly the silent
# guess the `unresolved` bucket exists to refuse — and the dangerous behavior is
# already covered, precisely, by the authored safety pack. A safety category the
# pack misses is a signal to add it to the pack, not to teach the compiler to
# guess.


# ---------------------------------------------------------------------------
# Top-level compile
# ---------------------------------------------------------------------------

def compile_rules(repo_root: str) -> Optional[CompiledDoc]:
    files = find_rules_files(repo_root)
    if not files:
        return None

    source = ", ".join(os.path.basename(f) for f in files)
    checks: List[Check] = []
    unsupported: List[dict] = []
    seen_rules: set = set()
    counter = 1

    for path in files:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        for rule in extract_candidates(text):
            key = re.sub(r"\s+", " ", rule.strip().lower())
            if key in seen_rules:
                continue
            seen_rules.add(key)
            check, reason = classify(rule)
            if check is not None:
                check.id = "CM%03d" % counter
                counter += 1
                checks.append(check)  # scope stays "repo"; never inferred
            else:
                unsupported.append({"rule": rule, "reason": reason or "unsupported"})

    return CompiledDoc(source=source, checks=checks, unsupported=unsupported)
