# mutalyze (rule-compliance monitor)

**Catches your coding agent breaking your own `CLAUDE.md` rules, and tells you which rule and at which turn.**

Coding agents now run for long, largely unattended stretches, and the only record
of what one actually did is the session log it writes to disk. Your rules live in
one file (`CLAUDE.md` / `AGENTS.md`); the record of the agent's behaviour lives in
another. **mutalyze checks one against the other** — it reads your rules and one
session transcript and reports exactly which rules were broken, at which turn,
with the command or line that proves it.

**No LLM judges a violation.** Every finding is a deterministic fact you can open
the transcript and see for yourself.

### Integration, not invention

This is not a new method — it's the assembly that was missing. The problem is
well-documented and the pieces are already published: tools that lint your rules
file, tools that read session transcripts, and research on step-wise verifiers
that score a trajectory against externally-checkable rules. What no one ships is
any of it as **one thing you install and point at a real repo.** The adjacent
tools each stop short of the join:

- **Rules-file linters / packs** (agnix, ctxlint, rule packs) check that your
  rules file is well-formed, or hand you rules — they never look at a session.
- **Transcript tools** (viewers, cost dashboards) read the session for cost,
  search, or to *generate* rules — never to check the rules were followed.
- **SpecLock** does enforce rules, but at the diff / proposed-action layer (a
  pre-commit hook plus a "check before acting" MCP tool). Its evidence is a diff
  or a proposed action; mutalyze's is a turn in the session that actually ran.
- **sessionaudit** scans transcripts for dangerous commands and secrets — close
  to mutalyze's built-in safety pack, worth knowing about.

mutalyze's slot is the one nobody occupies: join the rules file to the real
session transcript and return **per-turn evidence, deterministically, from one
install.** The claim is integration, not novelty.

```
$ mutalyze check

Session: 1158ee04  (879 turns, 3h 12m)
Rules:   7 found · 5 compiled · 2 unsupported
Path:    main path numbered; 4 off-path lines (rewinds) kept but not numbered

SESSION SHAPE  (tool-call phases — descriptive, never gates a check)
  turns 8–74       debugging
  turns 78–635     implementing
  turns 639–728    exploring
  turns 730–876    shipping

VIOLATIONS (12)

  turn 142    CM005  Use `rg` (not `grep`) for searching.
                Bash → grep -n "DATACENTERS\|const NUKES\|function drawMarkers" public/watchover.html

  turn 349    CM005  Use `rg` (not `grep`) for searching.
                Bash → grep -n '"r",sel?7:4\|attr("r",8)' public/watchover.html
  ...

HELD (n rules, no violations)
UNSUPPORTED (2 rules — see .mutalyze/checks.yaml, or --verbose)
```

### The honest modal case: a quiet run

Most first runs look like this — nothing broke. That is not a dead end: the
session-shape block still tells you what the agent spent its time doing, and the
counts tell you *why* it's quiet.

```
$ mutalyze check

Session: 0a3f21c9  (312 turns, 41m)
Rules:   4 found · 3 compiled · 1 unsupported
Path:    main path numbered

SESSION SHAPE  (tool-call phases — descriptive, never gates a check)
  turns 3–58       exploring
  turns 61–300     implementing

VIOLATIONS (0) — every check held.

HELD (3 rules, no violations)
UNSUPPORTED (1 rule — see .mutalyze/checks.yaml, or --verbose)
```

