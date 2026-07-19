# Findings

ruleguard run over the 7 real Claude Code sessions on this machine, with a
representative `CLAUDE.md` plus the built-in safety pack, every finding
hand-inspected. Four things came out — the first is the one worth publishing;
the last is why the plan is to ship, not measure more.

---

## 1. The headline isn't the violation rate. It's *what* the checkable rules are.

Of 90 reported violations across 3,400 turns, **66 come from one rule: "use `rg`
not `grep`."** Another 24 are `rm -rf` (22 from a user "never rm -rf" rule, 2
from the safety pack). The rules that would actually matter — no `console.log`,
read-before-edit, no inline `style=`, never commit to `main` — fired **zero**
in-repo.

That's the result, not noise. **The rules that are cheap to check mechanically
are the trivial ones.** A tool-name swap and a dangerous-command grep are
trivially checkable; "did it read the file before editing," "is this refactor
behavior-preserving," "did it weaken a test to go green" are the rules people
care about, and they're either unsupported or never fire.

Part 2 measured that ~65% of rules are *mechanically checkable*. True — but
that 65% skews trivial. The unmeasured number is **useful ∩ checkable**, and
this corpus says it's small. That's the Show HN: *"I built the rule-checker
everyone keeps proposing, ran it on real sessions, and the checkable rules
aren't the ones you care about."*

---

## 2. Precision: the fix was the verdict, not the scope.

The first pass claimed "0% false alarms," measured on the same sessions it was
tuned on — grading my own homework. Held-out validation (tune on 2 sessions,
inspect 3 cold) then looked like ~86% precision. **That 86% was also wrong** —
in the *opposite* direction. I was judging a `grep` finding by where grep
*searched* (a `/tmp` file → "false alarm") instead of where grep was *invoked*
(inside the repo → a real break of "use rg"). The rule forbids the tool, not
the target.

Fixed with two ideas, kept separate on purpose (repo-scoping was doing two
jobs and only one was right):

- **`scope` is a per-rule field** (`repo` default, `session` for safety rules).
  It answers *"is this rule about this repo?"* — nothing about path resolution.
  `rm -rf` / force-push compile to `scope: session` and fire wherever the agent
  worked; `rg`-not-`grep` stays repo-scoped. Hand-editable like everything else
  in `checks.yaml`.
- **An `unresolved` bucket** answers *"can I locate this command?"* — a command
  whose directory is built from a shell variable (`cd "$D" && grep …`) is
  **neither violated nor held**. It's reported apart ("2 commands couldn't be
  located, turns 88 and 341"), so a claim never rests on evidence I can't stand
  behind, and the dangerous case is never silently dropped. Same chokepoint
  shape as verifying a claim before rendering it.

**Re-measured on the 3 held-out sessions: 68 violations, 68 verified as real
in-scope command invocations, 0 unresolved.** Precision on what it *claims* is
100%; the ambiguous cases (3 across the corpus) sit in the third bucket. n is
still tiny — this is the instrument working, not a population estimate.

---

## 3. The cwd bug class, and where scope leaves a residual.

A command check that assumes cwd == repo root is the general hole (the earlier
"git commit in a sandbox isn't a commit to main" was one instance). Session
`9ff35ab8`'s transcript sits under the *nugudom* project dir but its edits went
to `~/kudzu/src` (353) and `~/cellamind/src` (112) — the agent roamed. Fixes,
all from recorded data: content/ordering scoped to files under the repo;
commands located by recorded per-event `cwd` plus resolvable `cd`. As scoping
landed the total moved 139 → 119 → 90 (the last step also *added* session-scoped
safety findings, so it isn't a pure drop).

Residual, surfaced not hidden: a command that never `cd`s but targets an
out-of-repo path through a variable is located by its invocation dir, which is
correct for invocation-rules (`rg` vs `grep`) and imperfect for target-rules.
That's a rule-semantics edge, now visible rather than silently wrong.

---

## 4. Strip the one style rule and the signal is ~zero. That's why we ship.

Take away `rg`-vs-`grep` and 24 findings remain — almost all `rm -rf`, and of
those the 22 from the user's blunt "never rm -rf" are mostly benign temp-dir
cleanup (the targeted safety-pack `SP003` fired just **2**, on real `~`/absolute
paths). High-confidence, actually-actionable signal on this corpus is roughly
two findings in seven sessions.

That's not a knock — it's consistent with the rest — but it makes the plan-order
flip not a preference but the only option:

- **The go/no-go number needs sessions that ran *under* a rules file.** This
  machine has none, and nobody's transcripts are public. The only source is
  users, so **ship + distribute (Part 5 steps 1–2) comes before the Part 6
  number**. More runs on this contaminated n=7 won't move the answer.
- **The safety pack may be the only thing a new user ever sees fire.** If
  hand-written rules mostly produce trivial or noisy findings, a built-in,
  session-scoped, high-confidence pack (`rm -rf` of home/root, force-push,
  `curl | sh`, secrets-to-disk) is what keeps a stranger's first run from
  saying nothing — and it fixes the "bottom third of repos" risk directly.
  It's also differentiator #1: rules shipped *with* their verifier.
- **The telemetry contract is locked now**, because the moment a stranger runs
  this and sends a number back we must already know what we may receive. See
  [`TELEMETRY.md`](TELEMETRY.md); enforced by `assert_clean()`, not promised.

Cheapest honest interim: a real [`CLAUDE.md`](CLAUDE.md) now governs this repo —
dogfood under it for a week for a genuine **n=1**, which beats the current n=0.
