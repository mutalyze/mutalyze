# mutalyze

A command-line tool that checks a Claude Code session against the rules in your
`CLAUDE.md` and reports which rules the agent broke, at which turn, with the
command or edit that proves it. No language model decides whether a rule was
broken. Every finding is something you can open the transcript and see.

Coding agents run for long stretches with little supervision, and the only
record of what an agent did is the JSONL log Claude Code writes to disk. Your rules
are in one file (`CLAUDE.md` or `AGENTS.md`), while the agent's behavior is in another.
mutalyze reads both and compares them.

It also works the other direction. A rules file is read once, at the start, in one
repo — and half your real rules were never written down at all: you typed them at
the agent mid-session and they died with the conversation. So mutalyze reads your
past transcripts for the rules you *stated* but never saved, keeps them in a store
that outlives the repo, and puts the relevant ones back in front of the agent when
a long session gets compacted. See [Rule memory](#rule-memory). Every step of that
is yours to approve; nothing is written or applied on its own.

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

mutalyze rules mine            # rules you stated in chat but never wrote down
mutalyze rules compose -o AGENTS.md   # stack your rule bundles into a rules file
mutalyze context               # the rules worth re-asserting right now
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

## Rule memory

Auditing tells you a rule was broken. This half tries to stop it being forgotten in
the first place. Three commands, one loop, and you approve every step:

```bash
mutalyze rules mine                       # 1. find rules you only ever said in chat
mutalyze rules import --bundle base       #    (or seed the store from an existing rules file)
mutalyze rules compose -o AGENTS.md       # 2. stack bundles into a file any agent auto-loads
mutalyze context                          # 3. re-assert the relevant ones after a compaction
```

**1. Find the unwritten rules.** `rules mine` reads your past sessions for
instructions you gave in chat, and proposes the ones that aren't already written
down. It reads only *your* messages — assistant turns, tool results, and
harness-injected text are excluded, because a rule the agent proposed is not a rule
you set. Each proposal cites `session:line` so you can go read what you actually
said, and repeats are counted, because restating a rule four times is signal.

```
$ mutalyze rules mine
Scanned 9 session(s) for rules you stated in chat.
  4 already covered by your rules file or store.

PROPOSED (2 — nothing has been added)
  [1] always use `rg` (not `grep`) when searching   ×3
      check:command · 81e6e408.jsonl:412
  [2] never commit directly to main
      check:command · 4b21c7f0.jsonl:88

Add them with:  mutalyze rules mine --apply            (all)
                mutalyze rules mine --only 1,3         (some)
```

**2. Keep them somewhere that outlives the repo.** Approved rules go into a store
(`~/.mutalyze/rules.yaml`, hand-editable) grouped into **bundles** you stack — a
`base` set plus a per-project overlay. `compose` merges them, in the order you name
them, into an ordinary Markdown rules file that every compliant agent auto-loads.
Duplicates collapse and contradictions are flagged (`` `rg`: R004 endorses it, R012
forbids it ``) — both rules are kept, because resolving your intent is not this
tool's call. Composed output is the same bullet shape Phase 1 reads back, so a
composed file is auditable by `mutalyze check` with no special handling.

**3. Survive compaction.** When a long session is squeezed to fit the context
window, the rules file is the first thing to fall out — it was read once, hundreds
of turns ago. `mutalyze context` prints what's worth re-asserting, ranked by
relevance computed from each compiled check's own scope (`applies_to` globs,
forbidden command tokens, an ordering trigger) against what the agent just did
and/or the prompt you opened with. That's arithmetic, not judgment: no LLM is
involved.

```bash
mutalyze context --relevant-to "search the codebase for TODOs"   # rank against a task
mutalyze context --max 0                                          # re-assert everything
mutalyze hook print                                               # config for your agent
mutalyze hook install                                             # write it (backs up first)
```

Ranking only decides what to **trim**: when every rule fits the budget they are all
re-asserted, and a trim is always reported rather than silently applied. Output is
Markdown by default so it's useful in any agent; `--format json` and a Claude Code
hook envelope are also available.

One honest caveat on the hook: event names and how context is injected differ
between Claude Code releases, so `hook print` tells you to verify the wiring.
`mutalyze context` works standalone regardless — you can pipe it anywhere.

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
- This is one harness (Claude Code). Transcript reading is Claude-Code-specific,
  so mining reads only its logs today; the *output* side is portable, since
  `compose` writes a plain `AGENTS.md` that any compliant agent loads. Drift over
  time and team aggregation are still out.
- Mining leans strict, and misses on purpose. A rule stated vaguely, spread across
  several sentences, or implied by a correction rather than said outright will not
  be proposed — a junk proposal costs more trust than a missed one. Expect to still
  write some rules by hand.
- Conflict detection is narrow. It flags only an explicit endorse-versus-forbid on
  the same token. Two rules that contradict each other in prose, without a shared
  backticked token, are not detected.
- Relevance ranking is scope matching, not comprehension. It knows a rule mentions
  `*.ts` or forbids `grep`; it does not know your task. A rule with no compiled
  scope falls back to keyword overlap, and with no signal at all everything simply
  keeps its place in line.
- Re-injection depends on your agent's hooks. mutalyze can compute and print the
  rules, but it cannot push them into a running conversation by itself, and hook
  behavior varies by release. It reports and supplies context; it never blocks the
  agent.

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
./.venv/bin/python tests/test_store.py              # rule store: bundles, dedupe, conflicts, compose round-trip
./.venv/bin/python tests/test_mine.py               # mining: human-only extraction, precision filters, citations
./.venv/bin/python tests/test_context.py            # relevance ranking, budget trimming, compaction detection
```

**Rule memory is not yet validated on other people's sessions.** The store, mining,
and relevance ranking are covered by the test suites above and were exercised on
this machine's real transcripts, but the number that would matter — how many useful
rules mining recovers from a stranger's sessions, and whether re-assertion changes
agent behavior — needs users. It is a launch dependency, not a local one.