`0 violations` here means *the compiled checks ran and held* — read it together
with `3 compiled · 1 unsupported`. A quiet report can also mean your rules mostly
compiled to `unsupported`; mutalyze always shows you which. See
[Limitations](#limitations--read-before-trusting-a-green-run).

## Install & use

```bash
python -m venv .venv && ./.venv/bin/pip install -e .   # PyYAML is the only dep

cd your-repo
mutalyze check                 # audits the newest session for this repo
mutalyze check path/to.jsonl   # a specific transcript
mutalyze check --json          # machine-readable
mutalyze check --verbose       # list held + unsupported rules individually
mutalyze check --recompile     # re-run rule compilation
```

Zero config: it auto-discovers the most recent Claude Code session for the
current directory, compiles your rules into checks, and runs them.

### Watch mode: catch it live

`mutalyze check` runs after a session ends — which means remembering to run it.
`mutalyze watch` follows a **live** session and reports each violation as it
happens, then prints the same summary `check` would on exit.

```bash
mutalyze watch                 # follow the newest session for this repo
mutalyze watch --replay s.jsonl  # replay a recorded session (test/demo)
```

It is **quiet until something fires** — a watcher that chatters gets closed.
Safety-pack findings (`rm -rf`, force-push, secrets, `curl | sh`) are called out
distinctly, because catching those *now* is the whole point. It handles the live
oddities: a finding prints once and never repeats; a rewind that retracts a
reported turn marks it **withdrawn** rather than leaving it standing; a
mid-session compaction is bridged without re-reporting; partial (mid-write)
lines are buffered.

**How it watches, and the one blind spot.** It *tails the transcript file* — it
does **not** install a hook. That means zero config, no mutation of
`~/.claude/settings.json`, and it reuses the exact same checks as `check` (no
LLM, ever). The trade-off, stated plainly: a hook would see **permission-denied
tool calls**, which the harness kills before they reach the transcript — watch
mode cannot see those. It reports and never intervenes: it never blocks,
re-injects a rule, or touches the agent.

## Architecture: compile, then execute

Two strictly separated phases.

**Phase 1 — compile** (`mutalyze/compile_rules.py`, deterministic, no LLM).
Reads `CLAUDE.md` / `AGENTS.md` (following symlink stubs), extracts normative
lines, and compiles each into an executable check written to
**`.mutalyze/checks.yaml`** — human-readable and **meant to be hand-edited**.
A rule that can't be mapped to a mechanical check with confidence goes to
`unsupported`, counted and named, never silently dropped and never turned into
a shaky check.

**Phase 2 — execute** (`mutalyze/execute.py`, **no LLM calls, ever**). Streams
the transcript once and evaluates every check. Every violation carries
`(check_id, turn, line_id, line_no, evidence)`.

### Three check types

| Type | Scans | Example |
|---|---|---|
| `command` | `Bash` inputs | "Use `cargo nextest` not `cargo test`" |
| `content` | `Write` / `Edit` / `MultiEdit` payloads | "No `console.log`" |
| `ordering` | sequence of tool calls | "Read a file before editing it" |

### Three verdicts, not two

A finding is `violated`, `held`, or **`unresolved`** — the last for a command
whose working directory was built from a shell variable (`cd "$D" && grep …`),
so we can't tell if it ran in the governed repo. Unresolved findings are neither
claimed nor dropped: they're reported apart, and you can still open the turn.
A violation always rests on evidence you can stand behind.

### Scope: repo vs session

Each rule has a `scope` in `checks.yaml`. `repo` (default) counts only inside
the governed repo — right for style rules like `rg` vs `grep`. `session` applies
wherever the agent worked — right for safety rules. Hand-editable.

### Built-in safety pack

Ships with checks no rules file bothers to write but every repo wants —
force-push, `curl | sh`, `rm -rf` of a home/root path, secrets written to disk —
all `session`-scoped and high-confidence. It runs even with no `CLAUDE.md`
(so a first run is never fully silent). Disable with `--no-safety`.

### Team tier: derived signals only

`mutalyze check --signals` emits hashes, categories, counts, and turn integers
— never a command, path, or rule text. A leak-guard re-checks the serialized
payload and raises if any raw content slipped in. See
[TELEMETRY.md](TELEMETRY.md).

### The hard parts it handles

- **Branch-safe turns.** Transcripts are trees (rewinds/edited prompts branch;
  subagents are appended inline). mutalyze traces the main path via parent
  pointers, numbers turns along it only, and keeps each line's uuid so citations
  survive renumbering. Off-path and subagent lines are kept, not numbered.
- **Compaction-safe.** When a long session auto-compacts, Claude Code writes a
  boundary line whose `parentUuid` is `null` — the pointer chain is cut on
  purpose — and moves the real link to `logicalParentUuid`. A naïve parent walk
  stops at the last boundary and silently drops the entire session before it
  (on a real 2,460-line session that was 95% of it). mutalyze bridges the cut,
  so a rule broken in hour one is still caught after three compactions. This is
  exactly the span the agent's own context can no longer see.
- **`Write` ≠ `Edit`.** `Edit`/`MultiEdit` added lines are checked; `Write` is
  content-checked only when the transcript shows the file *created* this session
  (`toolUseResult.type == "create"`), so a pre-existing violation in a rewritten
  file isn't blamed on the agent.
- **Comments & strings stripped** before content/command matching, so `any` in a
  comment or `grep` inside a quoted pattern doesn't false-fire.
- **Doesn't guess branch state.** Uses the recorded `gitBranch`, and skips a
  branch-gated check when the command `cd`s into another repo.
- **Refuses rather than reports zero.** Exits with an error (never a clean
  report) when no rules file resolves, fewer than 5 checks compile, or a
  mostly-TS/Py repo yields zero content checks — because a broken compile and a
  compliant session otherwise look identical.

## Cost

Execute consumes **zero tokens** — it's plain parsing, no model, no network.
Compile runs once per rules-file change, on *your* own API key (never routed
through a server), and is cheap. An LLM-judge approach pays per session, every
session, and the cost grows with session length — a judge can't cache, because
every transcript is new.

| approach | per session |
|---|---|
| **mutalyze** | **$0** (one-time compile ≈ $0.02 on Haiku 4.5) |
| LLM judge · Sonnet-class · 300K-token session | ~$0.60 |
| LLM judge · Opus 4.8 | ~$1.50 |
| LLM judge · Fable 5 | ~$3.00 |

*(Judge figures are order-of-magnitude estimates for one full-session pass.)*

## It reads what the agent can't

Claude Code writes an append-only JSONL transcript that is **never compacted**.
The agent's own context is finite (~830K usable tokens in Claude Code after the
auto-compaction buffer) and subject to context rot — recall degrades worst for
low-salience text near the *start* of the window, which is exactly where
`CLAUDE.md` sits. mutalyze reads the **full** session the agent itself can no
longer see. A verifier subagent, by contrast, inherits the same rot it is meant
to detect.

