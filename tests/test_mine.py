"""Mining tests: only the human's words, only rule-shaped ones, only unsaid ones.

Standalone (no pytest), matching the other tests here. Builds synthetic
transcripts so the assertions are about behavior, not about whatever happens to
be in the developer's ~/.claude directory.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mutalyze.mine import (  # noqa: E402
    known_rule_keys,
    mine_sessions,
    rule_candidates,
    user_messages,
)

FAILURES = []


def check(cond, label):
    if cond:
        return
    FAILURES.append(label)


def user_text(text):
    return {"type": "user", "uuid": "u", "message": {"role": "user",
            "content": [{"type": "text", "text": text}]}}


def user_str(text):
    return {"type": "user", "uuid": "u", "message": {"role": "user", "content": text}}


def assistant_text(text):
    return {"type": "assistant", "uuid": "a", "message": {"role": "assistant",
            "content": [{"type": "text", "text": text}]}}


def tool_result(text):
    return {"type": "user", "uuid": "t", "message": {"role": "user",
            "content": [{"type": "tool_result", "text": text}]}}


def write_session(path, objs):
    with open(path, "w", encoding="utf-8") as fh:
        for o in objs:
            fh.write(json.dumps(o) + "\n")


def main():
    tmp = tempfile.mkdtemp(prefix="mutalyze_mine_")

    # ---- sentence-level filtering ------------------------------------------
    keep = {
        "always use rg not grep when searching": "`rg`",
        "never commit directly to main": "main",
        "we should always run pytest before pushing": "`pytest`",
        "always read a file before editing it": "before",
    }
    for text, needle in keep.items():
        got = rule_candidates(text)
        check(len(got) == 1 and needle in got[0],
              "kept a real instruction: %r -> %r" % (text, got))

    drop = [
        "don't try to make unsupported rules work",   # `make` is not a build-tool rule here
        "that looks terrible, can you fix it?",
        "can you move the install to the top?",
        "you said earlier that we should always use rg",  # about the conversation
        "thanks, that always works",
        "ok",
    ]
    for text in drop:
        got = rule_candidates(text)
        check(got == [], "dropped non-rule chat: %r -> %r" % (text, got))

    # a sentence the user already formatted is not rewritten
    formatted = rule_candidates("always use `rg` (not `grep`)")
    check(formatted and formatted[0].count("`") == 4,
          "an already-backticked sentence is left alone: %r" % (formatted,))

    # ---- reading only the human half --------------------------------------
    s1 = os.path.join(tmp, "sess1.jsonl")
    write_session(s1, [
        user_text("always use rg not grep when searching"),
        assistant_text("Understood, I will never use grep from now on."),
        tool_result("never use grep in this repo"),
        user_text("<system-reminder>always use tabs</system-reminder> looks good"),
        user_str("also never commit directly to main"),
    ])

    said = list(user_messages(s1))
    joined = " || ".join(s.text for s in said)
    check("always use rg not grep" in joined, "reads a plain user text block")
    check("also never commit directly to main" in joined, "reads a string-valued user message")
    check("Understood" not in joined, "assistant turns are excluded")
    check("never use grep in this repo" not in joined, "tool_result blocks are excluded")
    check("always use tabs" not in joined, "system-reminder content is stripped")

    # ---- proposals, citations, dedupe vs known ----------------------------
    result = mine_sessions([s1])
    texts = [p.text for p in result.proposals]
    check(result.sessions_scanned == 1, "counts sessions scanned")
    check(any("`rg`" in t for t in texts), "proposes the rg rule from chat")
    check(any("main" in t for t in texts), "proposes the branch rule from chat")
    check(all("tabs" not in t for t in texts), "never proposes injected text")
    for p in result.proposals:
        check(p.cite() != "", "every proposal carries a citation")
        check(p.citations[0][0] == "sess1.jsonl", "citation names the session file")

    # checkability is reported honestly
    rg_prop = [p for p in result.proposals if "`rg`" in p.text][0]
    check(rg_prop.checkable and rg_prop.check_type == "command",
          "the rg rule is reported as a checkable command rule")

    # ---- repetition across sessions is counted ---------------------------
    s2 = os.path.join(tmp, "sess2.jsonl")
    write_session(s2, [user_text("always use rg not grep when searching")])
    multi = mine_sessions([s1, s2])
    rg_multi = [p for p in multi.proposals if "`rg`" in p.text][0]
    check(rg_multi.count == 2, "restating a rule increments its count (got %d)" % rg_multi.count)
    check(len(rg_multi.sessions) == 2, "tracks both sessions it was said in")
    check(multi.proposals[0].count >= multi.proposals[-1].count,
          "proposals are ranked with the most-repeated first")

    # ---- already-written rules are not re-proposed -----------------------
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo, exist_ok=True)
    with open(os.path.join(repo, "CLAUDE.md"), "w", encoding="utf-8") as fh:
        fh.write("# Rules\n\n- always use `rg` not `grep` when searching\n")
    known = known_rule_keys(repo)
    check(len(known) == 1, "known_rule_keys reads the repo rules file")

    filtered = mine_sessions([s1, s2], known=known)
    check(all("`rg`" not in p.text for p in filtered.proposals),
          "a rule already in the rules file is not re-proposed")
    check(filtered.already_known >= 2, "counts the candidates it dropped as known")
    check(any("main" in p.text for p in filtered.proposals),
          "the unsaid rule is still proposed")

    # store-side dedupe uses the same path
    known2 = known_rule_keys(repo, ["never commit directly to main"])
    both = mine_sessions([s1, s2], known=known2)
    check(both.proposals == [], "rules covered by file+store leave nothing to propose")

    # ---- an empty/irrelevant session is handled --------------------------
    s3 = os.path.join(tmp, "sess3.jsonl")
    write_session(s3, [user_text("hey what does this repo do?"), assistant_text("It audits rules.")])
    empty = mine_sessions([s3])
    check(empty.proposals == [], "a session with no instructions proposes nothing")
    check(mine_sessions([os.path.join(tmp, "nope.jsonl")]).sessions_scanned == 0,
          "a missing session file is skipped, not fatal")

    if FAILURES:
        sys.stdout.write("mining FAIL\n")
        for f in FAILURES:
            sys.stdout.write("  - %s\n" % f)
        return 1
    sys.stdout.write(
        "mining PASS (human-only extraction · injected text stripped · precision "
        "filters · citations · repetition counts · dedupe vs file+store)\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
