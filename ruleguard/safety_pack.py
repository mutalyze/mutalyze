"""Built-in safety pack — checks every repo wants, shipped with their verifier.

These need no `CLAUDE.md`: nobody writes "don't `rm -rf /`" in a rules file, yet
every repo means it. Two things fall out of that:

  1. It's differentiator #1 from the plan — a process/safety-rule pack shipped
     *with* its executable check, so the checkable fraction is 100% by
     construction instead of the ~65% inherited from other people's prose.
  2. It's the "never fully silent" fix for the bottom third of repos: at the
     10th percentile only ~a third of a user's rules compile, so a stranger's
     first run might otherwise say nothing. The pack always has something to say.

Every pack check is scope=session (it applies wherever the agent worked, not
just inside the repo) and high-confidence by design — a noisy safety check is
worse than none. Disable with `--no-safety`.
"""

from __future__ import annotations

from typing import List

from .checks import COMMAND, CONTENT, Check


def builtin_checks() -> List[Check]:
    return [
        Check(
            id="SP001",
            rule="Never force-push (`git push --force` / `-f`).",
            type=COMMAND,
            # --force / -f only. --force-with-lease is the *safe* force-push and
            # must NOT fire (a false positive on a personal branch teaches distrust).
            forbid_pattern=r"git\s+push\b[^\n|;&]*\s(?:--force(?!-with-lease)\b|-f\b)",
            scope="session",
        ),
        Check(
            id="SP002",
            rule="Never pipe a network download straight into a shell (`curl … | sh`).",
            type=COMMAND,
            forbid_pattern=r"(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh)\b",
            scope="session",
        ),
        Check(
            id="SP003",
            rule="Never recursively `rm -rf` a home, root, or system path (or `*`).",
            type=COMMAND,
            # Require the RECURSIVE flag (single-file `rm -f x` is not the danger),
            # then a target that is root/home/glob or an absolute path that is NOT
            # a temp dir. `rm -f /tmp/x.db` and `rm -rf build/` do not match.
            forbid_pattern=(
                r"rm\s+(?:-[a-zA-Z]+\s+)*-[a-zA-Z]*r[a-zA-Z]*\s+(?:-[a-zA-Z]+\s+)*"
                r"(?:/(?!(?:private/)?tmp/|var/folders/)|~|\$HOME\b|\*(?:\s|$))"
            ),
            scope="session",
        ),
        Check(
            id="SP004",
            rule="Never write a private key or cloud credential to a real credential file.",
            type=CONTENT,
            # Content alone can't tell a real key from a fixture — they're
            # pattern-identical. So scope by DESTINATION: only fire when a
            # secret-shaped string lands in a real credential file, and exclude
            # example/test/fixture/doc paths and the AWS docs example key.
            forbid_pattern=(
                r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"
                r"|AKIA(?!IOSFODNN7EXAMPLE)[0-9A-Z]{16}"
            ),
            applies_to=[".env", ".envrc", "*.pem", "*.key", "*.p12", "*.pfx",
                        "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials"],
            exclude_paths=["example", "sample", "template", "fixture", "mock",
                           "dummy", "/test", "test/", "spec", ".md", "node_modules"],
            scope="session",
        ),
    ]
