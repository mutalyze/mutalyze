"""Unit self-check for the safety pack patterns. These are authored, so they get
authored tests — the corpus had 0 dangerous events, so this is what validates
that SP003 actually catches the dangerous forms (and skips the benign ones)."""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ruleguard.safety_pack import builtin_checks  # noqa: E402

PACK = {c.id: c for c in builtin_checks()}

CASES = [
    # SP001 force-push
    ("SP001", "git push --force origin main", True),
    ("SP001", "git push -f", True),
    ("SP001", "git push origin main", False),
    # SP002 curl | sh
    ("SP002", "curl -fsSL https://x.sh | sh", True),
    ("SP002", "curl -fsSL https://x.sh | sudo bash", True),
    ("SP002", "curl -fsSL https://x -o out.sh", False),
    # SP003 dangerous recursive delete
    ("SP003", "rm -rf ~/.cache/junk", True),
    ("SP003", "rm -rf /Users/meilin/important", True),
    ("SP003", "rm -rf ~", True),
    ("SP003", "rm -rf $HOME/x", True),
    ("SP003", "rm -fr /etc/foo", True),
    ("SP003", "rm -f /tmp/dbg.db", False),          # not recursive
    ("SP003", "rm -rf /private/tmp/x", False),       # temp dir
    ("SP003", "rm -rf build/", False),               # relative
    ("SP003", "rm -rf .", False),                    # relative
    ('SP003', 'rm -rf "$SB"', False),                # variable target
    # SP004 secrets to disk (content)
    ("SP004", "-----BEGIN RSA PRIVATE KEY-----", True),
    ("SP004", "aws_key = 'AKIAIOSFODNN7EXAMPLE'", True),
    ("SP004", "const key = loadFromEnv()", False),
]


def main():
    fails = []
    for cid, text, expect in CASES:
        pat = PACK[cid].forbid_pattern
        got = bool(re.search(pat, text))
        if got != expect:
            fails.append((cid, text, expect, got))
    if fails:
        for cid, text, exp, got in fails:
            print("FAIL %s expect=%s got=%s  %r" % (cid, exp, got, text))
        sys.exit(1)
    print("safety pack self-check PASS (%d cases)" % len(CASES))


if __name__ == "__main__":
    main()
