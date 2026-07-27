# Security Policy

## Reporting a vulnerability

Please report suspected security issues **privately** — do not open a public issue.

- Preferred: open a private advisory at
  <https://github.com/mutalyze/mutalyze/security/advisories/new>
- Or email: `[SECURITY CONTACT EMAIL]`

We aim to acknowledge reports within a few business days and to share a
remediation timeline after triage.

## Scope

- The `mutalyze` command-line tool (this repository).
- The mutalyze website (the `site/` directory) and its deployment.

## Out of scope

- Issues that require an already-compromised local machine, or malicious local
  input the user supplies to their own run.
- Volumetric denial-of-service against the static site — that is mitigated at the
  CDN / hosting platform layer, not in site code.

## Design properties relevant to security

These are enforced by the project's own rules and validated design invariants:

- **No data exfiltration.** The analysis phase (`execute.py`) is deterministic
  and never calls the network or an LLM. Only the derived signals in
  `signals.py` are ever produced; transcript content is never transmitted off
  the machine. See [TELEMETRY.md](TELEMETRY.md).
- **No execution of transcript-derived data.** The tool never runs `eval()` or
  `exec()` on transcript content.
- **The website is static and hardened.** No backend, no database, no user
  input. A strict Content-Security-Policy, HSTS, and anti-clickjacking headers
  are applied in [`site/vercel.json`](site/vercel.json).

## Supported versions

| Version        | Supported     |
| -------------- | ------------- |
| latest `main`  | ✅             |
| older tags     | best-effort   |
