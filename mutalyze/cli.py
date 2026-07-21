"""mutalyze CLI — `mutalyze check`."""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from . import __version__
from .checks import CONTENT, CompiledDoc, load_checks
from .compile_rules import compile_rules, find_rules_files
from .discover import (
    code_extension_mix,
    find_repo_root,
    newest_session,
    project_transcript_dir,
)
from .execute import execute
from .report import render_json, render_text
from .safety_pack import builtin_checks
from .transcript import Transcript

MIN_CHECKS = 5


class Refuse(Exception):
    """Raised to refuse loudly instead of printing a clean zero-violation report."""


def _err(msg: str) -> None:
    sys.stderr.write(msg.rstrip() + "\n")


def _count_normative_lines(repo_root: str) -> int:
    from .compile_rules import extract_candidates

    total = 0
    for f in find_rules_files(repo_root):
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                total += len(extract_candidates(fh.read()))
        except OSError:
            pass
    return total


def _load_or_compile(repo_root: str, recompile: bool) -> CompiledDoc:
    checks_path = os.path.join(repo_root, ".mutalyze", "checks.yaml")

    if os.path.exists(checks_path) and not recompile:
        return load_checks(checks_path)

    rules_files = find_rules_files(repo_root)
    if not rules_files:
        raise Refuse(
            "ERROR  No CLAUDE.md or AGENTS.md found in %s\n"
            "       (and no CLAUDE.md symlink stub could be resolved).\n"
            "       Nothing to check against — not reporting a result." % repo_root
        )

    doc = compile_rules(repo_root)
    if doc is None:
        raise Refuse("ERROR  Could not read the rules file(s).")

    os.makedirs(os.path.dirname(checks_path), exist_ok=True)
    with open(checks_path, "w", encoding="utf-8") as fh:
        fh.write(doc.to_yaml())
    return doc


def _guard_compilation(doc: CompiledDoc, repo_root: str) -> Optional[str]:
    """Return a loud warning when a rules file compiled too thinly to trust — a
    bad compile otherwise looks identical to a compliant session. With the
    safety pack running underneath we warn rather than exit, so we're never
    fully silent; the warning still prints above the report."""
    if len(doc.checks) < MIN_CHECKS:
        return (
            "WARNING  Only %d checks compiled from %s (below %d). A low check\n"
            "         count looks like a compliant session — treat user-rule\n"
            "         results as thin and edit .mutalyze/checks.yaml by hand."
            % (len(doc.checks), doc.source or "the rules file", MIN_CHECKS)
        )

    mix = code_extension_mix(repo_root)
    ts_py = sum(mix.get(e, 0) for e in ("ts", "tsx", "js", "jsx", "py"))
    total_code = sum(mix.get(e, 0) for e in (
        "ts", "tsx", "js", "jsx", "py", "rs", "go", "java", "rb", "c", "cpp", "php"))
    content_checks = sum(1 for c in doc.checks if c.type == CONTENT)
    if total_code >= 10 and ts_py >= 0.5 * max(total_code, 1) and content_checks == 0:
        return (
            "WARNING  This repo is mostly TypeScript/Python but zero `content`\n"
            "         checks compiled — the largest checkable category is missing.\n"
            "         Edit .mutalyze/checks.yaml by hand."
        )
    return None


def cmd_check(args: argparse.Namespace) -> int:
    repo_root = os.path.abspath(args.repo) if args.repo else find_repo_root(os.getcwd())

    # 1) Resolve the session transcript.
    session_path = args.session
    if session_path:
        if not os.path.exists(session_path):
            _err("ERROR  Session file not found: %s" % session_path)
            return 2
    else:
        session_path = newest_session(repo_root)
        if not session_path:
            _err(
                "ERROR  No session transcript found for this repo.\n"
                "       Looked in: %s\n"
                "       Pass one explicitly:  mutalyze check <path-to.jsonl>"
                % project_transcript_dir(repo_root)
            )
            return 2

    # 2) Compile user rules (may be absent) + load the built-in safety pack.
    pack = [] if args.no_safety else builtin_checks()
    warning = None
    try:
        doc = _load_or_compile(repo_root, args.recompile)
        warning = _guard_compilation(doc, repo_root)
    except Refuse as r:
        # No rules file. The safety pack still runs — never fully silent — unless
        # it's disabled, in which case there is genuinely nothing to check.
        if not pack:
            _err(str(r))
            return 3
        warning = "NOTE  No CLAUDE.md/AGENTS.md found — running the built-in safety pack only."
        doc = CompiledDoc(source="(none)", checks=[], unsupported=[])

    all_checks = doc.checks + pack
    combined = CompiledDoc(source=doc.source, checks=all_checks, unsupported=doc.unsupported)
    rules_found = _count_normative_lines(repo_root)

    # 3) Execute.
    transcript = Transcript(session_path)
    result = execute(all_checks, transcript, repo_root=repo_root)

    # 4) Report.
    if args.signals:
        import json as _json
        from .signals import assert_clean, derive_signals

        session_id = os.path.splitext(os.path.basename(session_path))[0]
        payload = derive_signals(combined, transcript, result, session_id)
        assert_clean(payload, result, combined)  # prove no raw content leaked
        sys.stdout.write(_json.dumps(payload, indent=2) + "\n")
    else:
        if warning:
            _err(warning)
        if args.json:
            sys.stdout.write(render_json(session_path, transcript, combined, result, rules_found) + "\n")
        else:
            sys.stdout.write(render_text(session_path, transcript, combined, result, rules_found, args.verbose))

    return 1 if result.violations else 0


