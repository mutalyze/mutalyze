"""Parser-robustness sweep over the longest local Claude Code sessions.

This is a LOCAL diagnostic, not a unit test: it reads your own transcripts under
~/.claude/projects and reports only counts, never any transcript content. It
answers one question — does the parser survive long, compaction-heavy sessions,
the shape that once dropped 95% of a session before the logical-parent bridge?

For each of the N longest sessions it checks three things and prints PASS/FAIL:
  1. parses without raising,
  2. main-path turn numbering is contiguous 1..T, every tool call's turn in
     range and its seq strictly increasing (no gaps, no duplicates),
  3. coverage is retained across compactions — it re-runs a NAIVE walk that
     follows parentUuid only (no logicalParentUuid bridge) and reports how many
     turns that naive walk would have kept vs. the bridged parser. On a compacted
     session the naive number is the pre-fix regression; the bridged number is
     what mutalyze actually keeps.

Run:  python tests/sweep_parser_robustness.py
"""
import glob
import json
import os
import sys
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from mutalyze.transcript import Transcript  # noqa: E402

PROJECTS = os.path.expanduser("~/.claude/projects")
TOPN = int(os.environ.get("SWEEP_TOPN", "20"))


def line_count(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return sum(1 for _ in fh)


def naive_turns(path):
    """Main-path length following parentUuid ONLY (no compaction bridge).

    Mirrors Transcript's walk exactly, minus the logical_parent fallback, so the
    two counts are apples-to-apples. This is the pre-fix behaviour.
    """
    nodes, by_uuid, compactions = [], {}, 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for ln, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                o = json.loads(raw)
            except Exception:
                continue
            if not isinstance(o, dict):
                continue
            if o.get("logicalParentUuid") and not o.get("parentUuid"):
                compactions += 1
            n = {"uuid": o.get("uuid"), "parent": o.get("parentUuid"),
                 "etype": o.get("type"), "sc": bool(o.get("isSidechain"))}
            nodes.append(n)
            if n["uuid"]:
                by_uuid[n["uuid"]] = n
    leaf = None
    for n in reversed(nodes):
        if not n["sc"] and n["etype"] in ("user", "assistant") and n["uuid"]:
            leaf = n
            break
    seen, count, cur = set(), 0, leaf
    while cur is not None and cur["uuid"] and cur["uuid"] not in seen:
        seen.add(cur["uuid"])
        count += 1
        cur = by_uuid.get(cur["parent"]) if cur["parent"] else None  # no bridge
    return count, compactions


allf = glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))
if not allf:
    print("no transcripts under", PROJECTS)
    sys.exit(0)
top = sorted(((line_count(p), p) for p in allf), reverse=True)[:TOPN]
ndirs = len({os.path.dirname(p) for p in allf})

print("scanned %d transcripts across %d project dirs; parsing the %d longest"
      % (len(allf), ndirs, len(top)))
print("(sessions anonymized as S01..; no transcript content is read)\n")
hdr = "%-5s %8s %7s %7s %7s %7s  %s"
print(hdr % ("id", "lines", "cmpct", "naive", "kept", "tools", "status"))
print("-" * 68)

fails = 0
maxlines = 0
for i, (lines, p) in enumerate(top, start=1):
    sid = "S%02d" % i
    maxlines = max(maxlines, lines)
    try:
        t = Transcript(p)
        kept = t.stats.turns
        naive, cmp_n = naive_turns(p)
        bad = seq_prev = 0
        seqs = []
        for c in t.tool_calls():
            seqs.append(c.seq)
            if not (1 <= c.turn <= kept):
                bad += 1
        tools = len(seqs)
        seq_ok = seqs == list(range(len(seqs)))  # 0..n-1, strict, no dupes/gaps
        # coverage: on a compacted session the bridge must recover far more than
        # the naive walk would (else the pre-fix drop is back).
        cov_ok = True if cmp_n == 0 else kept > naive
        status = "PASS"
        if bad or not seq_ok:
            status = "FAIL numbering (bad=%d seq_ok=%s)" % (bad, seq_ok)
            fails += 1
        elif not cov_ok:
            status = "FAIL coverage (kept<=naive on compacted)"
            fails += 1
        elif cmp_n and kept > naive:
            status = "PASS  (+%d turns recovered by bridge)" % (kept - naive)
        print(hdr % (sid, lines, cmp_n, naive, kept, tools, status))
    except Exception as e:
        fails += 1
        print(hdr % (sid, lines, "-", "-", "-", "-", "CRASH: %r" % e))
        traceback.print_exc()

print("-" * 68)
print("summary: %d sessions, longest %d lines, %d FAIL"
      % (len(top), maxlines, fails))
print("cmpct=compaction boundaries; naive=turns a parentUuid-only walk keeps;")
print("kept=turns the bridged parser keeps. On compacted rows kept>naive is the")
print("fix working — the gap is the drop that the pre-bridge parser suffered.")
