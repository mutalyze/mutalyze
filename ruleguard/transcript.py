"""Streaming reader for Claude Code session transcripts (.jsonl).

Schema notes (observed by dumping real transcripts, NOT assumed):

  - One self-contained JSON object per line, append-only.
  - Every object has a top-level ``type``: assistant, user, system,
    permission-mode, mode, ai-title, last-prompt, file-history-snapshot,
    attachment, queue-operation, frame-link, agent-name, ...
  - ``assistant`` objects carry ``message.content`` — a list of blocks. Blocks
    with ``type == "tool_use"`` have ``name`` and ``input`` (the tool payload).
  - Message events carry ``uuid`` and ``parentUuid``. The file is a TREE, not a
    flat list: rewinds and edited prompts create sibling branches, and subagent
    runs are appended inline with ``isSidechain == true``. So a line's position
    is NOT its turn number.
  - ``gitBranch`` is stamped on essentially every event — authoritative
    recorded branch state, so branch-conditional rules read it directly.
  - Write/Edit tool *results* live on a following ``user`` line under
    ``toolUseResult``; ``toolUseResult.type == "create"`` marks a Write that
    created a new file (vs ``"update"``).

Turn numbering: we trace the main path via parent pointers, number turns along
that path only, and keep each line's own ``uuid`` (line_id) plus file line
number so any citation resolves to the exact line regardless of renumbering.
Side branches and sidechains are counted but not numbered (kept for v4).

Memory: two streaming passes. Pass 1 holds only a compact per-line index
(uuid / parent / type), never the payloads. Pass 2 re-streams and emits tool
calls for main-path lines only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple


@dataclass
class ToolCall:
    """A single tool invocation on the main conversational path."""

    turn: int  # position along the main path (branch-safe)
    seq: int  # 0-indexed order among main-path tool calls (for ordering windows)
    name: str
    input: Dict[str, Any]
    git_branch: Optional[str]
    timestamp: Optional[str]
    line_no: int  # 1-indexed file line — grep-verifiable
    line_id: Optional[str]  # the line's own uuid — survives renumbering
    cwd: Optional[str]  # recorded working directory for this call (authoritative)


@dataclass
class TranscriptStats:
    total_lines: int = 0
    parse_errors: int = 0
    turns: int = 0  # main-path message nodes
    tool_calls: int = 0  # main-path tool calls
    side_branch_lines: int = 0  # message lines off the main path (rewinds)
    sidechain_lines: int = 0  # subagent lines (isSidechain)
    branches: Set[str] = field(default_factory=set)
    first_timestamp: Optional[str] = None
    last_timestamp: Optional[str] = None


@dataclass
class _Node:
    line_no: int
    uuid: Optional[str]
    parent: Optional[str]
    etype: Optional[str]
    sidechain: bool


class Transcript:
    def __init__(self, path: str):
        self.path = path
        self.stats = TranscriptStats()
        self.created_paths: Set[str] = set()
        self.session_cwd: Optional[str] = None  # most common recorded cwd
        self._main_uuids: List[str] = []
        self._turn_of: Dict[str, int] = {}
        self._main_set: Set[str] = set()
        self._used_fallback = False
        self._index_and_plan()

    # -- pass 1: lightweight index + main-path plan + created-file set --------
    def _index_and_plan(self) -> None:
        nodes: List[_Node] = []
        by_uuid: Dict[str, _Node] = {}
        cwd_counts: Dict[str, int] = {}
        with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
            for line_no, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                self.stats.total_lines += 1
                try:
                    obj = json.loads(raw)
                except (ValueError, TypeError):
                    self.stats.parse_errors += 1
                    continue
                if not isinstance(obj, dict):
                    continue

                cwd = obj.get("cwd")
                if isinstance(cwd, str):
                    cwd_counts[cwd] = cwd_counts.get(cwd, 0) + 1

                # created-file set (from tool results, anywhere in the file)
                res = obj.get("toolUseResult")
                if isinstance(res, dict) and res.get("type") == "create":
                    fp = res.get("filePath")
                    if isinstance(fp, str):
                        self.created_paths.add(fp)

                node = _Node(
                    line_no=line_no,
                    uuid=obj.get("uuid"),
                    parent=obj.get("parentUuid"),
                    etype=obj.get("type"),
                    sidechain=bool(obj.get("isSidechain")),
                )
                nodes.append(node)
                if node.uuid:
                    by_uuid[node.uuid] = node
                if node.sidechain:
                    self.stats.sidechain_lines += 1

        # Trace the main path: last non-sidechain message node is the active
        # leaf; walk parent pointers to the root.
        leaf: Optional[_Node] = None
        for node in reversed(nodes):
            if not node.sidechain and node.etype in ("user", "assistant") and node.uuid:
                leaf = node
                break

        main_uuids: List[str] = []
        if leaf is not None:
            seen: Set[str] = set()
            cur: Optional[_Node] = leaf
            while cur is not None and cur.uuid and cur.uuid not in seen:
                seen.add(cur.uuid)
                main_uuids.append(cur.uuid)
                cur = by_uuid.get(cur.parent) if cur.parent else None
            main_uuids.reverse()

        if not main_uuids:
            # Fallback for transcripts without usable parent pointers: treat
            # every non-sidechain message line as the main path in file order.
            self._used_fallback = True
            for node in nodes:
                if not node.sidechain and node.etype in ("user", "assistant"):
                    key = node.uuid or ("line:%d" % node.line_no)
                    main_uuids.append(key)

        if cwd_counts:
            self.session_cwd = max(cwd_counts, key=cwd_counts.get)

        self._main_uuids = main_uuids
        self._main_set = set(main_uuids)
        self._turn_of = {u: i for i, u in enumerate(main_uuids, start=1)}
        self.stats.turns = len(main_uuids)

        # side-branch message lines = non-sidechain message nodes off main path
        for node in nodes:
            if node.sidechain or node.etype not in ("user", "assistant"):
                continue
            key = node.uuid or ("line:%d" % node.line_no)
            if key not in self._main_set:
                self.stats.side_branch_lines += 1

    # -- pass 2: emit main-path tool calls -----------------------------------
    def tool_calls(self) -> Iterator[ToolCall]:
        seq = 0
        with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
            for line_no, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "assistant":
                    continue

                key = obj.get("uuid") or ("line:%d" % line_no)
                turn = self._turn_of.get(key)
                if turn is None:
                    continue  # off the main path — kept in file, not numbered

                branch = obj.get("gitBranch")
                if branch:
                    self.stats.branches.add(branch)
                ts = obj.get("timestamp")
                if ts:
                    if self.stats.first_timestamp is None:
                        self.stats.first_timestamp = ts
                    self.stats.last_timestamp = ts

                message = obj.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name")
                    tool_input = block.get("input")
                    if not isinstance(name, str) or not isinstance(tool_input, dict):
                        continue
                    self.stats.tool_calls += 1
                    yield ToolCall(
                        turn=turn,
                        seq=seq,
                        name=name,
                        input=tool_input,
                        git_branch=branch,
                        timestamp=ts,
                        line_no=line_no,
                        line_id=obj.get("uuid"),
                        cwd=obj.get("cwd"),
                    )
                    seq += 1


def duration_human(stats: TranscriptStats) -> str:
    a, b = stats.first_timestamp, stats.last_timestamp
    if not a or not b:
        return "unknown duration"
    try:
        from datetime import datetime

        def parse(s: str) -> "datetime":
            return datetime.fromisoformat(s.replace("Z", "+00:00"))

        secs = int((parse(b) - parse(a)).total_seconds())
        if secs < 0:
            return "unknown duration"
        h, rem = divmod(secs, 3600)
        m, _ = divmod(rem, 60)
        return f"{h}h {m}m" if h else f"{m}m"
    except Exception:
        return "unknown duration"
