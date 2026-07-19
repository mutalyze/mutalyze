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

VIOLATIONS (12)

  turn 142    CM005  Use `rg` (not `grep`) for searching.
                Bash → grep -n "DATACENTERS\|const NUKES\|function drawMarkers" public/watchover.html

  turn 349    CM005  Use `rg` (not `grep`) for searching.
                Bash → grep -n '"r",sel?7:4\|attr("r",8)' public/watchover.html
  ...

HELD (n rules, no violations)
UNSUPPORTED (2 rules — see .ruleguard/checks.yaml, or --verbose)
```

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

## Validation

Run against real sessions and hand-checked. See **[FINDINGS.md](FINDINGS.md)**
for the measured violation rate, the 0% post-tuning false-alarm rate, and the
honest caveats.

```bash
./.venv/bin/python tests/make_fixture.py                       # labeled fixture: every check type + false-alarm guards
./.venv/bin/python tests/validate_corpus.py <repo> <session>...  # hand-validation harness
```

## Scope (v1)

Detection only. Drift-over-time, live rule re-injection via hooks, and team
aggregation are deliberately out until the detector proves useful.
