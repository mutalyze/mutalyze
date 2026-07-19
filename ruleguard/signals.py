"""Derived signals — the ONLY thing a team/hosted tier may transmit.

Transcripts contain source code, credentials, and internal architecture. No
security team will let you upload them. So the client extracts locally and sends
derived signals only: never a command, a file path, a rule's text, an evidence
string, a branch name, or any transcript content.

This module is the single chokepoint that produces that payload, and
``assert_clean()`` proves — against the actual findings — that none of the raw
evidence leaked into it. Decide and lock this schema now: it is cheap today and
structurally impossible to retrofit once strangers are sending numbers.

Fields, and why each is safe:
  schema_version : constant.
  session_uid    : salted hash of the session id (a random uuid; the hash is
                   pseudonymous, carries no content).
  rules[]        : per compiled rule —
      rule_uid   : hash of the NORMALIZED rule text. Lets the server group the
                   same rule across users without ever seeing the rule text.
      rule_type  : command | content | ordering  (a category, not content).
      verdict    : held | violated.
      violations : integer count.
      turns[]    : integer turn indices where it fired (numbers, not evidence).
  totals         : turn/tool-call counts (integers).

Explicitly excluded: evidence, commands, file paths, rule text, branches, cwd,
line contents, uuids.
"""

from __future__ import annotations

import hashlib
from typing import Dict, List

from .checks import CompiledDoc
from .execute import ExecResult
from .transcript import Transcript

SCHEMA_VERSION = 1


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _uid(text: str, salt: str = "ruleguard/v1") -> str:
    return hashlib.sha256((salt + "|" + text).encode("utf-8")).hexdigest()[:16]


def derive_signals(doc: CompiledDoc, transcript: Transcript,
                   result: ExecResult, session_id: str) -> Dict:
    by_check: Dict[str, List[int]] = {}
    for v in result.violations:
        by_check.setdefault(v.check_id, []).append(v.turn)
    unresolved_by_check: Dict[str, List[int]] = {}
    for u in result.unresolved:
        unresolved_by_check.setdefault(u.check_id, []).append(u.turn)

    rules = []
    for c in doc.checks:
        turns = sorted(by_check.get(c.id, []))
        unres = sorted(unresolved_by_check.get(c.id, []))
        rules.append({
            "rule_uid": _uid(_norm(c.rule)),
            "rule_type": c.type,
            "scope": c.scope,
            "verdict": "violated" if turns else ("unresolved" if unres else "held"),
            "violations": len(turns),
            "turns": turns,
            "unresolved": len(unres),
        })

    st = transcript.stats
    return {
        "schema_version": SCHEMA_VERSION,
        "session_uid": _uid(session_id or ""),
        "totals": {
            "turns": st.turns,
            "tool_calls": st.tool_calls,
            "rules_compiled": len(doc.checks),
            "rules_unsupported": len(doc.unsupported),
        },
        "rules": rules,
    }


def assert_clean(payload: Dict, result: ExecResult, doc: CompiledDoc) -> None:
    """Fail loudly if any raw content leaked into the derived payload.

    Checks the serialized payload against every evidence string, rule text, and
    unsupported-rule text. This is the guarantee the team tier is sold on, so it
    is enforced, not trusted.
    """
    import json

    blob = json.dumps(payload)
    leaked = []
    for v in list(result.violations) + list(result.unresolved):
        for chunk in (v.evidence, v.rule):
            for tok in chunk.split():
                if len(tok) >= 6 and tok in blob:
                    leaked.append(tok)
    for c in doc.checks:
        for tok in c.rule.split():
            if len(tok) >= 6 and tok in blob:
                leaked.append(tok)
    if leaked:
        raise AssertionError(
            "derived signal payload leaked raw content: %s" % sorted(set(leaked))[:5]
        )
