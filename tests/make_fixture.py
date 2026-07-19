"""Build a labeled synthetic transcript + CLAUDE.md, then assert cellamind's
output matches the ground truth. Proves each check type fires and that the
false-alarm guards hold. No network, no LLM."""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from cellamind.compile_rules import compile_rules  # noqa: E402
from cellamind.execute import execute  # noqa: E402
from cellamind.transcript import Transcript  # noqa: E402

CLAUDE_MD = """\
# Project rules

- Use `cargo nextest` (not `cargo test`).
- Never commit directly to `main`.
- No `any` types in TypeScript.
- Never use `console.log`.
- Read a file before editing it.
- Always run `npm test` before finishing.
- Write clean, maintainable code.
- Prefer descriptive variable names.
"""


class TB:
    """Builds a JSONL transcript with a valid parent chain and gitBranch."""

    def __init__(self, branch="feature/x"):
        self.lines = []
        self.parent = None
        self.n = 0
        self.branch = branch

    def _base(self, uuid, etype):
        return {
            "uuid": uuid, "parentUuid": self.parent, "type": etype,
            "gitBranch": self.branch, "timestamp": "2026-07-19T10:%02d:00.000Z" % (self.n % 60),
            "sessionId": "test",
        }

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

    tb = TB(branch="feature/x")

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

    # NOTE: `npm test` is never run -> require_present VIOLATION at session end

    tpath = os.path.join(repo, "session.jsonl")
    tb.dump(tpath)
    return tpath


def main():
    import tempfile
    repo = tempfile.mkdtemp(prefix="cellamind_fix_")
    tpath = build(repo)

    doc = compile_rules(repo)
    print("=== COMPILED CHECKS ===")
    for c in doc.checks:
        print(" ", c.id, c.type, "|", c.rule)
    print("unsupported:", [u["rule"] for u in doc.unsupported])

    transcript = Transcript(tpath)
    violations = execute(doc.checks, transcript)
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
    if has(content, "new.py"):
        problems.append("FALSE ALARM: content check fired on new.py (.py file, TS-only rule)")
    if has(ordering, "d.ts"):
        problems.append("FALSE ALARM: read-before-edit fired on d.ts (it WAS read first)")
    if any("nextest run" in v.evidence for v in command):
        problems.append("FALSE ALARM: flagged the sanctioned `cargo nextest run`")
    if any("ok here" in v.evidence for v in command):
        problems.append("FALSE ALARM: flagged a commit on a non-main branch")

    print("\n=== RESULT ===")
    if problems:
        for p in problems:
            print("  FAIL:", p)
        sys.exit(1)
    print("  PASS — every expected check fired; no false alarms on the guarded cases.")
    print("  repo:", repo)


if __name__ == "__main__":
    main()
