# mutalyze

A command-line tool that checks a Claude Code session against the rules in your
`CLAUDE.md` and reports which rules the agent broke, at which turn, with the
command or edit that proves it. No language model decides whether a rule was
broken. Every finding is something you can open the transcript and see.

Coding agents run for long stretches with little supervision, and the only
record of what an agent did is the JSONL log Claude Code writes to disk. Your rules
are in one file (`CLAUDE.md` or `AGENTS.md`), while the agent's behavior is in another.
mutalyze reads both and compares them.

## Install

```bash
pip install git+https://github.com/mutalyze/mutalyze     # PyYAML is the only dependency
```

## Use

```bash
cd your-repo
mutalyze check                 # audit the newest session for this repo
mutalyze check path/to.jsonl   # a specific transcript
mutalyze check --json          # machine-readable
mutalyze check --verbose       # list held and unsupported rules
mutalyze check --recompile     # re-run rule compilation
```

Mutalyze finds the most recent Claude Code session for the current directory, compiles
your rules into checks, and runs them. There's nothing to configure.

```
$ mutalyze check

Session: 1158ee04  (879 turns, 3h 12m)
Rules:   7 found · 5 compiled · 2 unsupported
Path:    main path numbered; 4 off-path lines (rewinds) kept but not numbered

SESSION SHAPE  (tool-call phases, descriptive, never gates a check)
  turns 8–74       debugging
  turns 78–635     implementing
  turns 639–728    exploring
  turns 730–876    shipping

VIOLATIONS (12)

  turn 142    CM005  Use `rg` (not `grep`) for searching.
                Bash → grep -n "DATACENTERS\|const NUKES" public/watchover.html

  turn 349    CM005  Use `rg` (not `grep`) for searching.
                Bash → grep -n '"r",sel?7:4' public/watchover.html
  ...

HELD (n rules, no violations)
UNSUPPORTED (2 rules, see .mutalyze/checks.yaml or --verbose)
```

Most first runs find nothing:

```
$ mutalyze check

Session: 0a3f21c9  (312 turns, 41m)
Rules:   4 found · 3 compiled · 1 unsupported

SESSION SHAPE  (tool-call phases, descriptive, never gates a check)
  turns 3–58       exploring
  turns 61–300     implementing

VIOLATIONS (0), every check held.

HELD (3 rules, no violations)
UNSUPPORTED (1 rule, see .mutalyze/checks.yaml or --verbose)
```

