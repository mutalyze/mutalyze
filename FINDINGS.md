# Findings

mutalyze over the 7 real Claude Code sessions on this machine, with a
representative `CLAUDE.md` plus the built-in safety pack. Every finding
hand-inspected.

Read §0 first: the adjudication rules are frozen *before* the numbers, on
purpose (see §4 for why).

---

## 0. Adjudication rules — frozen 2026-07-19, before the corpus was scored

What counts as a violation, per check type. Committed blind so the numbers below
are judged by a definition chosen ahead of them, not one tuned to flatter the
result. Changing any of these is a versioned event, not a quiet edit.

- **command (forbid).** A violation iff the forbidden token appears as a real
  command token — surviving shell string/comment stripping, word-boundary
  matched for single tokens — AND the command was **invoked** inside the scope.
  The rule forbids the *tool/invocation*, not what it searches or deletes:
  invoking `grep` in-repo breaks "use rg" even if it greps a `/tmp` file.
  - `scope: repo` → counts only when the invocation directory (recorded `cwd`,
    adjusted by a resolvable `cd`) is under the repo.
  - `scope: session` → counts anywhere.
  - Invocation directory built from a shell variable (`cd "$D" && …`) →
    **unresolved**, never counted as violated or held.
  - `when_branch` command → evaluated only when recorded `gitBranch` matches and
    the command does not `cd` (branch otherwise unreliable).
- **content (forbid).** A violation iff the pattern matches an **added** line —
  `Edit.new_string`, each `MultiEdit` edit's `new_string`, or `Write.content`
  *only when the file was created this session* — after stripping comments and
  string literals, with the file under the repo and matching `applies_to`.
  Pre-existing lines and out-of-repo files never count.
- **ordering (require_before).** A violation iff an in-repo trigger call (e.g.
  `Edit`) has no accepted prior call (`Read`/`Write`/`Edit`) of the same path
  within the window (unbounded for "have I ever seen this file").
- **scope is read from `checks.yaml`, never inferred from prose** (§3).
- Only main-path tool calls are numbered; side branches and subagent lines are
  kept, not scored.

By these rules, on the 3 held-out sessions (tuning used the other 2 + the two
empty ones): **55 violations, 55 verified as real in-scope invocations, 1
unresolved.** "Precision" here means each violation cites a real event the rule
names — not that the rule was worth writing.

---

## 1. Result: the checkable rules are the trivial ones.

67 violations across 3,459 turns. **66 are one rule — "use `rg` not `grep`."**
One is an in-repo `rm -rf`. The whole safety pack fired **zero**. The rules that
would matter — no `console.log`, read-before-edit, no inline `style=`, never
commit to `main` — fired zero in-repo.

**Mechanically checkable skews trivial.** Part 2 measured ~65% of rules
checkable; this says the useful slice of that 65% is small. *"useful ∩
checkable"* is the unmeasured number, and it's the real Show HN: the checkable
rules aren't the ones you care about.

---

## 2. Result: an authored rule shipped with its verifier beats prose — but the honest version is about crying wolf, not catching more.

Same intent, "stop dangerous recursive deletes," expressed two ways and measured
apples-to-apples (both at session scope):

| | fires | on a real home/root/system path | on benign temp/build cleanup |
|---|---:|---:|---:|
| user prose `never rm -rf` | 23 | 0 | 23 |
| authored pack `SP003` | 0 | 0 | 0 |

The prose rule cries wolf 23 times; the authored rule stays correctly silent.
**Two honesty caveats, both from inspection:**

1. **My first SP003 was also wrong.** It fired twice — on `rm -f /tmp/dbg.db`
   and `rm -f /tmp/test_archive.db`, benign temp-file removals — because the
   pattern matched any absolute path and didn't require the recursive flag.
   Inspecting those two before writing this table is the only reason the table
   is right. The pattern now requires `-r` and excludes temp dirs; a 10-case
   unit self-check guards it (`safety_pack` tests).
2. **There were 0 dangerous deletions in this corpus.** So SP003's *detection*
   of danger is untested here — it's validated by the unit cases, not the
   corpus. What the corpus shows is the precision half: an authored pattern
   doesn't fire on the 23 benign cases the prose rule does.

That is still differentiator #1, measured — just stated as "authored rules don't
cry wolf," which is what the data supports, rather than "authored rules catch
10× more," which it doesn't.

**Safety-pack false-positive sweep (the pack is the surface a new user meets
first).** Because the corpus has 0 dangerous events, every pack hit in it is a
known false alarm by construction — a clean false-positive test bed. But the
sweep found **0 hits**, and that's *not* a pass: this corpus (one dev's frontend
work) contains no force-pushes, no `curl | sh`, no secret-writing, and none of
the benign look-alikes either. The corpus is silent on the pack, so it can't
clear it. Testing the patterns against external benign look-alikes instead found
**four false positives, all fixed**:

