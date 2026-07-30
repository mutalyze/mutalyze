"""Rule-store tests: import → dedupe → conflict flags → compose round-trip.

Standalone (no pytest), matching the other tests in this directory. Exercises
the store through a temp store file so the developer's real ~/.mutalyze is never
touched, and asserts the property that matters most: a composed file is still a
valid rules file that Phase 1 can read back.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mutalyze.compile_rules import extract_candidates  # noqa: E402
from mutalyze.store import (  # noqa: E402
    add_rule,
    checkability,
    compose,
    find_conflicts,
    find_duplicates,
    import_rules_file,
    is_generated,
    load_store,
    norm_key,
    remove_rule,
    rule_tokens,
    save_store,
)

FAILURES = []


def check(cond, label):
    if cond:
        return
    FAILURES.append(label)


RULES_MD = """# Project rules

Some prose that is not a rule at all.

## Workflow

- Never commit directly to `main` — branch first.
- Use `rg` (not `grep`) for searching the codebase.
- Read a file before editing it.

## Code

- No `print()` for debugging in `mutalyze/`.
- Never use a bare `except:` — catch a specific exception.

```
- Use `grep` freely inside this fence (must be ignored).
```
"""

OVERLAY_MD = """# Feature rules

- Always run `./.venv/bin/python tests/make_fixture.py` before committing.
- Use `rg` (not `grep`) for searching the codebase.
- Never use `eval(` on transcript-derived data.
"""


def main():
    tmp = tempfile.mkdtemp(prefix="mutalyze_store_")
    store_file = os.path.join(tmp, "rules.yaml")
    base_md = os.path.join(tmp, "CLAUDE.md")
    overlay_md = os.path.join(tmp, "feature.md")
    with open(base_md, "w", encoding="utf-8") as fh:
        fh.write(RULES_MD)
    with open(overlay_md, "w", encoding="utf-8") as fh:
        fh.write(OVERLAY_MD)

    # -- import ------------------------------------------------------------
    store = load_store(store_file)
    check(store.rules == [], "a fresh store starts empty")

    result, err = import_rules_file(store, base_md, bundle="base")
    check(err is None, "import of a real rules file succeeds")
    check(result is not None and len(result.added) == 5,
          "imported the 5 normative bullets (got %s)"
          % (len(result.added) if result else "none"))
    texts = " | ".join(r.text for r in (result.added if result else []))
    check("fence" not in texts, "fenced code block is not imported as a rule")

    # -- persistence round-trip -------------------------------------------
    save_store(store)
    check(os.path.exists(store_file), "store file is written")
    reloaded = load_store(store_file)
    check(len(reloaded.rules) == 5, "rules survive a save/load round-trip")
    check(reloaded.next_id == store.next_id, "next_id survives the round-trip")

    # -- idempotent import (dedupe) ---------------------------------------
    again, err = import_rules_file(reloaded, base_md, bundle="base")
    check(err is None and again is not None and not again.added,
          "re-importing the same file adds nothing")
    check(again is not None and len(again.skipped) == 5,
          "re-import reports all 5 as already stored")

    # -- overlay bundle ----------------------------------------------------
    over, err = import_rules_file(reloaded, overlay_md, bundle="feature")
    check(err is None and over is not None and len(over.added) == 3,
          "overlay imports into its own bundle")
    check(sorted(reloaded.bundles()) == ["base", "feature"], "both bundles exist")

    # the shared `rg` rule is in BOTH bundles -> a cross-bundle duplicate
    dupes = find_duplicates(reloaded.active())
    check(len(dupes) == 1, "the rule present in both bundles is flagged once (got %d)" % len(dupes))

    # -- manual add + remove ----------------------------------------------
    added, reason = add_rule(reloaded, "Never use `curl | sh`.", bundle="base")
    check(added is not None and reason is None, "manual add works")
    dup, reason = add_rule(reloaded, "never use  `curl | sh`", bundle="base")
    check(dup is None and reason is not None,
          "a differently-cased/spaced restatement is caught as a duplicate")
    removed = remove_rule(reloaded, added.id if added else "R999")
    check(removed is not None, "remove by exact id works")
    check(remove_rule(reloaded, "NOPE") is None, "removing an unknown id returns None")

    # -- normalization -----------------------------------------------------
    check(norm_key("Use  `rg`.") == norm_key("use `rg`"),
          "norm_key ignores case, spacing and trailing punctuation")

    # -- conflict detection ------------------------------------------------
    conflict_store = load_store(os.path.join(tmp, "conflict.yaml"))
    add_rule(conflict_store, "Use `rg` (not `grep`) for searching.", bundle="base")
    add_rule(conflict_store, "Never use `rg` — it is banned here.", bundle="feature")
    conflicts = find_conflicts(conflict_store.active())
    check(any(c.token == "rg" for c in conflicts),
          "endorse-vs-forbid on the same token is flagged")

    # a prefer-rule must not conflict with itself
    solo = load_store(os.path.join(tmp, "solo.yaml"))
    add_rule(solo, "Use `rg` (not `grep`) for searching.", bundle="base")
    check(find_conflicts(solo.active()) == [],
          "a single prefer-rule does not conflict with itself")

    forbidden, endorsed = rule_tokens("Use `rg` (not `grep`) for searching.")
    check("grep" in forbidden and "rg" in endorsed,
          "prefer-rule splits into forbidden=grep / endorsed=rg")

    # unrelated rules never conflict
    calm = load_store(os.path.join(tmp, "calm.yaml"))
    add_rule(calm, "Never use a bare `except:`.", bundle="base")
    add_rule(calm, "No `print()` for debugging in `mutalyze/`.", bundle="base")
    check(find_conflicts(calm.active()) == [], "unrelated prohibitions do not conflict")

    # -- checkability (reuses the Phase 1 classifier) ----------------------
    ok, detail = checkability("Use `rg` (not `grep`) for searching.")
    check(ok and detail == "command", "a use-X-not-Y rule is checkable as a command rule")
    ok2, _ = checkability("Write beautiful, elegant code.")
    check(not ok2, "a vague aspiration is reported as not checkable")

    # -- compose -----------------------------------------------------------
    composed = compose(reloaded, bundles=["base", "feature"])
    check(composed.text.startswith("# Project rules"), "composed file has the title")
    check("## base" in composed.text and "## feature" in composed.text,
          "composed file groups by bundle")
    check(len(composed.duplicates) == 1,
          "the cross-bundle duplicate is dropped once and reported (got %d)"
          % len(composed.duplicates))
    body = composed.text
    check(body.count("- Use `rg` (not `grep`) for searching the codebase.") == 1,
          "the duplicated rule appears exactly once in the output")

    # THE round-trip property: Phase 1 can read a composed file back.
    recovered = extract_candidates(composed.text)
    check(len(recovered) == len(composed.used),
          "every composed rule is re-extractable by the compiler (%d written, %d read back)"
          % (len(composed.used), len(recovered)))

    # -- overwrite guard ---------------------------------------------------
    out_path = os.path.join(tmp, "AGENTS.md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(composed.text)
    check(is_generated(out_path), "a composed file is recognized as generated")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("# Hand-written rules\n\n- Never touch this.\n")
    check(not is_generated(out_path), "a hand-written file is NOT treated as generated")

    # -- ordering / precedence --------------------------------------------
    flipped = compose(reloaded, bundles=["feature", "base"])
    first_heading = [ln for ln in flipped.text.splitlines() if ln.startswith("## ")][0]
    check(first_heading == "## feature", "bundle order given is the order emitted")

    if FAILURES:
        sys.stdout.write("rule store FAIL\n")
        for f in FAILURES:
            sys.stdout.write("  - %s\n" % f)
        return 1
    sys.stdout.write(
        "rule store PASS (import · dedupe · idempotent re-import · bundles · "
        "conflict flags · checkability · compose round-trip · overwrite guard)\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
