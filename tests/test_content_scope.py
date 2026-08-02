"""Content checks must not fire on prose.

The class of bug this pins down: a content check compiled from a *code token*
applied to every file written, including Markdown. `strip_code` has no comment
or string syntax to blank in prose, so a token quoted in a sentence survived and
matched — writing "the tool never runs `eval()`" into SECURITY.md was reported
as a violation of "Never use `eval(`".

Guards asserted here:
  1. an unscoped content rule does not fire in prose files
  2. it still fires in code (no coverage lost)
  3. a rules file naming its own forbidden token never fires, even when the rule
     explicitly scopes to markdown
  4. an explicitly-scoped rule keeps the scope the user asked for
  5. the safety pack's destination-scoped secret check is unaffected

Standalone (no pytest), matching the other tests here.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mutalyze.checks import CONTENT, Check  # noqa: E402
from mutalyze.code_strip import is_prose_path  # noqa: E402
from mutalyze.execute import execute  # noqa: E402
from mutalyze.safety_pack import builtin_checks  # noqa: E402
from mutalyze.transcript import Transcript  # noqa: E402

FAILURES = []


def check(cond, label):
    if cond:
        return
    FAILURES.append(label)


def write_session(path, calls, repo):
    """Each call is (tool, input dict). Written as a simple main-path chain."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "uuid": "u0", "parentUuid": None,
                             "cwd": repo, "message": {"role": "user", "content": "go"}}) + "\n")
        parent = "u0"
        for i, (tool, inp) in enumerate(calls):
            uid = "a%d" % i
            obj = {
                "type": "assistant", "uuid": uid, "parentUuid": parent, "cwd": repo,
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "t%d" % i, "name": tool, "input": inp}]},
            }
            # Write content is only scored when the transcript shows this session
            # created the file — mirror that marker or Writes are silently ignored.
            if tool == "Write":
                obj["toolUseResult"] = {"type": "create", "filePath": inp["file_path"]}
            fh.write(json.dumps(obj) + "\n")
            parent = uid


def ids(violations):
    return sorted(v.check_id for v in violations)


def main():
    tmp = tempfile.mkdtemp(prefix="mutalyze_scope_")
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo, exist_ok=True)

    # the exact rule from this project that produced the false positive
    eval_rule = Check(
        id="CM001",
        rule="Never use `eval(` or `exec(` on transcript-derived data.",
        type=CONTENT,
        forbid=["eval(", "exec("],
    )

    # ---- 1 + 2: prose is skipped, code is not -----------------------------
    doc_text = "- **No execution of transcript data.** The tool never runs `eval()` or `exec()`.\n"
    code_text = "result = eval(user_supplied)\n"
    session = os.path.join(tmp, "s1.jsonl")
    write_session(session, [
        ("Write", {"file_path": os.path.join(repo, "SECURITY.md"), "content": doc_text}),
        ("Edit", {"file_path": os.path.join(repo, "docs/guide.rst"),
                  "old_string": "", "new_string": doc_text}),
        ("Edit", {"file_path": os.path.join(repo, "notes.txt"),
                  "old_string": "", "new_string": doc_text}),
        ("Edit", {"file_path": os.path.join(repo, "README"),
                  "old_string": "", "new_string": doc_text}),
        ("Edit", {"file_path": os.path.join(repo, "runner.py"),
                  "old_string": "", "new_string": code_text}),
    ], repo)
    res = execute([eval_rule], Transcript(session), repo_root=repo)

    prose_hits = [v for v in res.violations if v.evidence and (
        ".md" in v.evidence or ".rst" in v.evidence or ".txt" in v.evidence
        or "README" in v.evidence)]
    check(prose_hits == [],
          "no content violation in prose files (got %s)" % [v.evidence for v in prose_hits])
    code_hits = [v for v in res.violations if v.evidence and ".py" in v.evidence]
    check(len(code_hits) == 1,
          "the same rule still fires in real code (got %d)" % len(code_hits))

    # ---- 3: the rules file itself is never a violation -------------------
    rules_session = os.path.join(tmp, "s2.jsonl")
    write_session(rules_session, [
        ("Write", {"file_path": os.path.join(repo, "CLAUDE.md"),
                   "content": "- Never use `eval(` or `exec(` on transcript-derived data.\n"}),
        ("Edit", {"file_path": os.path.join(repo, "AGENTS.md"),
                  "old_string": "", "new_string": "- Never use `eval(`.\n"}),
    ], repo)
    res2 = execute([eval_rule], Transcript(rules_session), repo_root=repo)
    check(res2.violations == [], "writing the rule into CLAUDE.md/AGENTS.md is not a violation")

    # even when a rule explicitly scopes to markdown, the rules file is exempt
    md_rule = Check(id="CM002", rule="Never use `eval(` in docs.", type=CONTENT,
                    forbid=["eval("], applies_to=["*.md"])
    res3 = execute([md_rule], Transcript(rules_session), repo_root=repo)
    check(res3.violations == [],
          "the rules-file exemption holds even for an explicitly markdown-scoped rule")

    # ---- 4: an explicit scope is still honored ---------------------------
    md_session = os.path.join(tmp, "s3.jsonl")
    write_session(md_session, [
        ("Write", {"file_path": os.path.join(repo, "guide.md"), "content": "run `eval(` now\n"}),
    ], repo)
    res4 = execute([md_rule], Transcript(md_session), repo_root=repo)
    check(len(res4.violations) == 1,
          "a rule that explicitly targets *.md still fires there (user intent wins)")
    res5 = execute([eval_rule], Transcript(md_session), repo_root=repo)
    check(res5.violations == [], "the same file is skipped for the unscoped rule")

    # ---- 5: the safety pack is unaffected --------------------------------
    key = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n"
    secret_session = os.path.join(tmp, "s4.jsonl")
    write_session(secret_session, [
        ("Write", {"file_path": os.path.join(repo, "id_rsa"), "content": key}),
    ], repo)
    res6 = execute(builtin_checks(), Transcript(secret_session), repo_root=repo)
    check("SP004" in ids(res6.violations),
          "the destination-scoped secret check still fires (got %s)" % ids(res6.violations))

    # a secret written into prose is still not our business to flag as code,
    # but SP004 scopes by destination so this is governed by its own applies_to
    doc_secret = os.path.join(tmp, "s5.jsonl")
    write_session(doc_secret, [
        ("Write", {"file_path": os.path.join(repo, "SECURITY.md"), "content": key}),
    ], repo)
    res7 = execute(builtin_checks(), Transcript(doc_secret), repo_root=repo)
    check("SP004" not in ids(res7.violations),
          "a key quoted in docs is not a credential-file write")

    # ---- the predicate itself --------------------------------------------
    for p in ["/r/SECURITY.md", "/r/a.markdown", "/r/b.rst", "/r/c.txt", "/r/CHANGELOG",
              "/r/README", "/r/LICENSE", "/r/d.mdx", "/r/e.csv"]:
        check(is_prose_path(p), "is_prose_path treats %s as prose" % p)
    for p in ["/r/main.py", "/r/app.ts", "/r/Makefile", "/r/Dockerfile", "/r/x.go",
              "/r/conf.yaml", "/r/style.css", "/r/.env"]:
        check(not is_prose_path(p), "is_prose_path treats %s as code/config" % p)

    if FAILURES:
        sys.stdout.write("content scope FAIL\n")
        for f in FAILURES:
            sys.stdout.write("  - %s\n" % f)
        return 1
    sys.stdout.write(
        "content scope PASS (prose skipped · code still checked · rules file exempt · "
        "explicit scope honored · safety pack unaffected)\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
