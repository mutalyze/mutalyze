"""`mutalyze watch` — follow a live Claude Code session and report violations
as they happen, instead of after the fact.

Two decisions shape this file (see the v1.5 brief):

  1. **Tail the transcript, don't install a hook.** The session JSONL is
     append-only and already on disk; we poll it and run the SAME checks
     incrementally. No mutation of ~/.claude/settings.json, zero config, and we
     reuse the parser already validated against real transcripts. The cost is a
     stated blind spot: permission-denied tool calls are killed by the harness
     before they reach the transcript, so watch mode never sees them.

  2. **Re-evaluate the current main path on each change and diff.** The path
     itself is kept as incremental state (we extend it on append and only
     re-walk the diverged tail on a rewind — never re-trace the whole file). But
     the checks are re-run over the current main-path tool calls each change,
     because that is what makes rewind-withdrawal and dedup correct and simple:
     a finding whose line has left the main path simply isn't in the new result.

Nothing here touches the compiler, the adjudication rules, or the check types.
It adds a way to *run* the existing checks live; it changes nothing about what
they mean. No LLM, ever — every finding still cites a line you can open.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Dict, Iterator, List, Optional, Tuple

from .checks import Check, CompiledDoc
from .execute import ExecResult, execute
from .transcript import ToolCall, Transcript

# A live leaf never retracts more than this many turns at once; a bigger jump is
# a transcript artifact (orphan node / compaction transition), not the tip.
_MAX_RETRACT = 40


# ---------------------------------------------------------------------------
# Incremental tailer: parse appended lines, keep the main path as live state.
# ---------------------------------------------------------------------------
class LiveTranscript:
    """Exposes the three things execute() needs — tool_calls(), created_paths,
    session_cwd — but maintained incrementally as the file grows. A partial
    final line (the harness mid-write) is buffered, never parsed."""

    def __init__(self) -> None:
        self._buf = ""                       # unterminated trailing bytes, held back
        self._line_no = 0
        self.by_key: Dict[str, dict] = {}
        self.created_paths: set = set()
        self._cwd_counts: Dict[str, int] = {}
        self._leaf: Optional[str] = None     # key of the current active leaf
        # main path, kept incrementally
        self.main_uuids: List[str] = []
        self.main_index: Dict[str, int] = {}
        self.turn_of: Dict[str, int] = {}

    # -- ingest -------------------------------------------------------------
    def feed_text(self, chunk: str) -> bool:
        """Add raw text; parse only newline-terminated lines. Returns True if
        the main path changed (something worth re-checking happened)."""
        if not chunk:
            return False
        self._buf += chunk
        parts = self._buf.split("\n")
        self._buf = parts.pop()              # trailing partial (or "") stays buffered
        changed = False
        for raw in parts:
            self._line_no += 1
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except (ValueError, TypeError):
                continue                      # skip malformed; a partial can't reach here
            if not isinstance(obj, dict):
                continue
            key = self._ingest(obj, self._line_no)
            if key is not None:               # a candidate leaf — try to accept it
                changed |= self._accept_leaf(key)
        return changed

    def _ingest(self, obj: dict, line_no: int) -> bool:
        cwd = obj.get("cwd")
        if isinstance(cwd, str):
            self._cwd_counts[cwd] = self._cwd_counts.get(cwd, 0) + 1
        res = obj.get("toolUseResult")
        if isinstance(res, dict) and res.get("type") == "create":
            fp = res.get("filePath")
            if isinstance(fp, str):
                self.created_paths.add(fp)

        uuid = obj.get("uuid")
        key = uuid or ("line:%d" % line_no)
        etype = obj.get("type")
        sidechain = bool(obj.get("isSidechain"))
        node = {
            "key": key, "uuid": uuid, "parent": obj.get("parentUuid"),
            "logical": obj.get("logicalParentUuid"), "etype": etype,
            "sidechain": sidechain, "line_no": line_no,
            "branch": obj.get("gitBranch"), "ts": obj.get("timestamp"),
            "cwd": cwd, "tool_uses": [],
        }
        if etype == "assistant":
            msg = obj.get("message")
            if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                for b in msg["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_use" \
                            and isinstance(b.get("name"), str) and isinstance(b.get("input"), dict):
                        node["tool_uses"].append((b["name"], b["input"]))
        self.by_key[key] = node
        # A candidate leaf is the last non-sidechain message node — but only
        # _accept_leaf decides whether it actually belongs on the main path.
        if not sidechain and etype in ("user", "assistant"):
            return key
        return None

    # -- main-path maintenance (incremental) --------------------------------
    def _accept_leaf(self, candidate: str) -> bool:
        """Try to make `candidate` the new leaf. Walk back until we rejoin the
        established path (a rewind reconnects at its rewind point; a compaction
        reconnects via logicalParentUuid) or reach a root. If it does NOT
        reconnect and we already have a path, it's a disconnected orphan/queued
        node or a compaction segment whose bridge line hasn't arrived yet — do
        NOT abandon the main path for it. That is what stops a live session from
        churning every finding on each stray line. Returns True iff the path
        actually changed. The walk stops at the junction, so it costs the length
        of the diverged tail, never the whole file."""
        seg: List[str] = []
        seen: set = set()
        junction: Optional[int] = None
        cur = self.by_key.get(candidate)
        while cur is not None:
            k = cur["key"]
            if k in seen:                    # cycle guard
                break
            seen.add(k)
            if k in self.main_index:
                junction = self.main_index[k]
                break
            seg.append(k)
            nxt = self.by_key.get(cur["parent"]) if cur["parent"] else None
            if nxt is None and cur["logical"]:   # bridge a compaction boundary
                nxt = self.by_key.get(cur["logical"])
            cur = nxt

        if junction is None and self.main_uuids:
            return False                     # disconnected island — ignore, keep the path

        seg.reverse()                        # root->leaf order
        keep = (junction + 1) if junction is not None else 0
        # A live leaf advances, or rewinds a little — it does not retract hundreds
        # of turns at once. A candidate that would is a transcript artifact: an
        # orphan/queued `user` node, or a mid-compaction transition line that
        # branches near the root. Ignore it (keep the established path); the exit
        # summary, a full re-parse, always reflects the true final path anyway.
        if self.main_uuids and (len(self.main_uuids) - keep) > _MAX_RETRACT:
            return False
        if self.main_uuids[keep:] == seg:    # no actual change (re-seen same leaf)
            return False
        for k in self.main_uuids[keep:]:
            self.main_index.pop(k, None)
            self.turn_of.pop(k, None)
        self.main_uuids = self.main_uuids[:keep] + seg
        for i in range(keep, len(self.main_uuids)):
            k = self.main_uuids[i]
            self.main_index[k] = i
            self.turn_of[k] = i + 1          # turns are 1-indexed along the path
        self._leaf = candidate
        return True

    # -- execute()-compatible surface ---------------------------------------
    @property
    def session_cwd(self) -> Optional[str]:
        return max(self._cwd_counts, key=self._cwd_counts.get) if self._cwd_counts else None

    def tool_calls(self) -> Iterator[ToolCall]:
        seq = 0
        for key in self.main_uuids:
            node = self.by_key.get(key)
            if node is None or node["etype"] != "assistant":
                continue
            for name, tool_input in node["tool_uses"]:
                yield ToolCall(
                    turn=self.turn_of.get(key, 0), seq=seq, name=name, input=tool_input,
                    git_branch=node["branch"], timestamp=node["ts"],
                    line_no=node["line_no"], line_id=node["uuid"], cwd=node["cwd"],
                )
                seq += 1


# ---------------------------------------------------------------------------
# Replay harness (DoD item 8): feed a recorded transcript into a file line by
# line, so the whole watch path — real file I/O, partial lines, compaction,
# rewinds — is testable without a live agent.
# ---------------------------------------------------------------------------
class ReplayWriter:
    def __init__(self, src_path: str, dst_path: str) -> None:
        with open(src_path, "r", encoding="utf-8", errors="replace") as fh:
            self.lines = fh.readlines()      # each keeps its trailing "\n"
        self.dst_path = dst_path
        self.i = 0
        open(dst_path, "w", encoding="utf-8").close()   # start empty

    @property
    def done(self) -> bool:
        return self.i >= len(self.lines)

    def feed(self, n: int = 1) -> int:
        end = min(self.i + n, len(self.lines))
        with open(self.dst_path, "a", encoding="utf-8") as fh:
            fh.write("".join(self.lines[self.i:end]))
        fed = end - self.i
        self.i = end
        return fed

    def feed_split(self, frac: float = 0.5) -> None:
        """Append the next line in two writes with no newline on the first —
        exercises the partial-line buffer."""
        if self.done:
            return
        line = self.lines[self.i]
        cut = max(1, int(len(line) * frac))
        with open(self.dst_path, "a", encoding="utf-8") as fh:
            fh.write(line[:cut])
            fh.flush()
        with open(self.dst_path, "a", encoding="utf-8") as fh:
            fh.write(line[cut:])
        self.i += 1


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
class _Ink:
    def __init__(self, on: bool) -> None:
        self.on = on

    def _c(self, s: str, code: str) -> str:
        return "\033[%sm%s\033[0m" % (code, s) if self.on else s

    def red(self, s):    return self._c(s, "1;31")
    def mag(self, s):    return self._c(s, "35")
    def green(self, s):  return self._c(s, "32")
    def dim(self, s):    return self._c(s, "2")
    def yellow(self, s): return self._c(s, "33")


def _is_safety(check_id: str) -> bool:
    return check_id.startswith("SP")


# ---------------------------------------------------------------------------
# The watcher
# ---------------------------------------------------------------------------
class Watcher:
    def __init__(self, repo_root: str, all_checks: List[Check], combined: CompiledDoc,
                 rules_found: int, ink: _Ink, out=None, err=None) -> None:
        self.repo_root = repo_root
        self.all_checks = all_checks
        self.combined = combined
        self.rules_found = rules_found
        self.ink = ink
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        # session-end checks can't be adjudicated live — held for the exit summary
        self.session_end_ids = {c.id for c in all_checks if c.require_present or c.require_after}
        self.live = LiveTranscript()
        # (check_id, line_id) -> "active" | "withdrawn"
        self.reported: Dict[Tuple[str, Optional[str]], str] = {}
        self.records: Dict[Tuple[str, Optional[str]], object] = {}
        self.n_fired = 0

    # -- per-change evaluation ---------------------------------------------
    def process_change(self) -> None:
        result = execute(self.all_checks, self.live, repo_root=self.repo_root)
        current: Dict[Tuple[str, Optional[str]], object] = {}
        for v in result.violations:
            if v.check_id in self.session_end_ids:
                continue                     # session-absence — decided only at exit
            current[(v.check_id, v.line_id)] = v
        for u in result.unresolved:
            current[("~" + u.check_id, u.line_id)] = u   # namespace so it can't collide

        # new findings
        for k, finding in current.items():
            if k not in self.reported:
                self.reported[k] = "active"
                self.records[k] = finding
                self._print_new(finding, unresolved=k[0].startswith("~"))
            elif self.reported[k] == "withdrawn":
                self.reported[k] = "active"
                self._print_reconfirm(finding)

        # retracted findings: a reported line that left the main path (rewind)
        for k, status in list(self.reported.items()):
            if status == "active" and k not in current:
                self.reported[k] = "withdrawn"
                self._print_withdrawn(self.records[k])

    # -- printing -----------------------------------------------------------
    def _print_new(self, f, unresolved: bool) -> None:
        i = self.ink
        if unresolved:
            head = "  %s  turn %-6d %-6s %s" % (i.yellow("? unresolved"), f.turn, f.check_id, f.rule)
        elif _is_safety(f.check_id):
            head = "  %s  turn %-6d %-6s %s" % (i.red("⚠ DANGER"), f.turn, f.check_id, i.red(f.rule))
        else:
            head = "  %s  turn %-6d %-6s %s" % (i.mag("✗ mutation"), f.turn, f.check_id, f.rule)
            self.n_fired += 1
        self.out.write(head + "\n")
        self.out.write("                %s\n" % i.dim(f.evidence))
        self.out.flush()

    def _print_withdrawn(self, f) -> None:
        i = self.ink
        self.out.write("  %s  %-6s %s\n" % (
            i.dim("↩ withdrawn"), f.check_id,
            i.dim("turn %d left the main path (rewind) — finding retracted" % f.turn)))
        self.out.flush()

    def _print_reconfirm(self, f) -> None:
        i = self.ink
        self.out.write("  %s  turn %-6d %-6s %s\n" % (
            i.green("✓ re-confirmed"), f.turn, f.check_id, i.dim(f.rule)))
        self.out.flush()

    # -- summary (identical to `mutalyze check`) ---------------------------
    def summary(self, session_path: str) -> None:
        from .report import render_text
        if not session_path or not os.path.exists(session_path):
            return
        transcript = Transcript(session_path)
        result = execute(self.all_checks, transcript, repo_root=self.repo_root)
        self.err.write("\n" + self.ink.dim("── session summary " + "─" * 42) + "\n")
        self.out.write(render_text(session_path, transcript, self.combined, result, self.rules_found))
        self.out.flush()

    # -- drivers ------------------------------------------------------------
    def run_replay(self, src_path: str, dst_path: str, speed: float = 0.0,
                   split_lines: bool = False) -> int:
        """Feed a recorded transcript into dst_path line by line, tailing it the
        same way live mode does. Deterministic; used by tests and `--replay`."""
        writer = ReplayWriter(src_path, dst_path)
        self.err.write(self.ink.dim("replaying %s  (quiet until something fires)\n"
                                    % os.path.basename(src_path)))
        offset = 0
        toggle = False
        while not writer.done:
            if split_lines and toggle:
                writer.feed_split()
            else:
                writer.feed(1)
            toggle = not toggle
            offset = self._drain(dst_path, offset)
            if speed:
                time.sleep(speed)
        self._drain(dst_path, offset)
        self.summary(dst_path)
        return 1 if self.n_fired else 0

    def run_live(self, transcript_dir: str, session_path: Optional[str],
                 poll_interval: float = 0.25) -> int:
        """Tail the newest transcript in a project dir, following into a new
        session file if one appears. Runs until Ctrl-C."""
        current = session_path or _newest(transcript_dir)
        if current is None:
            self.err.write("waiting for a session to start in %s …\n" % transcript_dir)
        offset = 0
        self.err.write(self.ink.dim("watching %s  (quiet until something fires · Ctrl-C to stop)\n"
                                    % (os.path.basename(current) if current else transcript_dir)))
        try:
            while True:
                if session_path is None:      # follow the newest session
                    newest = _newest(transcript_dir)
                    if newest and newest != current:
                        if current:
                            self._drain(current, offset)
                            self.summary(current)
                            self.err.write(self.ink.dim("\n→ new session detected; following it\n"))
                        current, offset = newest, 0
                        self._reset_for_new_session()
                if current:
                    offset = self._drain(current, offset)
                time.sleep(poll_interval)
        except KeyboardInterrupt:
            self.err.write("\n")
            self.summary(current)
            return 1 if self.n_fired else 0

    def _reset_for_new_session(self) -> None:
        """A new session file means a fresh transcript: drop all per-session
        state so nothing carries across, and no finding double-reports."""
        self.live = LiveTranscript()
        self.reported.clear()
        self.records.clear()
        self.n_fired = 0

    def _drain(self, path: str, offset: int) -> int:
        """Read new bytes from `offset`, feed them, re-check if anything changed."""
        try:
            size = os.path.getsize(path)
        except OSError:
            return offset
        if size < offset:                     # file shrank/rotated — restart it
            offset = 0
            self.live = LiveTranscript()
        if size == offset:
            return offset
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            chunk = fh.read()
            offset = fh.tell()
        if self.live.feed_text(chunk):
            self.process_change()
        return offset


def _newest(transcript_dir: str) -> Optional[str]:
    if not os.path.isdir(transcript_dir):
        return None
    files = [os.path.join(transcript_dir, f) for f in os.listdir(transcript_dir)
             if f.endswith(".jsonl")]
    if not files:
        return None
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return files[0]
