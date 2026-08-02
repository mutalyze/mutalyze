"""Context/re-injection tests: relevance ranks, budget trims, nothing is lost silently.

Standalone (no pytest). Builds a synthetic repo and transcript so the assertions
are about ranking behavior rather than whatever this machine happens to contain.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mutalyze.context import (  # noqa: E402
    build_reminder,
    count_compactions,
    gather_rules,
    recent_activity,
    score_rules,
)

FAILURES = []


def check(cond, label):
    if cond:
        return
    FAILURES.append(label)


RULES = """# Rules

- Use `rg` (not `grep`) for searching the codebase.
- Never use `console.log` in TypeScript.
- Never commit directly to `main` — branch first.
- Read a file before editing it.
- Always write beautiful and elegant code.
"""


def call(tool, inp, uuid, parent):
    return {"type": "assistant", "uuid": uuid, "parentUuid": parent,
            "cwd": "/tmp", "gitBranch": "feature",
            "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "id": "t", "name": tool, "input": inp}]}}


def main():
    tmp = tempfile.mkdtemp(prefix="mutalyze_ctx_")
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo, exist_ok=True)
    with open(os.path.join(repo, "CLAUDE.md"), "w", encoding="utf-8") as fh:
        fh.write(RULES)
    # keep the store out of it: these tests are about the rules file
    os.environ["MUTALYZE_STORE"] = os.path.join(tmp, "store.yaml")

    # ---- gathering --------------------------------------------------------
    entries = gather_rules(repo, include_store=False)
    texts = [e.text for e in entries]
    check(len(entries) == 5, "gathers every rule, checkable or not (got %d)" % len(entries))
    check(any("beautiful" in t for t in texts),
          "an unsupported rule is still available to re-assert")
    check(sum(1 for e in entries if e.check is not None) == 4,
          "compiled scope metadata is attached where the rule compiles")

    # ---- relevance from a prompt ------------------------------------------
    scored = score_rules(entries, prompt="let me grep the codebase for TODOs")
    check("`rg`" in scored[0].text,
          "a grep-ish prompt ranks the rg rule first (got %r)" % scored[0].text[:40])
    check(scored[0].score > 0 and scored[0].reasons, "the top rule explains itself")

    scored_ts = score_rules(entries, prompt="add a banner to src/app.ts")
    check("console.log" in scored_ts[0].text,
          "a .ts prompt ranks the TypeScript rule first (got %r)" % scored_ts[0].text[:40])

    scored_git = score_rules(entries, prompt="please commit this to main")
    check("main" in scored_git[0].text,
          "a commit-to-main prompt ranks the branch rule first")

    # an unrelated prompt must not invent relevance
    calm = score_rules(entries, prompt="what time is it")
    check(all(s.score == 0 for s in calm), "an unrelated prompt scores nothing")

    # ---- concept relevance: the words people actually use ----------------
    # Ranking used to be literal-only, so "search" never reached a rule whose
    # token is "grep". Each pair below names no rule token at all.
    concept_cases = [
        ("search the codebase for TODOs", "`rg`"),
        ("look for every call site", "`rg`"),
        ("commit this work", "main"),
    ]
    for prompt, needle in concept_cases:
        ranked = score_rules(entries, prompt=prompt)
        check(needle in ranked[0].text,
              "concept prompt %r ranks the right rule first (got %r)"
              % (prompt, ranked[0].text[:44]))
        check(ranked[0].score > 0 and ranked[0].reasons,
              "the concept hit explains itself for %r" % prompt)

    # A language name lifts the rule for that language. Not asserted as rank 1:
    # "read before editing" also fires here, and when you are editing that rule
    # is genuinely relevant too — the goal is surfacing, not a fixed order.
    ts = {s.text: s for s in score_rules(entries, prompt="add a banner while editing the typescript")}
    ts_rule = [s for t, s in ts.items() if "console.log" in t][0]
    check(ts_rule.score > 0 and any("typescript" in r for r in ts_rule.reasons),
          "naming a language lifts that language's rule (%r)" % ts_rule.reasons)

    # a literal mention must still outrank a merely conceptual one
    literal = score_rules(entries, prompt="stop using grep")
    concept = score_rules(entries, prompt="stop searching that way")
    lit_rg = [s for s in literal if "`rg`" in s.text][0]
    con_rg = [s for s in concept if "`rg`" in s.text][0]
    check(lit_rg.score > con_rg.score,
          "an exact mention scores above a concept match (%d vs %d)"
          % (lit_rg.score, con_rg.score))

    # Concepts must not fire on a word that merely *contains* a concept word:
    # "blog post" scored a logging rule as relevant before word-boundary matching.
    noise = score_rules(entries, prompt="write the release announcement blog post")
    for s in noise:
        check(not any("rule governs" in r for r in s.reasons),
              "no concept match on 'blog post' (%r fired %r)" % (s.text[:32], s.reasons))

    # ---- relevance from session activity ---------------------------------
    session = os.path.join(tmp, "sess.jsonl")
    with open(session, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "uuid": "u1", "parentUuid": None,
                             "message": {"role": "user", "content": "go"}}) + "\n")
        fh.write(json.dumps(call("Edit", {"file_path": "/tmp/repo/src/app.ts"}, "a1", "u1")) + "\n")
        fh.write(json.dumps(call("Bash", {"command": "grep -rn TODO src/"}, "a2", "a1")) + "\n")

    activity = recent_activity(session)
    check("ts" in activity.extensions, "picks up edited file extensions")
    check("grep" in activity.commands, "picks up recent commands")
    check("Edit" in activity.tools, "picks up which tools were used")

    # assert on scores, not positions: equal-scoring rules tie-break by text,
    # which is stable but arbitrary and not the behavior under test.
    by_activity = {s.text: s for s in score_rules(entries, activity=activity)}
    ts_rule = [s for t, s in by_activity.items() if "console.log" in t][0]
    check(ts_rule.score > 0 and any("ts" in r for r in ts_rule.reasons),
          "editing .ts lifts the TypeScript rule (%r)" % ts_rule.reasons)
    rg_rule = [s for t, s in by_activity.items() if "`rg`" in t][0]
    check(rg_rule.score > 0 and any("grep" in r for r in rg_rule.reasons),
          "a recent grep lifts the rg rule (%r)" % rg_rule.reasons)
    ordering = [s for t, s in by_activity.items() if "before editing" in t][0]
    check(ordering.score > 0 and any("Edit" in r for r in ordering.reasons),
          "an ordering rule fires on its trigger tool being used")
    idle = [s for t, s in by_activity.items() if "beautiful" in t][0]
    check(idle.score == 0, "a rule with no bearing on current activity stays at zero")

    # ---- budget: trims, and says so --------------------------------------
    trimmed = build_reminder(repo, session=None, prompt="grep for TODOs",
                             max_rules=2, include_store=False)
    check(len(trimmed.rules) == 2, "--max caps the rule count")
    check(trimmed.dropped == 3, "the trimmed remainder is counted (got %d)" % trimmed.dropped)
    check("not shown" in trimmed.as_markdown(),
          "the markdown says rules were withheld — never a silent cut")

    everything = build_reminder(repo, session=None, max_rules=0, include_store=False)
    check(len(everything.rules) == 5 and everything.dropped == 0,
          "--max 0 re-asserts the whole rulebook (the post-compaction case)")

    # ---- output shapes ---------------------------------------------------
    md = everything.as_markdown()
    check(md.startswith("# Project rules still in force"), "markdown has a title")
    check(md.count("\n- ") == 5, "every rule is a markdown bullet")

    parsed = json.loads(everything.as_json())
    check(len(parsed["rules"]) == 5, "json form lists the rules")
    check("origin" in parsed["rules"][0], "json form says where each rule came from")

    envelope = json.loads(everything.as_claude_hook(event="PreCompact"))
    check(envelope["hookSpecificOutput"]["hookEventName"] == "PreCompact",
          "hook envelope carries the event name")
    check("Project rules" in envelope["hookSpecificOutput"]["additionalContext"],
          "hook envelope carries the rules as context")

    # ---- compaction detection -------------------------------------------
    check(count_compactions(session) == 0, "a clean session reports no compactions")
    compacted = os.path.join(tmp, "compacted.jsonl")
    with open(compacted, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "uuid": "u1", "parentUuid": None,
                             "message": {"role": "user", "content": "hi"}}) + "\n")
        fh.write(json.dumps({"type": "system", "subtype": "compact_boundary",
                             "uuid": "c1", "parentUuid": None,
                             "logicalParentUuid": "u1"}) + "\n")
    check(count_compactions(compacted) == 1, "a compaction boundary is detected")
    check(count_compactions(os.path.join(tmp, "missing.jsonl")) == 0,
          "a missing transcript is not fatal")

    # ---- no rules at all -------------------------------------------------
    bare = os.path.join(tmp, "bare")
    os.makedirs(bare, exist_ok=True)
    empty = build_reminder(bare, session=None, max_rules=0, include_store=False)
    check(empty.rules == [], "a repo with no rules file yields nothing to re-assert")

    if FAILURES:
        sys.stdout.write("context FAIL\n")
        for f in FAILURES:
            sys.stdout.write("  - %s\n" % f)
        return 1
    sys.stdout.write(
        "context PASS (prompt relevance · activity relevance · ordering triggers · "
        "budget trim reported · md/json/hook shapes · compaction detection)\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