| rule | fired on (benign) | fix |
|---|---|---|
| SP001 | `git push --force-with-lease` (the *safe* force-push) | require `--force`/`-f`, exclude `--force-with-lease` |
| SP002 | `curl … | sh` inside a heredoc writing a doc | strip heredoc bodies before matching |
| SP004 | the AWS docs example key `AKIA…EXAMPLE` | negative-lookahead exclude it |
| SP004 | a key in `.env.example` / a test-fixture `.pem` | **destination-scope**: only real credential files, exclude example/test/fixture/doc paths |

SP004 can't be made precise on content (a fixture key and a real key are
pattern-identical), so it now fires only when a secret-shaped string lands in a
real credential file (`.env`, `id_rsa`, `*.pem`, `credentials`, …) outside
example/test paths. **Coverage gap, stated:** a secret hard-coded into a source
file (`config.ts`) no longer fires — precision bought at the cost of that case.

Two of those four I first *mislabeled as true positives* while writing the test —
defending my own rule, the same blind spot again. The external look-alikes are
what caught it; my unit cases wouldn't have (they'd have encoded the same blind
spot, exactly as SP003's own 19 cases missed SP003).

---

## 3. Scope is never inferred from prose.

An earlier version auto-tagged rules matching `rm -rf`/force-push as
`scope: session`. Removed. Reason: promoting an inferred rule to the widest
blast radius is a silent guess about intent — the exact thing the `unresolved`
bucket exists to refuse — and it takes the noisiest, guessy rule and gives it
the most reach. A false fire wearing a safety-rule name is worse than an
ordinary false positive: it teaches distrust of the one category that has to be
believed. And nothing is lost, because the dangerous behavior is already covered
precisely by the authored pack; inference only bought duplicate coverage of the
rules the pack already handles. A safety category the pack misses
(`terraform destroy`, dropping a DB) is a signal to author a pack check, not to
teach the compiler to guess. `scope: session` is something a human writes in
`checks.yaml`; the compiler only ever emits `repo`.

---

## 4. The precision correction, and the habit it exposed.

"Use `rg`" forbids the tool, not the target — so a `grep` invoked in-repo on a
`/tmp` file is a real violation. Correcting that moved held-out precision 86% →
100%. Right call. But notice the shape: I changed the *adjudication rule after
seeing the measurement*. That's the same failure the held-out split guards
against, arriving through the labeling side instead of the data side. §0 is the
fix — the definitions are now frozen ahead of the next corpus, so the next
number is scored by a rule committed blind.

The cwd bug class sits under all of this: a command check that assumes cwd ==
repo root is the general hole (the sandbox-`git commit` catch was one instance).
Session `9ff35ab8` proved it — the transcript sat under one project's dir while the
edits landed in two *other* checkouts (353 and 112 edits). Fixed with recorded `cwd` +
resolvable `cd`; the residual (out-of-repo target via a variable, no `cd`) is
what the unresolved bucket surfaces.

---

## 5. Parser robustness on long, compacted sessions (local sweep)

Everything above is about *judging rules*. This section is about the layer under
that — *reading the transcript* — and it does not change any precision or
violation number above. A rule verdict is only as good as the path the parser
reconstructs, and §4 already showed where that path breaks: a naive walk up
`parentUuid` stops at the last compaction boundary and silently drops the session
before it.

Method, reproducible with `python tests/sweep_parser_robustness.py`. Over the 10
longest real sessions on this machine (3 project dirs; longest 10,420 lines; 3
carried compaction boundaries, 1–4 each), each session is checked for three
things: it parses without raising; main-path turn numbering is contiguous 1..T
with every tool call's turn in range and `seq` strictly increasing; and coverage
is retained across compactions, measured by re-running a naive `parentUuid`-only
walk and comparing its turn count against the bridged parser's.

| session | lines | compactions | naive walk keeps | bridged keeps | dropped by naive |
|---|---:|---:|---:|---:|---:|
| S01 | 10,420 | 4 | 872 | 6,938 | 87% |
| S02 | 4,562 | 2 | 109 | 3,068 | 96% |
| S03 | 3,735 | 1 | 717 | 2,790 | 74% |

The seven non-compacted sessions parse identically either way (naive == bridged)
and all pass. **0 failures across all 10.** The compacted rows are the point: on
S02 a pre-bridge reader keeps 109 of 3,068 turns, so a rule broken in the first
96% of that session would be invisible. The bridge recovers it.

**Residual limitation, stated.** This is 10 sessions from one machine and one
person's usage. It shows the parser survives long and compaction-heavy
transcripts *as they occur here* — up to four compactions and ~10K lines — not
that it survives everyone's (other harness versions, heavier branch/rewind
shapes, sessions longer than any I've run). Like the precision number, the way to
grow it is other people's long sessions, which is a launch dependency, not a
local one.

