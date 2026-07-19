"""cellamind CLI — `cellamind check`."""

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
    checks_path = os.path.join(repo_root, ".cellamind", "checks.yaml")

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


def _guard_compilation(doc: CompiledDoc, repo_root: str) -> None:
    """Refuse rather than report zero — a bad compile looks identical to a
    compliant session."""
    if len(doc.checks) < MIN_CHECKS:
        raise Refuse(
            "ERROR  Only %d checks compiled from %s (minimum %d).\n"
            "       Not reporting a result — a low check count looks\n"
            "       identical to a compliant session.\n"
            "       See .cellamind/checks.yaml and edit by hand."
            % (len(doc.checks), doc.source or "the rules file", MIN_CHECKS)
        )

    mix = code_extension_mix(repo_root)
    ts_py = sum(mix.get(e, 0) for e in ("ts", "tsx", "js", "jsx", "py"))
    total_code = sum(mix.get(e, 0) for e in (
        "ts", "tsx", "js", "jsx", "py", "rs", "go", "java", "rb", "c", "cpp", "php"))
    content_checks = sum(1 for c in doc.checks if c.type == CONTENT)
    if total_code >= 10 and ts_py >= 0.5 * max(total_code, 1) and content_checks == 0:
        raise Refuse(
            "ERROR  This repo is mostly TypeScript/Python but zero `content`\n"
            "       checks compiled — the largest checkable category is missing.\n"
            "       The compilation is probably too shallow to trust.\n"
            "       See .cellamind/checks.yaml and edit by hand."
        )


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
                "       Pass one explicitly:  cellamind check <path-to.jsonl>"
                % project_transcript_dir(repo_root)
            )
            return 2

    # 2) Compile (or load cached) checks, with refuse-rather-than-zero guards.
    try:
        doc = _load_or_compile(repo_root, args.recompile)
        _guard_compilation(doc, repo_root)
    except Refuse as r:
        _err(str(r))
        return 3

    rules_found = _count_normative_lines(repo_root)

    # 3) Execute.
    transcript = Transcript(session_path)
    violations = execute(doc.checks, transcript)

    # 4) Report.
    if args.json:
        sys.stdout.write(render_json(session_path, transcript, doc, violations, rules_found) + "\n")
    else:
        sys.stdout.write(render_text(session_path, transcript, doc, violations, rules_found, args.verbose))

    return 1 if violations else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cellamind",
        description="Catch your coding agent breaking your own CLAUDE.md rules.",
    )
    p.add_argument("--version", action="version", version="cellamind %s" % __version__)
    sub = p.add_subparsers(dest="command")

    c = sub.add_parser("check", help="audit a session against the rules file")
    c.add_argument("session", nargs="?", help="path to a .jsonl transcript (default: newest for this repo)")
    c.add_argument("--repo", help="repo root (default: nearest .git above cwd)")
    c.add_argument("--json", action="store_true", help="machine-readable output")
    c.add_argument("--verbose", action="store_true", help="list held and unsupported rules individually")
    c.add_argument("--recompile", action="store_true", help="re-run rule compilation, overwriting checks.yaml")
    c.set_defaults(func=cmd_check)
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
