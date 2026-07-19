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


def extract_candidates(text: str) -> List[str]:
    """Return the bullet/numbered lines that carry a normative token."""
    out: List[str] = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _BULLET_RE.match(line)
        if not m:
            continue
        body = m.group(1).strip()
        if not (12 <= len(body) <= 300):
            continue
        low = body.lower()
        # Ordering rules ("read a file before editing it") often carry no
        # normative verb, so also keep lines that express a "before" sequence.
        if not any(tok in low for tok in _NORMATIVE) and " before " not in low:
            continue
        # Drop descriptive "`thing` — description" lines unless a strong verb
        # makes them genuinely prescriptive.
        if _DESCRIPTIVE_RE.match(body) and not any(s in low for s in _STRONG):
            continue
        out.append(body)
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
    # identifier, dotted/namespaced name, decorator, or short symbol
    return bool(re.match(r"^[@#]?[\w.:<>\-\[\]/ ]+$", span)) and span.count(" ") <= 1


def _globs_for(low: str) -> List[str]:
    for word, globs in _LANG_GLOBS.items():
        if re.search(r"\b" + re.escape(word) + r"\b", low):
            return list(globs)
    return []


def _content_pattern(token: str) -> Tuple[Optional[str], List[str]]:
    """Turn a forbidden code token into (forbid_pattern, forbid_literals).

    Alphanumeric identifiers get a word-boundary regex (so `any` does not match
    `many`); anything with symbols is matched as a literal substring.
    """
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token):
        return r"\b" + re.escape(token) + r"\b", []
    return None, [token]


# ---------------------------------------------------------------------------
# Per-line classification
# ---------------------------------------------------------------------------

def classify(rule: str) -> Tuple[Optional[Check], Optional[str]]:
    """Return (Check, None) if classifiable, else (None, reason-unsupported)."""
    low = rule.lower()
    spans = _BACKTICK_RE.findall(rule)
    negative = bool(_NEG_RE.search(rule))

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

    # 2) "use `X` (not `Y`)" / "`Y` instead" — forbidden command + alternative
    m_not = re.search(r"(?:not|instead of|rather than|don't use|never use|avoid)\s+`([^`]+)`", rule, re.IGNORECASE)
    m_use = re.search(r"(?:use|prefer|run)\s+`([^`]+)`", rule, re.IGNORECASE)
    if m_not and _looks_like_command(m_not.group(1)):
        instead = None
        if m_use and _looks_like_command(m_use.group(1)) and m_use.group(1) != m_not.group(1):
            instead = m_use.group(1).strip()
        return (
            Check(id="", rule=rule, type=COMMAND, forbid=[m_not.group(1).strip()], require_instead=instead),
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

    # 5) content: forbidden code token in written output
    if negative:
        for span in spans:
            if _is_code_token(span):
                pat, literals = _content_pattern(span.strip())
                return (
                    Check(
                        id="", rule=rule, type=CONTENT,
                        forbid=literals, forbid_pattern=pat,
                        applies_to=_globs_for(low),
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