Zero violations means the compiled checks ran and none fired. Read it with the
`compiled` and `unsupported` counts: a quiet report can also mean most of your
rules compiled to `unsupported`, and mutalyze always shows which. See
[Limitations](#limitations).

## Watch mode

`mutalyze check` runs after a session ends, so you have to remember to run it.
`mutalyze watch` follows a live session and prints each violation when it
happens, then prints a final summary of each violation on exit.

```bash
mutalyze watch                   # follow the newest session
mutalyze watch --replay s.jsonl  # replay a recorded session
```

Mutalyze stays quiet until a check fires. Safety-pack findings (`rm -rf`, force-push,
secrets, `curl | sh`) are marked separately, since those are the ones worth
seeing right away. It handles the awkward parts of reading a file that's still
being written: each finding prints once, a rewind that retracts a reported turn
marks it withdrawn instead of leaving it, a mid-session compaction is handled
without re-reporting, and a half-written line is buffered until it's complete.

Watch mode tails the transcript file. It does not install a hook, so there's no
config and no change to `~/.claude/settings.json`, and it runs the same checks
as `check`. The cost of not using a hook: permission-denied tool calls never
reach the transcript, so watch mode can't see them. It only reports but it never
blocks the agent or changes what it does.

## Related tools

The problem is known and the pieces exist but what's missing is a single tool you
install and point at a repo.

- Rules-file linters (agnix, ctxlint, rule packs) check that your rules file is
  well-formed, or hand you rules. They don't look at a session.
- Transcript viewers and cost dashboards read the session for cost or search, or
  to generate rules, not to check whether the rules were followed.
- SpecLock enforces rules, but at the diff and proposed-action layer (a
  pre-commit hook and a check-before-acting MCP tool). Its evidence is a diff or
  a proposed action; mutalyze's is a turn that proactively ran.
- sessionaudit scans transcripts for dangerous commands and secrets, which
  overlaps with mutalyze's safety pack.

mutalyze joins the rules file to the session transcript and returns per-turn
evidence from one install.

## How it works

Two phases, kept separate.

Compile (`mutalyze/compile_rules.py`) reads `CLAUDE.md` or `AGENTS.md`, pulls out
the normative lines, and turns each into a check written to
`.mutalyze/checks.yaml`. That file is plain YAML and meant to be edited by hand.
A rule that can't be mapped to a mechanical check with confidence is marked
`unsupported` and listed, not dropped and not turned into a guess. Compile is
deterministic and runs once per change to the rules file.

Execute (`mutalyze/execute.py`) reads the transcript once and runs every check.
No model, no network. Each violation records `(check_id, turn, line_id, line_no,
evidence)`.

### Check types

| Type | Scans | Example |
|---|---|---|
| command | Bash inputs | "Use `cargo nextest`, not `cargo test`" |
| content | Write / Edit / MultiEdit payloads | "No `console.log`" |
| ordering | sequence of tool calls | "Read a file before editing it" |

### Verdicts

A finding is `violated`, `held`, or `unresolved`. Unresolved covers a command
whose working directory came from a shell variable (`cd "$D" && grep …`), where
there's no way to tell whether it ran inside the repo. Those are reported
separately and you can still open the turn but they're never counted as violations.

### Scope

Each rule has a `scope` in `checks.yaml`. `repo` (the default) counts only inside
the repo, which is right for style rules. `session` counts wherever the agent
worked, which is right for safety rules. You can change it by hand.

### Safety pack

mutalyze ships with checks that no rules file bothers to write but most repos
want: force-push, `curl | sh`, `rm -rf` of a home or root path, secrets written
to disk. They're session-scoped and run even with no `CLAUDE.md`, so a first run
is never completely silent. Turn them off with `--no-safety`.

### Signals output

`mutalyze check --signals` emits hashes, categories, counts, and turn numbers
only, never a command, path, or rule text. A guard re-checks the serialized
output and raises if any raw content got through. See [TELEMETRY.md](TELEMETRY.md).

### Things that are easy to get wrong, and how it handles them

- Transcripts are trees and not lists. Rewinds and edited prompts create branches,
  and subagent runs are appended inline. mutalyze follows the main path by parent
  pointers, numbers turns along that path only, and keeps each line's uuid so a
  citation survives renumbering.
- When a long session compacts, Claude Code writes a boundary line with
  `parentUuid` set to null and the real link moved to `logicalParentUuid`.
  Walking parent pointers naively stops at the last boundary and drops everything
  before it. On one real 2,460-line session that was 95% of the session. mutalyze
  bridges the boundary, so a rule broken early is still caught after several
  compactions.
- `Edit` and `MultiEdit` additions are content-checked. `Write` is only checked
  when the transcript shows the file was created this session, so a pre-existing
  line in a rewritten file isn't blamed on the agent.
- Comments and string literals are stripped before matching, so `any` in a
  comment or `grep` inside a quoted string doesn't fire.
- Branch-gated checks use the recorded `gitBranch` and are skipped when the
  command `cd`s into another repo.
- It errors out instead of printing a clean zero when no rules file resolves,
  fewer than five checks compile, or a mostly-TS/Py repo produces no content
  checks. Otherwise a broken compile and a compliant session look the same.

## Cost

Execute uses no tokens; it's parsing. Compile runs once per change to your rules
file, on your own API key, and is cheap. An LLM-judge approach pays per session
and can't cache, since every transcript is new.

| approach | per session |
|---|---|
| mutalyze | $0 (one-time compile ≈ $0.02 on Haiku 4.5) |
| LLM judge, Sonnet-class, 300K-token session | ~$0.60 |
| LLM judge, Opus 4.8 | ~$1.50 |
| LLM judge, Fable 5 | ~$3.00 |

Judge figures are rough estimates for one full-session pass.

## Why read the transcript

Claude Code writes an append-only JSONL log that is never compacted. The agent's
own context is finite (about 830K usable tokens after the compaction buffer) and
recall degrades for low-salience text near the start of the window, which is
where `CLAUDE.md` sits. mutalyze reads the full session, including the part the
agent can no longer see. A verifier running inside the same context would
inherit the same problem it's meant to catch.

## What compiles, and what doesn't

Compilation is the weakest part, because it turns your prose into checks and
people phrase rules in ways the author never anticipated. Here's the compiler run
against 45 rules of the kind a real person writes, not ones chosen to pass:

| outcome | count | examples |
|---|---|---|
| compiled to a working check | 26 | `use rg not grep`, `no console.log`, `no print()`, `don't commit to main`, read-before-edit |
| refused (`unsupported`, counted and listed) | 19 | `keep functions under 50 lines`, `add docstrings`, `never edit package-lock.json`, `always run tests` |

Some compile, some are refused. The cases worth watching are the two that could
pass as success without being caught:

- Forbidding the wrong tool. "Use pytest, not unittest" has to forbid `unittest`,
  not `pytest`. Backwards, it flags every correct test run.
- A check that can never fire. "No TODO" would compile to a pattern that never
  matches, because comments are stripped before matching, so it's marked
  `unsupported` rather than a check that always reports "held".
- A blanket forbid from a conditional rule. "Never pip install without a venv"
  can't verify the condition, so it refuses instead of flagging every
  `pip install`.

Compilation doesn't have to be perfect on rules it hasn't seen. Anything it
can't compile with confidence goes to `unsupported`, counted and named, and
`.mutalyze/checks.yaml` is plain YAML you can fix in a line. Read the `compiled`
and `unsupported` counts, not just the violation count.

## Limitations

- The safety pack's secrets check (SP004) is destination-scoped. It fires when a
  key-shaped string is written to a real credential file (`.env`, `id_rsa`,
  `*.pem`, `credentials`). It does not fire on a secret hard-coded into a source
  file like `config.ts`, which is the common case. Content alone can't tell a
  real key from a test fixture, so it errs toward not firing. This is not secret
  scanning.
- Checkable rules skew trivial. Across 128 real rules files, roughly 65% of
  normative lines are mechanically checkable, but that 65% is weighted toward the
  cheap ones (`rg` vs `grep`, dangerous commands), not the rules you most care
  about. Rules that need judgment are marked `unsupported`. On the corpus in
  FINDINGS.md, one style rule produced most of the findings.
- A quiet run is not proof of compliance. Zero findings can mean the agent
  complied, or that most rules compiled to `unsupported`, or that the session
  never touched the checked surface. Read the header counts.
- Command location is best-effort. A command whose directory comes from a shell
  variable is reported `unresolved`, not violated.
- This is one harness (Claude Code) and detection only. Drift over time, live
  re-injection through hooks, and team aggregation are out until the detector
  proves useful on real rule-governed sessions.

## Validation

Two separate things are validated here, and one does not stand in for the other.

**Rule-detection precision (7 real development sessions).** Evaluated on seven real Claude Code software engineering sessions from prior development projects. Each session was analyzed using the project's `CLAUDE.md` rule context, allowing Mutalyze to evaluate agent behavior against the intended coding rules rather than generic transcript patterns. Every reported violation was manually reviewed against the original transcript to verify that it corresponded to a genuine in-scope event.
On the three held-out sessions, 55 of 55 reported violations corresponded to real in-scope events, with one unresolved case. This remains a small validation set drawn from one developer's own projects, so it demonstrates that the detector accurately identifies rule-relevant events on realistic development sessions, but it is not yet a broad evaluation across multiple users or diverse codebases. Detailed results, held-out precision, and error analysis are available in FINDINGS.md.

**Parser robustness (long local sessions).** This is a separate claim about transcript parsing rather than rule detection. The parser was evaluated on the ten longest Claude Code sessions available on the development machine (longest: 10,420 lines. Three containing compaction boundaries). Across all sessions, parsing completed without crashes, maintained contiguous turn numbering, and preserved coverage across every compaction boundary.
On the 10,420-line session with four compactions, a naïve `parentUuid`-only traversal retained only 872 turns, while the parser reconstructed 6,938 turns, recovering transcript history that would otherwise be silently lost. This demonstrates robustness on long & compacted local transcripts. Broader validation across additional users remains future work.

```bash
./.venv/bin/python tests/make_fixture.py            # labeled fixture: every check type, plus false-alarm guards
./.venv/bin/python tests/test_safety_pack.py        # safety pack both directions: 24 real dangers, 26 look-alikes
./.venv/bin/python tests/sweep_parser_robustness.py # parser over the longest local sessions (naive vs bridged coverage)
```
