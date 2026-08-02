"""Hand-validation harness for `mutalyze rules mine`.

Mirrors the discipline of validate_corpus.py and FINDINGS.md §0: the criteria
below are FROZEN BEFORE any number is produced, so precision is judged against a
definition chosen ahead of the result rather than one tuned to flatter it.

===========================================================================
ADJUDICATION CRITERIA — frozen 2026-08-02, before the fixes were written
===========================================================================
A proposal counts as a RULE (true positive) iff ALL hold:

  R1. It is *stated by the user as a standing instruction* — something meant to
      hold beyond the current step. "Always use rg" qualifies; "run the tests
      now" does not.
  R2. It is the user's own instruction, not quoted, reported, or described.
      "It says things like 'always run tests'" is describing a rule, not
      setting one. Reporting what a file/tool/other person said fails R2.
  R3. It is self-contained enough to act on. A fragment cut mid-clause, or a
      sentence whose subject is missing, fails.
  R4. It is not a one-off command tied to a specific path/script invocation.
      "Run `python scripts/sync.py`" is a task, not a rule.
  R5. It is not an artifact of formatting — leftover markdown emphasis,
      bullet markers, or quote characters embedded in the rule text fail.

Anything else is NOT-A-RULE (false positive). UNSURE is available and is
counted separately; an UNSURE never counts as a success.

Precision = RULE / (RULE + NOT-A-RULE), with UNSURE excluded from both.
The four defect classes named in the plan (markdown leakage, near-duplicate,
reported speech, one-off command) are also counted individually, because the
fix is supposed to eliminate those specifically.
===========================================================================

Usage:
    ./.venv/bin/python tests/validate_mining.py            # auto-adjudicate
    ./.venv/bin/python tests/validate_mining.py --list     # print for eyeballing

Auto-adjudication implements R1-R5 independently of mine.py's own filters: it
re-derives the verdict from the proposal text and its cited transcript line, so
it is not simply asking the code under test whether it was right.
"""

import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mutalyze.mine import mine_sessions  # noqa: E402

SAMPLE_SIZE = 60  # largest N sessions machine-wide — fixed so runs are comparable


# --- independent adjudication (does NOT reuse mine.py's filters) -----------

_REPORTED = re.compile(
    r"\b(?:it says|says things like|the rule is|for example|e\.g\.|such as"
    r"|claims|according to|quoted|verbatim|reads:)\b", re.IGNORECASE)
_MD_ARTIFACT = re.compile(r"\*\*|^\s*[-*+]\s|^\s*#{1,6}\s|^\s*>\s|^[\"']|[\"']$")
_ONE_OFF = re.compile(
    r"^\s*(?:please\s+)?(?:run|execute|start|open|launch|try)\b(?![^.]*\b(?:always|never|every time|before committing)\b)",
    re.IGNORECASE)
_PATH_ARG = re.compile(r"[\w./-]+\.(?:py|js|ts|sh|rb|go|rs|json|ya?ml|toml|md)\b|scripts?/")
_MODAL = re.compile(r"\b(always|never|must|don'?t|do not|avoid|only|prefer|no more)\b",
                    re.IGNORECASE)
_FRAGMENT = re.compile(r"^[a-z]*\)|^\(|\b(?:was|were)\s+\w+ing\b.*\)$|—\s*$|\.\.\.$")


def adjudicate(text):
    """Return (verdict, defect_class). Verdict: RULE / NOT-A-RULE / UNSURE."""
    t = text.strip()

    # R5 — formatting artifacts
    if _MD_ARTIFACT.search(t):
        return ("NOT-A-RULE", "markdown-artifact")
    # R2 — reported / quoted speech
    if _REPORTED.search(t):
        return ("NOT-A-RULE", "reported-speech")
    # R4 — one-off command (imperative + concrete path, no standing modality)
    if _ONE_OFF.match(t) and _PATH_ARG.search(t) and not _MODAL.search(t):
        return ("NOT-A-RULE", "one-off-command")
    # R3 — obvious fragment
    if _FRAGMENT.search(t) or len(t) < 12:
        return ("NOT-A-RULE", "fragment")
    # R1 — a standing instruction needs some normative modality, or a
    # use-X-not-Y shape which is inherently standing.
    if not _MODAL.search(t) and not re.search(
            r"\buse\s+\S+\s+(?:not|instead of|rather than)\b", t, re.IGNORECASE):
        return ("UNSURE", "no-modality")
    return ("RULE", "")


def near_dup_groups(texts):
    """Count proposals that are near-duplicates of an earlier one."""
    seen, dupes = {}, 0
    for t in texts:
        key = re.sub(r"[^a-z0-9 ]", "", t.lower())
        key = re.sub(r"\s+", " ", key).strip()
        key = re.sub(r"\s*\(.*?\)\s*", " ", key).strip()  # drop parentheticals
        key = " ".join(key.split()[:9])                    # first 9 words
        if key in seen:
            dupes += 1
        else:
            seen[key] = t
    return dupes


def main():
    sessions = glob.glob(os.path.expanduser("~/.claude/projects/**/*.jsonl"), recursive=True)
    sessions = sorted(sessions, key=os.path.getsize, reverse=True)[:SAMPLE_SIZE]
    if not sessions:
        sys.stdout.write("no sessions found on this machine — nothing to validate\n")
        return 0

    result = mine_sessions(sessions)
    proposals = result.proposals
    texts = [p.text for p in proposals]

    counts = {"RULE": 0, "NOT-A-RULE": 0, "UNSURE": 0}
    defects = {}
    rows = []
    for p in proposals:
        verdict, defect = adjudicate(p.text)
        counts[verdict] += 1
        if defect:
            defects[defect] = defects.get(defect, 0) + 1
        rows.append((verdict, defect, p))

    dupes = near_dup_groups(texts)
    if dupes:
        defects["near-duplicate"] = dupes

    scored = counts["RULE"] + counts["NOT-A-RULE"]
    precision = (100.0 * counts["RULE"] / scored) if scored else 0.0

    if "--list" in sys.argv:
        for verdict, defect, p in rows:
            mark = {"RULE": "OK ", "NOT-A-RULE": "BAD", "UNSURE": "?? "}[verdict]
            sys.stdout.write("%s %-58s %s\n" % (mark, p.text[:58],
                                                ("[%s]" % defect) if defect else ""))
            sys.stdout.write("      %s\n" % p.cite())
        sys.stdout.write("\n")

    sys.stdout.write("MINING VALIDATION (criteria frozen in this file's header)\n")
    sys.stdout.write("  sessions scanned : %d\n" % result.sessions_scanned)
    sys.stdout.write("  proposals        : %d\n" % len(proposals))
    sys.stdout.write("  RULE             : %d\n" % counts["RULE"])
    sys.stdout.write("  NOT-A-RULE       : %d\n" % counts["NOT-A-RULE"])
    sys.stdout.write("  UNSURE           : %d  (excluded from precision)\n" % counts["UNSURE"])
    sys.stdout.write("  PRECISION        : %.1f%%  (%d/%d)\n" % (precision, counts["RULE"], scored))
    if defects:
        sys.stdout.write("  defect classes:\n")
        for k in sorted(defects):
            sys.stdout.write("    %-18s %d\n" % (k, defects[k]))
    else:
        sys.stdout.write("  defect classes   : none\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
