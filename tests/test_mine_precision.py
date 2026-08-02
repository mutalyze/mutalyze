"""One test per mining defect observed on real machine-wide data.

These are not hypotheticals. Each fixture below is a real string (or a faithful
reduction of one) that `rules mine` proposed as a rule when the corpus was
widened from one repo to the whole machine, taking measured precision to 70%.
Pinning them here means a future filter change cannot quietly bring them back.

Also guards the other direction — the legitimate rules that must survive every
filter — because the cheap way to make precision look good is to stop proposing
anything.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mutalyze.discover import all_sessions  # noqa: E402
from mutalyze.mine import (  # noqa: E402
    _strip_markdown,
    mine_key,
    mine_sessions,
    rule_candidates,
)

FAILURES = []


def check(cond, label):
    if cond:
        return
    FAILURES.append(label)


def main():
    # ---- defect 1: markdown emphasis leaking into the rule text ----------
    md_cases = [
        '**use-X-not-Y** ("Use `pytest`, not `unittest`" forbade the wrong one)',
        "**CRITICAL:** always use `rg` for searching",
        "## Always use `rg` not `grep`",
        "> never commit directly to main",
    ]
    for raw in md_cases:
        got = rule_candidates(raw)
        for g in got:
            check("**" not in g and not g.startswith("#") and not g.startswith(">"),
                  "markdown stripped from proposal: %r -> %r" % (raw[:34], g))
    check(_strip_markdown("**bold** and *em* and __also__") == "bold and em and also",
          "_strip_markdown removes emphasis but keeps words")
    check("`rg`" in _strip_markdown("always use `rg`"),
          "_strip_markdown preserves backticks (the classifier needs them)")

    # ---- defect 2: reported speech mined as an instruction ---------------
    reported = [
        'It says things like "always run the tests before finishing"',
        "The rule is that you must never commit to main",
        "According to the docs, always use `rg` not `grep`",
        "For example, never use `eval(` on user data",
        "the docs says to always run `pytest` first",
    ]
    for raw in reported:
        check(rule_candidates(raw) == [],
              "reported speech is not mined: %r -> %r" % (raw[:40], rule_candidates(raw)))

    # ---- defect 3: one-off commands mined as standing rules -------------
    one_offs = [
        "Run `python` scripts/spotify_sync.py",
        "run scripts/build.sh now",
        "please execute src/migrate.py",
        "open tests/test_watch.py",
    ]
    for raw in one_offs:
        check(rule_candidates(raw) == [],
              "one-off task is not mined as a rule: %r -> %r" % (raw[:38], rule_candidates(raw)))

    # ...but the same verb WITH standing modality is a real rule
    standing = rule_candidates("always run `pytest` before committing")
    check(len(standing) == 1,
          "an imperative with standing modality IS a rule: %r" % (standing,))

    # ---- defect 4: near-duplicate restatements not collapsing -----------
    a = '**use-X-not-Y** ("Use `pytest`, not `unittest`" forbade the wrong one)'
    b = 'use-X-not-Y ("Use `pytest`, not `unittest`" — was flagging correct runs)'
    check(mine_key(a) == mine_key(b),
          "two renderings of one rule share a mine_key\n      %r\n      %r"
          % (mine_key(a), mine_key(b)))
    check(mine_key("never commit directly to main") != mine_key("never push directly to main"),
          "genuinely different rules keep different keys")
    check(mine_key("Use `rg`, not `grep`.") == mine_key("use rg not grep"),
          "formatting and punctuation do not split one rule into two")

    # ---- the other direction: real rules must still survive -------------
    keepers = {
        "always use rg not grep when searching": "`rg`",
        "never commit directly to main": "main",
        "always run `pytest` before pushing": "`pytest`",
        "never use a bare `except:` in python": "except",
        "always read a file before editing it": "before",
    }
    for raw, needle in keepers.items():
        got = rule_candidates(raw)
        check(len(got) == 1 and needle in got[0],
              "genuine rule still mined: %r -> %r" % (raw[:38], got))

    # unbalanced backtick means the sentence was cut mid-token
    check(rule_candidates("always use `rg for searching the codebase") == [],
          "a sentence cut mid-backtick is not proposed as a rule")

    # ---- defect 5: label prefixes (seen in the customer run) -------------
    # `rule: "Never commit directly to `main`` reached the review list with the
    # label and quote attached. Stripping them recovers the real rule, which is
    # better than discarding the line — a quote-balance guard was tried and cost
    # three genuine rules for no precision gain, so cleaning wins over rejecting.
    recovered = rule_candidates('rule: "Never commit directly to `main`')
    check(len(recovered) == 1 and recovered[0].lower().startswith("never"),
          "a labelled/quoted fragment is cleaned into the rule it states: %r" % (recovered,))
    check(all('"' not in r and not r.lower().startswith("rule") for r in recovered),
          "the recovered rule carries no label or stray quote")
    labelled = rule_candidates('rule: never commit directly to main')
    check(len(labelled) == 1 and labelled[0].lower().startswith("never"),
          "a label prefix is stripped, keeping the rule itself: %r" % (labelled,))
    for prefix in ("constraint:", "note:", "policy —", "guideline -"):
        got = rule_candidates("%s always use `rg` not `grep`" % prefix)
        check(got and not got[0].lower().startswith(prefix.split()[0].rstrip(":—-")),
              "prefix %r stripped from %r" % (prefix, got))

    # ---- corpus widening actually reaches more sessions -----------------
    every = all_sessions()
    if every:
        check(all(p.endswith(".jsonl") for p in every), "all_sessions returns transcripts")
        check(len(every) == len(set(every)), "all_sessions does not return duplicates")
        # newest first
        mtimes = [os.path.getmtime(p) for p in every[:20] if os.path.exists(p)]
        check(mtimes == sorted(mtimes, reverse=True), "all_sessions is newest-first")

    # ---- end-to-end: proposals from a synthetic pair of sessions --------
    tmp = tempfile.mkdtemp(prefix="mutalyze_minprec_")
    import json

    def write(path, texts):
        with open(path, "w", encoding="utf-8") as fh:
            for t in texts:
                fh.write(json.dumps({"type": "user", "uuid": "u", "message": {
                    "role": "user", "content": [{"type": "text", "text": t}]}}) + "\n")

    s1 = os.path.join(tmp, "a.jsonl")
    s2 = os.path.join(tmp, "b.jsonl")
    write(s1, ["**always use `rg` not `grep`**", "It says things like always use tabs",
               "run scripts/deploy.sh"])
    write(s2, ["always use `rg`, not `grep`!"])   # a restatement of the same rule
    res = mine_sessions([s1, s2])
    check(len(res.proposals) == 1,
          "noise dropped and the restatement merged: got %r"
          % [p.text for p in res.proposals])
    if res.proposals:
        check(res.proposals[0].count == 2,
              "the merged rule counts both statements (got %d)" % res.proposals[0].count)
        check("**" not in res.proposals[0].text, "stored text carries no markdown")

    if FAILURES:
        sys.stdout.write("mining precision FAIL\n")
        for f in FAILURES:
            sys.stdout.write("  - %s\n" % f)
        return 1
    sys.stdout.write(
        "mining precision PASS (markdown stripped · reported speech rejected · "
        "one-off tasks rejected · near-duplicates merged · real rules survive)\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
