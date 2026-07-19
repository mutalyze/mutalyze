# Findings

ruleguard over the 7 real Claude Code sessions on this machine, with a
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
Session `9ff35ab8` proved it — transcript under the *nugudom* project dir, edits
in `~/kudzu/src` (353) and `~/cellamind/src` (112). Fixed with recorded `cwd` +
resolvable `cd`; the residual (out-of-repo target via a variable, no `cd`) is
what the unresolved bucket surfaces.

---

## The number, and why the plan is to ship

- **~1.9 reported violations / 100 turns**, 66 of 67 from one trivial style rule.
- Held-out: **55/55 real in-scope invocations, 1 unresolved** — the instrument
  is honest; the signal is nearly a single rule.
- The safety pack fired 0 (no dangerous behavior occurred), so it's the
  precision-and-never-silent story here, not a detection result.
- **These sessions never ran under this `CLAUDE.md`.** This validates the
  instrument, not agents-breaking-rules-they-were-given.

Strip the `rg` rule and the corpus produces ~1 finding across 7 sessions. The
go/no-go number for v2/v3 needs sessions that ran *under* a rules file; this
machine has none and no public corpus exists, so the only source is users. That
puts **ship + distribute before the Part 6 number, not after** — and makes the
built-in safety pack likely the only thing a stranger's first run ever sees fire.
The telemetry contract ([`TELEMETRY.md`](TELEMETRY.md), enforced by
`assert_clean()`) is locked now for the same reason. Dogfood interim: a real
[`CLAUDE.md`](CLAUDE.md) governs this repo — a week under it is a genuine n=1.
