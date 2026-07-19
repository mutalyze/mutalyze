"""Render the check result as a human report or JSON."""

from __future__ import annotations

import json
import os
from typing import List

from .checks import CompiledDoc
from .execute import ExecResult
from .transcript import Transcript, duration_human


def _session_id(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def render_text(
    session_path: str,
    transcript: Transcript,
    doc: CompiledDoc,
    result: ExecResult,
    rules_found: int,
    verbose: bool = False,
) -> str:
    st = transcript.stats
    violations = result.violations
    unresolved = result.unresolved
    sid = _session_id(session_path)
    lines: List[str] = []
    lines.append("")
    lines.append("Session: %s  (%s turns, %s)" % (sid[:8], f"{st.turns:,}", duration_human(st)))
    lines.append("Rules:   %d found · %d checks (incl. safety pack) · %d unsupported"
                 % (rules_found, len(doc.checks), len(doc.unsupported)))
    if st.side_branch_lines or st.sidechain_lines:
        extra = []
        if st.side_branch_lines:
            extra.append("%d off-path lines (rewinds)" % st.side_branch_lines)
        if st.sidechain_lines:
            extra.append("%d subagent lines" % st.sidechain_lines)
        lines.append("Path:    main path numbered; " + ", ".join(extra) + " kept but not numbered")
    lines.append("")

    violated_ids = {v.check_id for v in violations}

    if violations:
        lines.append("VIOLATIONS (%d)" % len(violations))
        lines.append("")
        for v in violations:
            lines.append("  turn %-6d %-6s %s" % (v.turn, v.check_id, v.rule))
            lines.append("  %s %s" % (" " * 13, v.evidence))
            lines.append("")
    else:
        lines.append("VIOLATIONS (0) — every check held.")
        lines.append("")

    # The third bucket: findings we can't stand behind, never silently dropped.
    if unresolved:
        lines.append("UNRESOLVED (%d — could not locate the command; may or may not apply)" % len(unresolved))
        for u in unresolved:
            lines.append("  turn %-6d %-6s %s" % (u.turn, u.check_id, u.rule))
            lines.append("  %s %s" % (" " * 13, u.evidence))
        lines.append("")

    held = [c for c in doc.checks if c.id not in violated_ids]
    if verbose:
        lines.append("HELD (%d rules, no violations)" % len(held))
        for c in held:
            lines.append("  %-6s %s" % (c.id, c.rule))
        lines.append("")
        lines.append("UNSUPPORTED (%d rules)" % len(doc.unsupported))
        for u in doc.unsupported:
            lines.append("  %s" % u["rule"])
            lines.append("        ↳ %s" % u["reason"])
        lines.append("")
    else:
        lines.append("HELD (%d rules, no violations)" % len(held))
        lines.append("UNSUPPORTED (%d rules — see .ruleguard/checks.yaml, or --verbose)"
                     % len(doc.unsupported))
        lines.append("")

    return "\n".join(lines)


def render_json(
    session_path: str,
    transcript: Transcript,
    doc: CompiledDoc,
    result: ExecResult,
    rules_found: int,
) -> str:
    st = transcript.stats
    violations = result.violations
    violated_ids = {v.check_id for v in violations}
    out = {
        "session": _session_id(session_path),
        "session_path": session_path,
        "stats": {
            "turns": st.turns,
            "tool_calls": st.tool_calls,
            "duration": duration_human(st),
            "total_lines": st.total_lines,
            "side_branch_lines": st.side_branch_lines,
            "sidechain_lines": st.sidechain_lines,
            "branches": sorted(st.branches),
        },
        "rules": {
            "found": rules_found,
            "compiled": len(doc.checks),
            "unsupported": len(doc.unsupported),
            "source": doc.source,
        },
        "violations": [v.to_dict() for v in violations],
        "unresolved": [u.to_dict() for u in result.unresolved],
        "held": [c.id for c in doc.checks if c.id not in violated_ids],
        "unsupported": doc.unsupported,
        # headline metrics (Part 6): reported as *reported*, before hand-validation.
        "metrics": {
            "reported_violations": len(violations),
            "reported_violations_per_100_turns": round(
                (len(violations) / st.turns * 100) if st.turns else 0.0, 3
            ),
            "unresolved": len(result.unresolved),
            "rules_broken_at_least_once": len(violated_ids),
            "rules_compiled": len(doc.checks),
        },
    }
    return json.dumps(out, indent=2)
