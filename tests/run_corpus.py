"""Run ruleguard over a set of real sessions and, for every reported
violation, print the raw transcript line at the cited line so each one can be
hand-validated. Prints Part-6 metrics per session and in aggregate."""

from __future__ import annotations

import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ruleguard.compile_rules import compile_rules  # noqa: E402
from ruleguard.execute import execute  # noqa: E402
from ruleguard.transcript import Transcript  # noqa: E402


def raw_line(path: str, line_no: int) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for i, ln in enumerate(fh, 1):
            if i == line_no:
                return ln
    return ""


def evidence_from_line(path: str, line_no: int, check_id: str) -> str:
    """Re-extract the actual tool call at the cited line, independent of the
    executor, to confirm the citation is real."""
    ln = raw_line(path, line_no)
    try:
        o = json.loads(ln)
    except Exception:
        return "(could not parse cited line)"
    if o.get("type") != "assistant":
        return "(cited line is not an assistant turn: type=%s)" % o.get("type")
    out = []
    for b in (o.get("message", {}) or {}).get("content", []) or []:
        if isinstance(b, dict) and b.get("type") == "tool_use":
            inp = b.get("input", {})
            if b.get("name") == "Bash":
                out.append("Bash: " + (inp.get("command", "")[:120]))
            elif b.get("name") in ("Edit", "Write", "MultiEdit"):
                fp = os.path.basename(inp.get("file_path", "") or "")
                out.append("%s: %s" % (b.get("name"), fp))
    return " | ".join(out) if out else "(no tool_use on cited line)"


def main():
    repo = sys.argv[1]
    sessions = sys.argv[2:]
    doc = compile_rules(repo)
    print("COMPILED %d checks from %s:" % (len(doc.checks), doc.source))
    for c in doc.checks:
        print("  %-6s %-9s %s" % (c.id, c.type, c.rule))
    print("UNSUPPORTED: %d" % len(doc.unsupported))
    for u in doc.unsupported:
        print("   -", u["rule"])
    print("=" * 78)

    agg_turns = 0
    agg_viol = 0
    for spath in sessions:
        t = Transcript(spath)
        viols = execute(doc.checks, t)
        agg_turns += t.stats.turns
        agg_viol += len(viols)
        print("\nSESSION %s" % os.path.basename(spath))
        print("  turns(main-path)=%d  tool_calls=%d  total_lines=%d  side_branch=%d"
              % (t.stats.turns, t.stats.tool_calls, t.stats.total_lines, t.stats.side_branch_lines))
        print("  reported violations: %d  (%.2f per 100 turns)"
              % (len(viols), (len(viols) / t.stats.turns * 100) if t.stats.turns else 0))
        by_check = {}
        for v in viols:
            by_check.setdefault(v.check_id, 0)
            by_check[v.check_id] += 1
        if by_check:
            print("  by check:", by_check)
        # print each violation with the raw re-extracted evidence for hand-check
        for v in viols[:40]:
            print("    turn %-6d line %-6d %-6s | %s" % (v.turn, v.line_no, v.check_id, v.evidence))
            print("         cited-line re-extract: %s" % evidence_from_line(spath, v.line_no, v.check_id))
        if len(viols) > 40:
            print("    (+%d more)" % (len(viols) - 40))

    print("\n" + "=" * 78)
    print("AGGREGATE: %d sessions  %d turns  %d reported violations  %.2f per 100 turns"
          % (len(sessions), agg_turns, agg_viol, (agg_viol / agg_turns * 100) if agg_turns else 0))


if __name__ == "__main__":
    main()
