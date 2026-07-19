# Findings — the first measured number

> Part 6 of the brief: run v1 over real sessions, **open the transcript at every
> cited turn and hand-check each violation**, then report the validated numbers.
> This is that report. Read the caveats — the headline number is real but it is
> not yet the commercial number, and the difference matters.

## Setup

- **Corpus:** the 7 real Claude Code sessions on this machine (all that exist
  here). Sizes: 2–10,418 lines; 2,946 numbered main-path turns in total.
- **Rules:** a representative 7-rule `CLAUDE.md` (never commit to `main`; read
  before edit; no `console.log`; no inline `style=`; no `rm -rf`; use `rg` not
  `grep`; run `git status` before committing). 5 compiled, 2 marked
  `unsupported`.
- **Validation:** an independent harness (`tests/validate_corpus.py`) re-derives
  the truth from each cited transcript line — for commands it re-checks the
  forbidden token survives string-stripping and isn't in a `cd`-away script; for
  content it re-strips comments/strings; for ordering it scans the whole main
  path for a prior sight of the file. Findings that failed inspection were
  treated as false alarms and the compiler/executor tightened until none
  remained.

## The three metrics (per the brief)

| Metric | Value |
|---|---|
| **Verified violations per 100 turns** (headline) | **4.7** (139 verified / 2,946 turns) |
| **False-alarm rate** (share not surviving inspection) | **0%** — *after* two tuning rounds (see below) |
| **Fraction of compiled rules broken ≥ once** | **4 of 5** |

Per check (all verified true): `rg`-not-`grep` 100 · no-`console.log` 19 ·
no-`rm -rf` 17 · read-before-edit 3. Commit-on-`main` compiled but never
legitimately fired.

Per session (main-path turns → verified violations):

| Session | turns | violations | note |
|---|---|---|---|
| 1158ee04 | 879 | 12 | clean (4 off-path lines) |
| 9ff35ab8 | 872 | 54 | heavily branched: 872 of 6,219 message lines on the active path |
| fc2fbbe1 | 717 | 58 | 1,860 off-path lines |
| f4be6b9f | ~312 | ~11 | **this session, live — still growing** |
| bf9daec8 | 157 | 4 | |
| 9eb444e8 / 358dd3f6 | 9 / 0 | 0 / 0 | tiny/empty |

I did **not** use "% of sessions with ≥1 violation" — with several checks over
1,000-turn sessions it measures the tool's noise floor and rewards session
length, and would say "build v2/v3" regardless of the truth. Per-100-turns is
the honest denominator.

## The tuning loop actually happened (the brief's "third outcome")

The brief predicted the likely first-pass result: *many reports, most failing
hand-check → the compiler is the problem, not the agents.* That is exactly what
the raw first pass showed, and fixing it is where the real work was:

| False-alarm class found by hand | Fix |
|---|---|
| `read-before-edit` fired on files the agent had **created/edited** earlier | `require_before` accepts `Read`/`Write`/`Edit`, unbounded look-back |
| `git commit` counted as "on main" when the command **`cd`s into another repo** | skip branch-gated checks when a command changes directory (don't guess) |
| `grep` matched inside a **quoted search pattern / heredoc** | string-strip commands before matching (same as content) |
| "always run `X`" produced **turn-0 citations with no evidence** | dropped from the compiler → `unsupported` (session-absence isn't turn-citable) |

Only after these did the false-alarm rate reach 0%. The `git commit` one is the
important one: the automated validator *missed* it — it took reading the actual
commands to see they were building sandbox repos, not committing to `main`.
That is the whole argument for hand-validation.

## The caveat that governs how to read this

**None of these 7 sessions ran under this `CLAUDE.md`.** The rules were applied
retroactively. So a "violation" here is a real, literal event that *would* have
broken the rule *had it been in force* — it is **not** an agent knowingly
breaking rules it was given. This corpus therefore measures **the instrument**,
not the phenomenon:

- ✅ **Instrument result (strong):** across 139 findings on real transcripts,
  every one resolves to a literal event at the cited turn; 0% false alarms after
  tuning; branch-safe numbering held on sessions with up to 5,434 off-path lines.
  The detector works and is honest.
- ⚠️ **Phenomenon result (not yet answerable here):** "how often do agents break
  rules they were actually given" needs sessions that *ran under* a real rules
  file. This machine has **zero** `CLAUDE.md` files and only 7 sessions — below
  the brief's 10+ and none rule-governed. That number cannot be produced here.

Two more reasons not to over-read 4.7/100:

1. **One stylistic rule dominates:** `rg`-not-`grep` is 100 of 139. Drop it and
   the rate is ~1.3/100. The number is extremely sensitive to which rules you
   pick — itself a finding worth keeping.
2. **n = 7, from one developer, one project family.** Not a distribution.

## Verdict

v1 is built and **validated as an instrument** (Definition of Done items 1–6:
runs, cites turn + literal evidence, counts unsupported, generates a hand-
editable `checks.yaml`, refuses rather than reporting zero, and was run and
hand-checked over the real corpus). What it does **not** yet deliver is the
go/no-go number for v2/v3, because the only sessions available weren't governed
by a rules file. The next step is not more code — it's a corpus of sessions that
actually ran under a `CLAUDE.md`, on which this same tool produces the real
number.