def _prepare_checks(repo_root: str, args: argparse.Namespace):
    """Compile user rules + load the safety pack, or refuse loudly. Shared by
    check and watch so both obey 'refuse rather than report zero'. Returns
    (all_checks, combined_doc, rules_found, warning)."""
    pack = [] if getattr(args, "no_safety", False) else builtin_checks()
    try:
        doc = _load_or_compile(repo_root, getattr(args, "recompile", False))
        warning = _guard_compilation(doc, repo_root)
    except Refuse as r:
        if not pack:
            raise
        warning = "NOTE  No CLAUDE.md/AGENTS.md found — running the built-in safety pack only."
        doc = CompiledDoc(source="(none)", checks=[], unsupported=[])
    all_checks = doc.checks + pack
    combined = CompiledDoc(source=doc.source, checks=all_checks, unsupported=doc.unsupported)
    return all_checks, combined, _count_normative_lines(repo_root), warning


def cmd_watch(args: argparse.Namespace) -> int:
    import tempfile

    from .discover import project_transcript_dir
    from .watch import _Ink, Watcher

    repo_root = os.path.abspath(args.repo) if args.repo else find_repo_root(os.getcwd())
    try:
        all_checks, combined, rules_found, warning = _prepare_checks(repo_root, args)
    except Refuse as r:
        _err(str(r))
        return 3
    if warning:
        _err(warning)

    ink = _Ink(on=sys.stdout.isatty() and not args.no_color)
    watcher = Watcher(repo_root, all_checks, combined, rules_found, ink)

    if args.replay:
        if not os.path.exists(args.replay):
            _err("ERROR  Replay file not found: %s" % args.replay)
            return 2
        fd, dst = tempfile.mkstemp(prefix="mutalyze_watch_", suffix=".jsonl")
        os.close(fd)
        try:
            return watcher.run_replay(args.replay, dst, speed=args.speed, split_lines=args.split_lines)
        finally:
            try:
                os.unlink(dst)
            except OSError:
                pass

    tdir = os.path.dirname(args.session) if args.session else project_transcript_dir(repo_root)
    return watcher.run_live(tdir, args.session)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mutalyze",
        description="Catch your coding agent breaking your own CLAUDE.md rules.",
    )
    p.add_argument("--version", action="version", version="mutalyze %s" % __version__)
    sub = p.add_subparsers(dest="command")

    c = sub.add_parser("check", help="audit a session against the rules file")
    c.add_argument("session", nargs="?", help="path to a .jsonl transcript (default: newest for this repo)")
    c.add_argument("--repo", help="repo root (default: nearest .git above cwd)")
    c.add_argument("--json", action="store_true", help="machine-readable output")
    c.add_argument("--signals", action="store_true",
                   help="emit derived signals only (no transcript content) — the team-tier payload")
    c.add_argument("--verbose", action="store_true", help="list held and unsupported rules individually")
    c.add_argument("--recompile", action="store_true", help="re-run rule compilation, overwriting checks.yaml")
    c.add_argument("--no-safety", action="store_true", help="disable the built-in safety pack")
    c.set_defaults(func=cmd_check)

    w = sub.add_parser("watch", help="follow a live session and report violations as they happen")
    w.add_argument("session", nargs="?", help="a specific .jsonl to follow (default: newest for this repo)")
    w.add_argument("--repo", help="repo root (default: nearest .git above cwd)")
    w.add_argument("--no-safety", action="store_true", help="disable the built-in safety pack")
    w.add_argument("--recompile", action="store_true", help="re-run rule compilation, overwriting checks.yaml")
    w.add_argument("--no-color", action="store_true", help="disable ANSI color")
    w.add_argument("--replay", metavar="FILE",
                   help="replay a recorded .jsonl instead of a live session (test/demo)")
    w.add_argument("--speed", type=float, default=0.0,
                   help="replay: seconds to pause between lines (0 = as fast as possible)")
    w.add_argument("--split-lines", action="store_true",
                   help="replay: write some lines in two writes, to exercise partial-line handling")
    w.set_defaults(func=cmd_watch)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
