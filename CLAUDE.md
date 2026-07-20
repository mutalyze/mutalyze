# Working rules for this repo

These are real rules for developing mutalyze itself — not a demo. The point is
to work under them for a while and then run `mutalyze check` on the session, so
we finally have a transcript that was *actually governed* by a rules file
(n=1 beats the current n=0). Keep them honest and keep them few.

## Workflow

- Never commit directly to `main` — branch first.
- Read a file before editing it.
- Run `./.venv/bin/python tests/make_fixture.py` after changing anything in `mutalyze/` and before committing.
- Use `rg` (not `grep`) for searching the codebase.

## Code

- No `print()` for debugging in `mutalyze/` — the CLI writes to stdout deliberately, debug noise does not.
- Never use a bare `except:` — catch a specific exception.
- No `# type: ignore` comments.
- Never use `eval(` or `exec(` on transcript-derived data.

## Design invariants (not all mechanically checkable — that's the point)

- Phase 2 (`execute.py`) must never call an LLM or the network. Every violation
  stays deterministic and citable.
- Never transmit transcript content off the machine; only the derived signals in
  `signals.py`. See [TELEMETRY.md](TELEMETRY.md).
- Prefer marking a rule `unsupported` over emitting a check that might false-fire.
