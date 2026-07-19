# Findings

Run of the v1 detector over the 7 real Claude Code sessions on this machine,
with a representative 7-rule `CLAUDE.md`, every finding hand-inspected. Three
things came out of it — the third is the one worth publishing.

---

## 1. The headline isn't the violation rate. It's *what* the checkable rules are.

Of 70 findings across 3,129 turns, **66 come from one rule: "use `rg` not
`grep`."** Another 4 are "never `rm -rf`." The other three compiled rules —
no `console.log`, read-before-edit, no inline `style=` — produced **zero**
in-repo findings.

That is not noise, it's the result. **The rules that are cheap to check
mechanically are the trivial ones.** A tool-name swap and a dangerous-command
grep are trivially checkable; "did the agent read the file before editing it,"
"is this refactor actually behavior-preserving," "did it weaken a test to go
green" are the rules that matter, and they are either unsupported or almost
never fire.

Part 2 of the brief measured that ~65% of rules are *mechanically checkable*.
True — but that 65% is skewed toward the cheap end. The number nobody has
measured is **useful ∩ checkable**, and this corpus suggests it's much smaller.
That's a better Show HN than any violation rate: *"I built the rule-compliance
checker everyone keeps proposing, ran it on real sessions, and found the
checkable rules aren't the ones you care about."*

| Compiled rule | in-repo findings | character |
|---|---:|---|
| use `rg` not `grep` | 66 | trivial (style) |
| never `rm -rf` | 4 | trivial (safety) |
| no `console.log` | 0 | would matter — never fired in-repo |
| read before edit | 0 | would matter — never fired in-repo |
| never commit to `main` | 0 | matters — never fired (all commits were out-of-repo) |

---

## 2. The "0% false alarms" from the first pass was grading my own homework.

The first report claimed 0% false alarms on these 7 sessions. But I *tuned the
compiler against those same 7 sessions and then measured on them* — of course
it came back clean; I'd just fixed everything it got wrong. Real precision was
still unknown.

So: **held-out validation.** Tuning was driven entirely by two sessions
(`9ff35ab8`, `1158ee04`). I never inspected individual findings in the other
three (`fc2fbbe1`, `bf9daec8`, `f4be6b9f`) until after the code was frozen, then
hand-checked every finding in them cold.

**Held-out result: 56 findings, ≈48 genuine in-repo violations, ≈8 false
(≈86% precision).** Not 0%. Every error is the same class — a `grep`/`rm`
running on an out-of-repo path (`/tmp`, `~/cellamind`, `Downloads`) reached via
a shell variable or pipe that the repo-scoper can't resolve. n=7 (really n=3
held-out) makes this weak either way, but it's honest and it cost nothing but
restraint. The lesson generalizes: **never quote a precision number measured on
the data you tuned against.**

---

## 3. Command checks assume cwd == repo root. That bug is wider than branches.

The first pass had a nice catch: a `git commit` inside a script that `cd`s into
a sandbox isn't a commit to `main`. But that's one instance of a general hole —
**any command check that assumes the working directory is the repo the rules
govern.** The held-out data proved it: session `9ff35ab8`'s transcript lives
under the *nugudom* project dir, but its edits landed in `~/kudzu/src` (353) and
`~/cellamind/src` (112) — the agent wandered across repos, and the checker was
judging kudzu edits against nugudom's rules.

Fixes applied, all using recorded data rather than guesses:

- **content/ordering** scoped to files under the repo root (absolute paths in
  the transcript make this exact);
- **commands** scoped by the recorded per-event `cwd`, and skipped when the
  command `cd`s to a resolvable path outside the repo.

Effect on the total, as each scoping fix landed: **139 → 119 → 70.** The
"violation rate" nearly halved once out-of-repo activity stopped counting. The
number was inflated, and now it is less so.

**Residual, documented not hidden:** a command that never `cd`s but targets an
out-of-repo path through a variable (`SB=/tmp/x; rm -rf "$SB"`) still evaluates.
Resolving that needs shell-variable tracking; it's the source of the ~14%
held-out false alarms. A design question falls out of it too: *safety* rules
(`rm -rf`) arguably should fire everywhere, while *style/workflow* rules
(`rg` vs `grep`) are repo-scoped — v1 scopes both.

---

## The number, stated honestly

- **≈2.2 reported violations / 100 turns**, ~94% of them one trivial style rule.
- **≈86% precision on held-out sessions** (not the in-sample 0%), all errors from
  command-location fuzziness.
- **These sessions never ran under this `CLAUDE.md`** — the rules were applied
  retroactively, so this measures the instrument, not agents breaking rules they
  were given. This machine has zero real `CLAUDE.md`-governed sessions.

## What this changes about the plan

The go/no-go number for v2/v3 requires sessions that *ran under* a real rules
file. This machine can't make them and nobody else's transcripts are public, so
the only source is **users running the tool** — which puts "ship free +
distribute" (Part 5 steps 1–2) *before* the Part 6 number, not after.

Two consequences, both acted on:

1. **Stop measuring, start shipping.** More runs on this contaminated n=7 won't
   move the answer.
2. **The derived-signals schema is urgent now, not "before the first customer."**
   The moment a stranger runs this and sends a number back, we must already know
   what we're allowed to receive. See [`TELEMETRY.md`](TELEMETRY.md).

Cheapest honest interim: a real `CLAUDE.md` now lives in this repo
([`CLAUDE.md`](CLAUDE.md)); dogfood under it for a week for a real **n=1** — which
beats the current **n=0** of rule-governed sessions.
