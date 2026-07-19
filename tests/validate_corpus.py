"""Hand-validation harness: for every reported violation, independently
re-derive the truth from the cited transcript line and classify it
TRUE / FALSE / UNSURE. Prints the false-alarm rate per check and overall.

Independent logic (does NOT reuse the executor's verdict):
 - command: pull the full Bash command at the cited line; string-strip it;
   the forbidden token must survive stripping (i.e. be a real command token,
   not text inside a quoted search pattern).
 - content: pull the added text; string/comment-strip it; the pattern must
   still match.
 - ordering (read-before-edit): scan the WHOLE main path before the trigger
   for any Read/Write/Edit of the same path.
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ruleguard.checks import COMMAND, CONTENT, ORDERING  # noqa: E402
from ruleguard.code_strip import strip_code  # noqa: E402
from ruleguard.compile_rules import compile_rules  # noqa: E402
from ruleguard.execute import execute  # noqa: E402
from ruleguard.transcript import Transcript  # noqa: E402


def raw_obj(path, line_no):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for i, ln in enumerate(fh, 1):
            if i == line_no:
                try:
                    return json.loads(ln)
                except Exception:
                    return None
    return None


def tool_at(obj, want_names):
    if not obj or obj.get("type") != "assistant":
        return None
    for b in (obj.get("message", {}) or {}).get("content", []) or []:
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in want_names:
            return b
    return None


def classify(check_by_id, transcript, path, v):
    c = check_by_id.get(v.check_id)
    if c is None:
        return "UNSURE", "no check def"

    if c.type == COMMAND:
        b = tool_at(raw_obj(path, v.line_no), {"Bash"})
        if not b:
            return "FALSE", "cited line has no Bash call"
        cmd = b.get("input", {}).get("command", "")
        stripped = strip_code(cmd, "sh")
        # branch-gated check on a command that cd's away = unverifiable branch
        if c.when_branch:
            cd_away = bool(re.search(r"(?:^|[\n;&|])\s*(?:cd|pushd)\s+\S", stripped)) \
                or "git -C" in stripped or "git --git-dir" in stripped
            if cd_away:
                return "FALSE", "command changes directory — recorded branch unreliable"
        # the forbidden token must survive shell string-stripping
        for f in c.forbid:
            if " " not in f and re.fullmatch(r"[\w.-]+", f):
                if re.search(r"(?<![\w.-])%s(?![\w.-])" % re.escape(f), stripped):
                    return "TRUE", "real `%s` command token" % f
            elif f in stripped:
                return "TRUE", "contains `%s`" % f
        if c.forbid_pattern and re.search(c.forbid_pattern, stripped):
            return "TRUE", "matches /%s/ outside quotes" % c.forbid_pattern
        # token only inside quotes -> false alarm
        return "FALSE", "forbidden token only appears inside a quoted string"

    if c.type == CONTENT:
        b = tool_at(raw_obj(path, v.line_no), {"Edit", "Write", "MultiEdit"})
        if not b:
            return "FALSE", "cited line has no Edit/Write call"
        inp = b.get("input", {})
        if b["name"] == "Edit":
            text = inp.get("new_string", "")
        elif b["name"] == "MultiEdit":
            text = "\n".join(e.get("new_string", "") for e in inp.get("edits", []))
        else:
            text = inp.get("content", "")
        ext = os.path.splitext(inp.get("file_path", ""))[1].lstrip(".")
        stripped = strip_code(text, ext)
        hit = any(f in stripped for f in c.forbid) or (
            c.forbid_pattern and re.search(c.forbid_pattern, stripped))
        return ("TRUE", "pattern present in stripped added code") if hit else (
            "FALSE", "pattern only in comment/string")

    if c.type == ORDERING:
        # independently scan the whole main path for a prior sight of the file
        accepted = {t.strip() for t in (c.require_before or "").split(",")}
        target = None
        b = tool_at(raw_obj(path, v.line_no), {"Edit", "Write", "MultiEdit"})
        if b:
            target = b.get("input", {}).get("file_path")
        seen_before = False
        for tc in transcript.tool_calls():
            if tc.turn >= v.turn:
                break
            if tc.name in accepted:
                p = tc.input.get("file_path")
                if not c.same_path or p == target:
                    seen_before = True
                    break
        return ("FALSE", "file WAS seen earlier via %s" % accepted) if seen_before else (
            "TRUE", "no prior Read/Write/Edit of %s" % (os.path.basename(target or "?")))

    return "UNSURE", "unknown type"


def main():
    repo = sys.argv[1]
    sessions = sys.argv[2:]
    doc = compile_rules(repo)
    check_by_id = {c.id: c for c in doc.checks}

    totals = {"TRUE": 0, "FALSE": 0, "UNSURE": 0}
    per_check = {}
    false_examples = []

    for spath in sessions:
        t = Transcript(spath)
        viols = execute(doc.checks, t)
        for v in viols:
            verdict, why = classify(check_by_id, t, spath, v)
            totals[verdict] += 1
            pc = per_check.setdefault(v.check_id, {"TRUE": 0, "FALSE": 0, "UNSURE": 0})
            pc[verdict] += 1
            if verdict == "FALSE" and len(false_examples) < 15:
                false_examples.append((os.path.basename(spath), v.turn, v.check_id, why, v.evidence[:90]))

    total = sum(totals.values())
    print("HAND-VALIDATION OF %d REPORTED VIOLATIONS" % total)
    print("  TRUE  (survives inspection): %d" % totals["TRUE"])
    print("  FALSE (does not):            %d" % totals["FALSE"])
    print("  UNSURE:                      %d" % totals["UNSURE"])
    fa = totals["FALSE"] / total * 100 if total else 0
    print("  FALSE-ALARM RATE: %.1f%%" % fa)
    print("\nPER CHECK:")
    for cid in sorted(per_check):
        pc = per_check[cid]
        rule = check_by_id[cid].rule
        print("  %-6s T=%-4d F=%-4d  %s" % (cid, pc["TRUE"], pc["FALSE"], rule))
    if false_examples:
        print("\nFALSE-ALARM EXAMPLES:")
        for ex in false_examples:
            print("  %s turn %s %s — %s\n      %s" % ex)


if __name__ == "__main__":
    main()