## What actually compiles — a self-red-team

Compilation is the honest weak point: it maps *your* prose into checks, and rules
you'd never phrase yourself break in ways the author can't imagine. So here is the
compiler red-teamed against **45 rules a real person might write** — not ones
tuned to pass:

| outcome | count | examples |
|---|---|---|
| **Compiled to a correct check** | 26 | `use rg not grep`, `no console.log`, `no print()`, `don't commit to main`, read-before-edit |
| **Refused — `unsupported`, counted + shown** | 19 | `keep functions under 50 lines`, `add docstrings`, `never edit package-lock.json`, `always run tests` |

A third work, a third honestly refuse. What matters is the two failure modes that
read as *success* — both now guarded:

- **Forbidding the sanctioned tool.** `use pytest, not unittest` must forbid
  `unittest`, never `pytest` — the reverse flags every correct test run.
- **A check that can never fire.** `no TODO` would compile to nothing useful
  (comments are stripped before matching), so it's marked `unsupported`, not
  emitted as a check that silently always "holds".
- **Blanket forbids from conditional rules.** `never pip install without a venv`
  can't verify the condition, so it refuses rather than flag *every* `pip install`.

**The escape hatch is the point.** Compilation doesn't have to be perfect on
unseen input: every rule that can't be compiled with confidence is **counted and
named** in the `unsupported` bucket (never silently dropped), and
`.mutalyze/checks.yaml` is **plain, hand-editable YAML** — a wrong compile is a
one-line fix, not a broken tool. Read the header's `compiled` / `unsupported`
counts, not just the violation count.

## Limitations — read before trusting a green run

- **The safety pack's "secrets to disk" (SP004) is destination-scoped.** It fires
  when a key-shaped string is written to a *real credential file* (`.env`,
  `id_rsa`, `*.pem`, `credentials`, …). It does **not** fire on a secret
  hard-coded into a source file like `config.ts` — that's the common case, and
  mutalyze will not catch it. Content alone can't tell a real key from a test
  fixture, so precision was chosen over coverage here. Do not read "secrets to
  disk" as full secret-scanning; it isn't one.
- **Checkable rules skew trivial.** Across a sample of 128 real rules files, about
  **~65% of normative rule lines are mechanically checkable** — but that 65% skews
  toward the cheap ones (`rg` vs `grep`, dangerous commands), not the rules you
  actually care about. Rules needing judgement are marked `unsupported`, counted,
  never guessed at. See [FINDINGS.md](FINDINGS.md) for what that meant on a real
  corpus (one style rule produced the overwhelming majority of findings).
- **A quiet run is not a clean bill of health.** Zero findings can mean the agent
  complied — or that your rules mostly compiled to `unsupported`, or that the
  session didn't touch the checked surface. Read the `compiled` / `unsupported`
  counts in the header, not just the violation count.
- **Command location is best-effort.** A command whose directory comes from a
  shell variable is reported `unresolved`, not violated — see the verdicts above.
- **v1 is one harness (Claude Code) and detection only.** Drift-over-time, live
  re-injection via hooks, and team aggregation are deliberately out until the
  detector proves useful on real rule-governed sessions.

## Validation

Run against 7 real sessions and hand-inspected. See **[FINDINGS.md](FINDINGS.md)**
for the honest numbers, the held-out precision, and the four traps hit along the
way (the write-up's real subject).

```bash
./.venv/bin/python tests/make_fixture.py       # labeled fixture: every check type + false-alarm guards
./.venv/bin/python tests/test_safety_pack.py   # safety pack, both directions: fires on real
                                               # danger (24 events, right turn + evidence) and
                                               # stays silent on 26 look-alikes
```
