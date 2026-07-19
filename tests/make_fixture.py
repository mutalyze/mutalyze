"""Build a labeled synthetic transcript + CLAUDE.md, then assert ruleguard's
output matches the ground truth. Proves each check type fires and that the
false-alarm guards hold. No network, no LLM."""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from ruleguard.compile_rules import compile_rules  # noqa: E402
from ruleguard.execute import execute  # noqa: E402
from ruleguard.transcript import Transcript  # noqa: E402

CLAUDE_MD = """\
# Project rules

- Use `cargo nextest` (not `cargo test`).
- Never commit directly to `main`.
- No `any` types in TypeScript.
- Never use `console.log`.
- Read a file before editing it.
- Use `rg` (not `grep`) for searching.
- Always run `npm test` before finishing.
- Write clean, maintainable code.
- Prefer descriptive variable names.
"""


class TB:
    """Builds a JSONL transcript with a valid parent chain and gitBranch."""

    def __init__(self, branch="feature/x", cwd=None):
        self.lines = []
        self.parent = None
        self.n = 0
        self.branch = branch
        self.cwd = cwd  # recorded working directory (real transcripts always have it)

    def _base(self, uuid, etype):
        base = {
            "uuid": uuid, "parentUuid": self.parent, "type": etype,
            "gitBranch": self.branch, "timestamp": "2026-07-19T10:%02d:00.000Z" % (self.n % 60),
            "sessionId": "test",
        }
        if self.cwd:
            base["cwd"] = self.cwd
        return base

    def assistant_tool(self, name, tool_input):
        self.n += 1
        uuid = "a%d" % self.n
        obj = self._base(uuid, "assistant")
        obj["message"] = {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t%d" % self.n, "name": name, "input": tool_input}
        ]}
        self.lines.append(obj)
        self.parent = uuid
        return "t%d" % self.n

    def tool_result(self, tool_id, result):
        self.n += 1
        uuid = "u%d" % self.n
        obj = self._base(uuid, "user")
        obj["message"] = {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}
        ]}
        obj["toolUseResult"] = result
        self.lines.append(obj)
        self.parent = uuid

    def set_branch(self, b):
        self.branch = b

    def write(self, path):
        return open(path, "w", encoding="utf-8")

    def dump(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            for o in self.lines:
                fh.write(json.dumps(o) + "\n")


def build(repo):
    os.makedirs(repo, exist_ok=True)
    with open(os.path.join(repo, "CLAUDE.md"), "w", encoding="utf-8") as fh:
        fh.write(CLAUDE_MD)
    # make the repo look like a TS project so the content-check guard is happy
    for name in ("a.ts", "b.ts", "c.ts", "d.ts", "e.py"):
        with open(os.path.join(repo, name), "w", encoding="utf-8") as fh:
            fh.write("export const x = 1\n")

    tb = TB(branch="feature/x", cwd=repo)

    # (V) forbidden command: cargo test
    tb.assistant_tool("Bash", {"command": "cargo test --lib"})           # VIOLATION cargo nextest
    # (H) sanctioned command holds
    tb.assistant_tool("Bash", {"command": "cargo nextest run"})          # held

    # (V) no `any` in a real .ts edit
    tb.assistant_tool("Edit", {"file_path": os.path.join(repo, "a.ts"),
                               "old_string": "x", "new_string": "const opts: any = {}"})  # VIOLATION
    # (FALSE-ALARM GUARD) `any` only inside a comment + a string -> must NOT fire
    tb.assistant_tool("Edit", {"file_path": os.path.join(repo, "b.ts"),
                               "old_string": "x",
                               "new_string": "// no any types allowed here\nconst s = \"there is any inside a string\""})  # held
    # (V) console.log in an edit
    tb.assistant_tool("Edit", {"file_path": os.path.join(repo, "c.ts"),
                               "old_string": "x", "new_string": "console.log(opts)"})     # VIOLATION

    # ordering: read-before-edit
    # (H) read d.ts, then edit d.ts -> holds
    tid = tb.assistant_tool("Read", {"file_path": os.path.join(repo, "d.ts")})
    tb.assistant_tool("Edit", {"file_path": os.path.join(repo, "d.ts"),
                               "old_string": "x", "new_string": "const ok = 1"})          # held (read first)
    # (V) edit a.ts again is fine (a.ts never read) -> but that's the `any` file; use a fresh unread file
    tb.assistant_tool("Edit", {"file_path": os.path.join(repo, "c.ts"),
                               "old_string": "y", "new_string": "const z = 2"})           # VIOLATION read-before-edit (c.ts never read)

    # (FALSE-ALARM GUARD) Write to a file that was NOT created this session ->
    # content check must be skipped even though it contains `any`.
    tb.assistant_tool("Write", {"file_path": os.path.join(repo, "e.py"),
                                "content": "val: any = 1\n"})                             # held (update, not create)

    # (FALSE-ALARM GUARD) edit a file OUTSIDE the governed repo -> must be
    # skipped (this repo's CLAUDE.md doesn't govern another checkout).
    tb.assistant_tool("Edit", {"file_path": "/tmp/other-repo/out.ts",
                               "old_string": "x", "new_string": "const bad: any = {}"})    # held (out of repo)

    # (H) create a NEW file with `any` in it, but it's .py so the TS `any` rule's
    # applies_to (*.ts,*.tsx) should NOT match -> held.
    wid = tb.assistant_tool("Write", {"file_path": os.path.join(repo, "new.py"),
                                      "content": "z: any = 2\n"})
    tb.tool_result(wid, {"type": "create", "filePath": os.path.join(repo, "new.py")})

    # branch check: switch to main and commit -> VIOLATION
    tb.set_branch("main")
    tb.assistant_tool("Bash", {"command": "git commit -m 'wip'"})                         # VIOLATION commit on main
    # same command on a feature branch -> held
    tb.set_branch("feature/x")
    tb.assistant_tool("Bash", {"command": "git commit -m 'ok here'"})                     # held (not main)

    # (V) grep in-repo -> violates "use rg not grep" (invoked in the repo)
    tb.assistant_tool("Bash", {"command": "grep -n foo %s" % os.path.join(repo, "a.ts")})   # VIOLATION rg-not-grep
    # (UNRESOLVED) grep after cd into a shell variable -> location unknown
    tb.assistant_tool("Bash", {"command": 'cd "$SANDBOX" && grep -n foo bar.txt'})           # UNRESOLVED
    # (V, pack) force-push -> SP001, session-scoped (fires regardless of location)
    tb.assistant_tool("Bash", {"command": "git push --force origin main"})                    # VIOLATION SP001
    # (V, pack) rm -rf a home path -> SP003, session-scoped
    tb.assistant_tool("Bash", {"command": "rm -rf ~/.cache/junk"})                            # VIOLATION SP003

    tpath = os.path.join(repo, "session.jsonl")
    tb.dump(tpath)
    return tpath


def main():
    import tempfile
    repo = tempfile.mkdtemp(prefix="ruleguard_fix_")
    tpath = build(repo)

    doc = compile_rules(repo)
    print("=== COMPILED CHECKS ===")
    for c in doc.checks:
        print(" ", c.id, c.type, "|", c.rule)
    print("unsupported:", [u["rule"] for u in doc.unsupported])

    from ruleguard.safety_pack import builtin_checks

    transcript = Transcript(tpath)
    result = execute(doc.checks + builtin_checks(), transcript, repo_root=repo)
    violations = result.violations
    unresolved = result.unresolved
    print("\n=== UNRESOLVED (%d) ===" % len(unresolved))
    for u in unresolved:
        print("  turn %d  %s  %s" % (u.turn, u.check_id, u.evidence))
    print("\n=== VIOLATIONS (%d) ===" % len(violations))
    for v in violations:
        print("  turn %d  %s  %s\n         %s" % (v.turn, v.check_id, v.rule, v.evidence))

    # ---- assertions against ground truth ----
    problems = []
    fired_text = " || ".join(v.rule + " :: " + v.evidence for v in violations)
    content = [v for v in violations if v.type == "content"]
    ordering = [v for v in violations if v.type == "ordering"]
    command = [v for v in violations if v.type == "command"]

    def has(vs, needle):
        return any(needle in v.evidence for v in vs)

    # Expected TRUE positives, by type
    if not has(command, "cargo test"):
        problems.append("MISS: cargo test not flagged")
    if not has(content, "a.ts") or not has(content, "any"):
        problems.append("MISS: `any` in a.ts not flagged")
    if not has(content, "console.log"):
        problems.append("MISS: console.log not flagged")
    if not has(command, "git commit -m 'wip'"):
        problems.append("MISS: commit on main not flagged")
    if not any("npm test" in u["rule"] for u in doc.unsupported):
        problems.append("MISS: 'always run npm test' should be unsupported (session-absence, no turn to cite)")
    if not (has(ordering, "a.ts") and has(ordering, "b.ts") and has(ordering, "c.ts")):
        problems.append("MISS: read-before-edit did not fire on the unread files")

    # False-alarm guards
    if has(content, "b.ts"):
        problems.append("FALSE ALARM: content check fired on b.ts (any only in comment/string)")
    if has(content, "e.py"):
        problems.append("FALSE ALARM: content check fired on e.py (Write that was an update, not create)")
    if has(content, "out.ts") or has(ordering, "out.ts"):
        problems.append("FALSE ALARM: fired on /tmp/other-repo/out.ts (outside the governed repo)")
    if has(content, "new.py"):
        problems.append("FALSE ALARM: content check fired on new.py (.py file, TS-only rule)")
    if has(ordering, "d.ts"):
        problems.append("FALSE ALARM: read-before-edit fired on d.ts (it WAS read first)")
    if any("nextest run" in v.evidence for v in command):
        problems.append("FALSE ALARM: flagged the sanctioned `cargo nextest run`")
    if any("ok here" in v.evidence for v in command):
        problems.append("FALSE ALARM: flagged a commit on a non-main branch")

    # New: scope, unresolved bucket, safety pack
    if not has(command, "grep -n foo"):
        problems.append("MISS: in-repo grep not flagged (rg-not-grep, invoked in repo)")
    if not any("SP001" == u for u in [v.check_id for v in violations]):
        problems.append("MISS: SP001 force-push (safety pack) not flagged")
    if not any("SP003" == v.check_id for v in violations):
        problems.append("MISS: SP003 rm -rf home path (safety pack, session-scoped) not flagged")
    # the cd-into-$VAR grep must be UNRESOLVED, not a violation and not dropped
    if any('bar.txt' in v.evidence for v in violations):
        problems.append("FALSE ALARM: cd-into-$VAR grep reported as a violation (should be unresolved)")
    if not any('bar.txt' in u.evidence for u in unresolved):
        problems.append("MISS: cd-into-$VAR grep should be in the unresolved bucket")

    print("\n=== RESULT ===")
    if problems:
        for p in problems:
            print("  FAIL:", p)
        sys.exit(1)
    print("  PASS — every expected check fired; no false alarms on the guarded cases.")
    print("  repo:", repo)


if __name__ == "__main__":
    main()
