"""Regression guards for two compiler/execute fixes found during pre-upload
dogfooding — both of the "a null reads as a pass" family:

  1. A function-call rule ("No `print()` ...") must compile to `\\bprint\\(` and
     fire on real calls with arguments (`print("x")`), not only the empty-paren
     literal. A check that can only match `print()` reports HELD against code
     full of `print(x)` — identical output to a rule that was obeyed.

  2. An `Edit` to a file the agent authored via a shell heredoc/redirect has no
     Read/Write/Edit tool call to credit, but writing it IS seeing it. That's
     not "edited sight-unseen" — it goes to `unresolved`, not `violated`, so the
     most-visible check type doesn't cry wolf on every heredoc-authored file.

Full compile -> execute pipeline, no LLM, no network.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mutalyze.compile_rules import compile_rules  # noqa: E402
from mutalyze.execute import execute  # noqa: E402
from mutalyze.transcript import Transcript  # noqa: E402

CLAUDE_MD = """\
# Rules
- Never use `print()` for debugging.
- Read a file before editing it.
- Use `rg` (not `grep`) for searching.
- No `any` types in TypeScript.
- Never commit directly to `main`.
"""


class TB:
    def __init__(self, cwd):
        self.lines = []; self.parent = None; self.n = 0; self.cwd = cwd

    def _base(self, uuid, etype):
        return {"uuid": uuid, "parentUuid": self.parent, "type": etype,
                "gitBranch": "feature/x", "cwd": self.cwd,
                "timestamp": "2026-07-19T10:00:00.000Z", "sessionId": "cfix"}

    def tool(self, name, inp):
        self.n += 1; uuid = "a%d" % self.n
        o = self._base(uuid, "assistant")
        o["message"] = {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t%d" % self.n, "name": name, "input": inp}]}
        self.lines.append(o); self.parent = uuid

    def dump(self, p):
        with open(p, "w", encoding="utf-8") as fh:
            for o in self.lines:
                fh.write(json.dumps(o) + "\n")


def run():
    problems = []
    repo = tempfile.mkdtemp(prefix="ruleguard_cfix_")
    with open(os.path.join(repo, "CLAUDE.md"), "w") as fh:
        fh.write(CLAUDE_MD)
    for nm in ("a.ts", "b.ts", "c.ts"):
        open(os.path.join(repo, nm), "w").write("export const x = 1\n")

    doc = compile_rules(repo)
    pr = [c for c in doc.checks if "print" in c.rule.lower()]
    # ---- Fix 1: compile shape ----
    if not pr:
        problems.append("MISS: `print()` rule did not compile to a check")
    elif pr[0].forbid_pattern != r"\bprint\(":
        problems.append("BAD PATTERN: print() -> %r (want %r)" % (pr[0].forbid_pattern, r"\bprint\("))

    def rp(*a):
        return os.path.join(repo, *a)

    tb = TB(repo)
    # (1) authored via heredoc, then edited with no Read -> UNRESOLVED, not violated
    tb.tool("Bash", {"command": "cat > %s <<'EOF'\nx = 0\nEOF" % rp("gen.py")})
    tb.tool("Edit", {"file_path": rp("gen.py"), "old_string": "x = 0", "new_string": "x = 1"})
    # (2) print() with real args in a normal edit -> VIOLATION (Fix 1)
    tb.tool("Edit", {"file_path": rp("app.py"), "old_string": "z", "new_string": 'print("debug", val)'})
    # (3) sprint( must NOT match \bprint\(  (Fix 1 word boundary)
    tb.tool("Edit", {"file_path": rp("safe.py"), "old_string": "z", "new_string": "y = sprint(3)"})
    # (4) a genuinely blind edit (never read, never in a shell command) -> VIOLATION (Fix 2 didn't over-suppress)
    tb.tool("Edit", {"file_path": rp("blind.py"), "old_string": "z", "new_string": "y = 2"})

    tpath = os.path.join(repo, "s.jsonl"); tb.dump(tpath)
    res = execute(doc.checks, Transcript(tpath), repo_root=repo)
    content_v = [v for v in res.violations if v.type == "content"]
    ordering_v = [v for v in res.violations if v.type == "ordering"]
    uev = " || ".join("%s::%s" % (u.check_id, u.evidence) for u in res.unresolved)

    # ---- Fix 1: print() fires on a real call, not on sprint( ----
    if not any("app.py" in v.evidence and "print" in v.evidence for v in content_v):
        problems.append("MISS: print() did not fire on print(\"debug\", val)")
    if any("safe.py" in v.evidence for v in content_v):
        problems.append("FALSE ALARM: print() (content) fired on sprint(3)")

    # ---- Fix 2: heredoc-authored edit is unresolved, blind edit still violated ----
    if "gen.py" not in uev:
        problems.append("MISS: heredoc-authored gen.py edit should be UNRESOLVED [u=%s]" % uev)
    if any("gen.py" in v.evidence for v in ordering_v):
        problems.append("MISLABEL: gen.py ordering routed to VIOLATED, expected unresolved")
    if not any("blind.py" in v.evidence for v in ordering_v):
        problems.append("REGRESSION: blind edit no longer flagged (Fix 2 over-suppressed)")

    return problems


SCOPE_MD = """\
# Rules
- No `print()` for debugging in `mutalyze/`.
- No `eval()` in `sandbox`.
- Use `rg` (not `grep`) for searching.
- No `any` types in TypeScript.
- Never commit directly to `main`.
"""


def run_scope():
    """A content rule scoped to a directory ("... in `mutalyze/`") must honor
    that scope — fire inside it, not outside — and an ambiguous scope must go to
    `unsupported`, never a widened check that flags the wrong tree."""
    problems = []
    repo = tempfile.mkdtemp(prefix="ruleguard_scope_")
    with open(os.path.join(repo, "CLAUDE.md"), "w") as fh:
        fh.write(SCOPE_MD)

    doc = compile_rules(repo)
    pr = [c for c in doc.checks if "print" in c.rule.lower()]
    if not pr:
        problems.append("MISS: scoped print() rule did not compile")
    elif pr[0].applies_to != ["mutalyze/*"]:
        problems.append("BAD SCOPE: applies_to=%r (want ['mutalyze/*'])" % (pr[0].applies_to,))

    # ambiguous scope (`sandbox`, no slash) -> unsupported, not a widened check
    if any("eval" in c.rule.lower() for c in doc.checks):
        problems.append("BAD: `eval()` in `sandbox` compiled a check; ambiguous scope must be unsupported")
    if not any("eval" in u["rule"].lower() for u in doc.unsupported):
        problems.append("MISS: ambiguous-scope rule should be listed unsupported")

    tb = TB(repo)
    tb.tool("Edit", {"file_path": os.path.join(repo, "mutalyze", "mod.py"),
                     "old_string": "z", "new_string": 'print("dbg", x)'})   # inside scope -> fire
    tb.tool("Edit", {"file_path": os.path.join(repo, "tests", "t.py"),
                     "old_string": "z", "new_string": 'print("ok", y)'})    # outside scope -> silent
    tpath = os.path.join(repo, "s.jsonl"); tb.dump(tpath)
    cv = [v for v in execute(doc.checks, Transcript(tpath), repo_root=repo).violations
          if v.type == "content"]
    if not any("mod.py" in v.evidence for v in cv):
        problems.append("MISS: print() inside mutalyze/ not flagged")
    if any("t.py" in v.evidence for v in cv):
        problems.append("FALSE ALARM: print() in tests/ flagged — directory scope not honored")
    return problems


def run_hardening():
    """Pre-launch compiler hardening: the failure modes that read as success on
    a stranger's rules file, plus a guard that we didn't start over-refusing."""
    from mutalyze.compile_rules import classify
    problems = []
    def c(rule): return classify(rule)[0]
    def unsupported(rule): return classify(rule)[0] is None

    # "use X, not Y" forbids Y (the rejected tool), never X (the sanctioned one)
    k = c("Use `pytest`, not `unittest`.")
    if not (k and k.forbid == ["unittest"] and k.require_instead == "pytest"):
        problems.append("use-X-not-Y: forbid the rejected tool — got %r" % (k and (k.forbid, k.require_instead)))
    if c("Use `rg` instead of `grep`.").forbid != ["grep"]:
        problems.append("use-X-instead-Y forbids the wrong token")
    if c("Prefer `vitest` over `jest`.").forbid != ["jest"]:
        problems.append("prefer-X-over-Y forbids the wrong token")

    # comment-marker rules -> unsupported (a check on stripped comments never fires)
    if not unsupported("No `TODO`."): problems.append("comment marker `TODO` should be unsupported, not an inert check")
    if not unsupported("No `XXX` markers in code."): problems.append("comment marker `XXX` should be unsupported")

    # conditional rules -> unsupported (a blanket forbid over-fires on the allowed case)
    if not unsupported("Never use `pip install` without a venv."): problems.append("conditional (without) should be unsupported")
    if not unsupported("No `--force` unless rebasing your own branch."): problems.append("conditional (unless) should be unsupported")

    # guard: we did NOT start over-refusing the canonical rules
    for keep in ("Never use `eval`.", "Do not commit directly to `main`.", "No `console.log`."):
        if unsupported(keep): problems.append("REGRESSION: over-refused a canonical rule: %r" % keep)
    return problems


def main():
    problems = run() + run_scope() + run_hardening()
    if problems:
        for p in problems:
            print("FAIL:", p)
        sys.exit(1)
    print("compile/execute fixes PASS (print() fires on real calls; heredoc edits -> unresolved; "
          "blind edits still flagged; directory scope honored; ambiguous scope -> unsupported; "
          "use-X-not-Y forbids Y; comment-marker + conditional rules -> unsupported)")


if __name__ == "__main__":
    main()
