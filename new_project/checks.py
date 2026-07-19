"""The compiled-check data model and the .new-project/checks.yaml (de)serializer.

A Check is the executable form of one natural-language rule. checks.yaml is
written to be human-readable and hand-editable — a user correcting a bad
compilation is a feature, not a failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

# The three supported check types.
COMMAND = "command"
CONTENT = "content"
ORDERING = "ordering"


@dataclass
class Check:
    id: str
    rule: str  # original natural-language text, kept verbatim (v3 re-injects it)
    type: str

    # command / content: any literal substring present => violation
    forbid: List[str] = field(default_factory=list)
    # command / content: regex search hit => violation
    forbid_pattern: Optional[str] = None
    # informational: the sanctioned alternative, surfaced in the report
    require_instead: Optional[str] = None
    # command only: a substring/regex that MUST appear at least once in the
    # session; violation if it never does.
    require_present: Optional[str] = None

    # command only: only evaluate when the current git branch equals this
    when_branch: Optional[str] = None

    # content only: shell globs (matched against the file's basename); empty
    # means "applies to every Write/Edit".
    applies_to: List[str] = field(default_factory=list)

    # ordering only. A `trigger` tool call must be accompanied by either:
    #   - a `require_before` tool call within `within_turns` prior turns
    #     (optionally on the `same_path`), or
    #   - a `require_after` action later in the session. When `scope` is
    #     "session_end", it must follow the LAST trigger.
    # require_after may be a bare tool name ("Bash") or "Bash: <substr>".
    trigger: Optional[str] = None
    require_before: Optional[str] = None
    require_after: Optional[str] = None
    same_path: bool = False
    within_turns: int = 50
    scope: Optional[str] = None

    def to_yaml_obj(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"id": self.id, "rule": self.rule, "type": self.type}
        if self.forbid:
            out["forbid"] = self.forbid
        if self.forbid_pattern:
            out["forbid_pattern"] = self.forbid_pattern
        if self.require_instead:
            out["require_instead"] = self.require_instead
        if self.require_present:
            out["require_present"] = self.require_present
        if self.when_branch:
            out["when_branch"] = self.when_branch
        if self.applies_to:
            out["applies_to"] = self.applies_to
        if self.trigger:
            out["trigger"] = self.trigger
        if self.require_before:
            out["require_before"] = self.require_before
        if self.require_after:
            out["require_after"] = self.require_after
        if self.same_path:
            out["same_path"] = self.same_path
        if self.type == ORDERING and self.require_before and self.within_turns != 50:
            out["within_turns"] = self.within_turns
        if self.scope:
            out["scope"] = self.scope
        return out


@dataclass
class CompiledDoc:
    source: str
    checks: List[Check] = field(default_factory=list)
    unsupported: List[Dict[str, str]] = field(default_factory=list)
    version: int = 1

    def to_yaml(self) -> str:
        doc: Dict[str, Any] = {
            "version": self.version,
            "source": self.source,
            "checks": [c.to_yaml_obj() for c in self.checks],
        }
        if self.unsupported:
            doc["unsupported"] = self.unsupported
        header = (
            "# new-project compiled checks — generated from your rules file.\n"
            "# This file is meant to be edited. Fix a bad rule, delete a noisy\n"
            "# one, or move a rule between `checks` and `unsupported` by hand.\n"
        )
        return header + yaml.safe_dump(doc, sort_keys=False, width=100, allow_unicode=True)


def load_checks(path: str) -> CompiledDoc:
    with open(path, "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    checks: List[Check] = []
    for raw in doc.get("checks") or []:
        if not isinstance(raw, dict):
            continue
        checks.append(
            Check(
                id=str(raw.get("id", "")),
                rule=str(raw.get("rule", "")),
                type=str(raw.get("type", "")),
                forbid=list(raw.get("forbid", []) or []),
                forbid_pattern=raw.get("forbid_pattern"),
                require_instead=raw.get("require_instead"),
                require_present=raw.get("require_present"),
                when_branch=raw.get("when_branch"),
                applies_to=list(raw.get("applies_to", []) or []),
                trigger=raw.get("trigger"),
                require_before=raw.get("require_before"),
                require_after=raw.get("require_after"),
                same_path=bool(raw.get("same_path", False)),
                within_turns=int(raw.get("within_turns", 50) if raw.get("within_turns") is not None else 50),
                scope=raw.get("scope"),
            )
        )
    unsupported = [
        {"rule": str(u.get("rule", "")), "reason": str(u.get("reason", ""))}
        for u in (doc.get("unsupported") or [])
        if isinstance(u, dict)
    ]
    return CompiledDoc(
        source=str(doc.get("source", "")),
        checks=checks,
        unsupported=unsupported,
        version=int(doc.get("version", 1) or 1),
    )
