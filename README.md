# ruleguard (rule-compliance monitor)

**Catches your coding agent breaking your own `CLAUDE.md` rules, and tells you which rule and at which turn.**

Every repo using an AI coding agent has a rules file (`CLAUDE.md` / `AGENTS.md`).
The agent follows it at first, then over a long session quietly stops. Nothing
errors. Nobody checks. ruleguard reads your rules file and one session
transcript and reports exactly which rules were broken, at which turn, with the
literal evidence — so you can open the transcript and see it yourself.

**No LLM judges a violation.** Every finding is a deterministic, citable fact.

```
$ ruleguard check

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
UNSUPPORTED (2 rules — see .ruleguard/checks.yaml, or --verbose)
```

### The honest modal case: a quiet run

Most first runs look like this — nothing broke. That is not a dead end: the
session-shape block still tells you what the agent spent its time doing, and the
counts tell you *why* it's quiet.

```
$ ruleguard check

Session: 0a3f21c9  (312 turns, 41m)
Rules:   4 found · 3 compiled · 1 unsupported
Path:    main path numbered

SESSION SHAPE  (tool-call phases — descriptive, never gates a check)
  turns 3–58       exploring
  turns 61–300     implementing

VIOLATIONS (0) — every check held.

HELD (3 rules, no violations)
UNSUPPORTED (1 rule — see .ruleguard/checks.yaml, or --verbose)
```

`0 violations` here means *the compiled checks ran and held* — read it together
with `3 compiled · 1 unsupported`. A quiet report can also mean your rules mostly
compiled to `unsupported`; ruleguard always shows you which. See
[Limitations](#limitations--read-before-trusting-a-green-run).

## Install & use

```bash
python -m venv .venv && ./.venv/bin/pip install -e .   # PyYAML is the only dep

cd your-repo
ruleguard check                 # audits the newest session for this repo
ruleguard check path/to.jsonl   # a specific transcript
ruleguard check --json          # machine-readable
ruleguard check --verbose       # list held + unsupported rules individually
ruleguard check --recompile     # re-run rule compilation
```

Zero config: it auto-discovers the most recent Claude Code session for the
current directory, compiles your rules into checks, and runs them.

## Architecture: compile, then execute

Two strictly separated phases.

**Phase 1 — compile** (`ruleguard/compile_rules.py`, deterministic, no LLM).
Reads `CLAUDE.md` / `AGENTS.md` (following symlink stubs), extracts normative
lines, and compiles each into an executable check written to
**`.ruleguard/checks.yaml`** — human-readable and **meant to be hand-edited**.
A rule that can't be mapped to a mechanical check with confidence goes to
`unsupported`, counted and named, never silently dropped and never turned into
a shaky check.

**Phase 2 — execute** (`ruleguard/execute.py`, **no LLM calls, ever**). Streams
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

`ruleguard check --signals` emits hashes, categories, counts, and turn integers
— never a command, path, or rule text. A leak-guard re-checks the serialized
payload and raises if any raw content slipped in. See
[TELEMETRY.md](TELEMETRY.md).

### The hard parts it handles

- **Branch-safe turns.** Transcripts are trees (rewinds/edited prompts branch;
  subagents are appended inline). ruleguard traces the main path via parent
  pointers, numbers turns along it only, and keeps each line's uuid so citations
  survive renumbering. Off-path and subagent lines are kept, not numbered.
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

## Limitations — read before trusting a green run

- **The safety pack's "secrets to disk" (SP004) is destination-scoped.** It fires
  when a key-shaped string is written to a *real credential file* (`.env`,
  `id_rsa`, `*.pem`, `credentials`, …). It does **not** fire on a secret
  hard-coded into a source file like `config.ts` — that's the common case, and
  ruleguard will not catch it. Content alone can't tell a real key from a test
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
./.venv/bin/python tests/test_safety_pack.py   # safety-pack false-positive regression guard
```