---

## The number, and why the plan is to ship

- **~1.9 reported violations / 100 turns**, 66 of 67 from one trivial style rule.
- Held-out: **55/55 real in-scope invocations, 1 unresolved** — the instrument
  is honest; the signal is nearly a single rule.
- The safety pack fired 0 (no dangerous behavior occurred), so it's the
  precision-and-never-silent story here, not a detection result.
- **These sessions never ran under this `CLAUDE.md`.** This validates the
  instrument, not agents-breaking-rules-they-were-given.

**The ownership rule (why the compiler isn't hardened further before launch):**
*what ships identically to every user must be right before launch; what varies
per user gets fixed by user reports.* The safety pack lands the same in every
install, so its false positives are ship-blocking and were fixed now. The
compiler's `content` regexes run against code no one here has seen, so their
failures arrive as feedback — which is the rule-governed corpus we're trying to
acquire anyway. Deferring them isn't a punt; it's assigning them to their
correct owner.

Strip the `rg` rule and the corpus produces ~1 finding across 7 sessions. The
go/no-go number for v2/v3 needs sessions that ran *under* a rules file; this
machine has none and no public corpus exists, so the only source is users. That
puts **ship + distribute before the Part 6 number, not after** — and makes the
built-in safety pack likely the only thing a stranger's first run ever sees fire.
The telemetry contract ([`TELEMETRY.md`](TELEMETRY.md), enforced by
`assert_clean()`) is locked now for the same reason. Dogfood interim: a real
[`CLAUDE.md`](CLAUDE.md) governs this repo — a week under it is a genuine n=1.

---

## The real finding is the method, not the number

This corpus scored down to ~1 non-trivial finding, so the violation rate isn't
the story. The story is the instrument plus four traps — each hit, named, and
fixed here — that anyone building this class of tool will hit too:

1. **Precision quoted off the tuning set.** The first "0% false alarms" was
   measured on the sessions the compiler was tuned on. Fix: a held-out split.
2. **Adjudication drifting after seeing results.** The invocation-vs-target
   relabel moved precision 86% → 100% *after* the number was in view — the
   held-out failure arriving through the labeling side. Fix: freeze the
   adjudication rules before scoring (§0).
3. **Authored rules are broader than their author thinks.** SP003 fired on
   `rm -f /tmp/x`; SP004 on the AWS example key and test fixtures — and I twice
   tried to relabel those benign hits as true positives. Fix: test against
   external look-alikes, not author-written unit cases; scope by destination
   when content can't discriminate.
4. **A null result read as validation** — the strongest, because nothing looks
   wrong. The safety-pack sweep returned **0 hits** and that meant *nothing*:
   the instrument was pointed at empty ground (a corpus with no relevant
   traffic), not confirmed clean. This one has replicated across projects — a
   diff-mutation harness once returned a false `0/19` because the transform
   errored the suite rather than because everything was killed. Traps 1–3 are a
   claim resting on an invisible guess; this is a *non*-claim worn as a clean
   bill of health. Fix: before trusting a zero, prove the instrument fires on
   ground truth (here: the external look-alikes; there: a known-live mutant).

The first three are one shape — a claim resting on a guess I couldn't see I was
making. The fourth is the negative of it, and more dangerous for looking like
success. That's the honest write-up — more useful to a reader than any number
this corpus could have produced.

## First real-data application (n=1, pre-launch verification)

Every number above is from synthetic, tuned, or safety-pack-only corpora. The
pre-upload verification produced the first application of the instrument to a
**real, human-authored `CLAUDE.md`** (mutalyze's own dogfood rules — 10 rules,
10 checks incl. safety pack, 4 unsupported) against a **real 1,941-turn
session** (3.2h+ of actual work, spanning a compaction boundary the parser now
bridges). Result:

- **3 `CM002` violations** (read-before-edit, no shell evidence for the path),
- **4 unresolved** (edits to heredoc-authored files — evidence present but not
  cleanly adjudicable),
- **`CM004` held** (the `print()`-in-`mutalyze/` rule now scopes to the
  package and found no debug prints there; the test-harness prints it used to
  flag are correctly out of scope),
- 4 rules unsupported, counted and named.

**Caveat — this is not clean dogfood.** The transcript is a session in which
mutalyze work happened, not one governed end-to-end by mutalyze's `CLAUDE.md`.
So it is *real rules × real session*, not *a session that ran under the rules*.
The clean n=1 still requires working a full session under the rules file and
checking that. But it is a different kind of number than anything above:
produced from inputs neither authored nor tuned for the test — which is exactly
the condition the four traps are about. Notably, the three fixes shipped during
this run (dropped `()`, heredoc author, dropped `Y/`) were all found *because*
real inputs exercised rule shapes the synthetic fixtures didn't.
