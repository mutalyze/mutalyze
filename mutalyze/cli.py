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
from .store import DEFAULT_BUNDLE as STORE_DEFAULT_BUNDLE
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


def _out(msg: str = "") -> None:
    sys.stdout.write(msg.rstrip("\n") + "\n")


def cmd_rules_import(args: argparse.Namespace) -> int:
    from .store import import_rules_file, load_store, save_store

    store = load_store()
    target = args.path or (args.repo or find_repo_root(os.getcwd()))
    result, err = import_rules_file(store, target, bundle=args.bundle)
    if err is not None:
        _err("ERROR  %s" % err)
        return 2
    if result is None or (not result.added and not result.skipped):
        _err("NOTE  No normative rule lines found in %s" % target)
        return 0

    save_store(store)
    _out("Imported from %s" % result.source)
    _out("  bundle: %s   →  %s" % (args.bundle, store.path))
    if result.added:
        _out("")
        _out("ADDED (%d)" % len(result.added))
        for rule in result.added:
            _out("  %s  %s" % (rule.id, rule.text))
    if result.skipped:
        _out("")
        _out("SKIPPED (%d — already stored)" % len(result.skipped))
        for text, reason in result.skipped:
            _out("  %s  (%s)" % (_clip(text), reason))
    return 0


def cmd_rules_list(args: argparse.Namespace) -> int:
    from .store import checkability, find_conflicts, find_duplicates, load_store

    store = load_store()
    rules = store.in_bundle(args.bundle) if args.bundle else store.rules
    if not rules:
        where = "bundle '%s'" % args.bundle if args.bundle else store.path
        _err("NOTE  No rules stored in %s\n"
             "      Import some:  mutalyze rules import" % where)
        return 0

    _out("Store: %s" % store.path)
    _out("Rules: %d in %d bundle(s)" % (len(rules), len({r.bundle for r in rules})))
    for bundle in (b for b in store.bundles() if not args.bundle or b == args.bundle):
        in_b = [r for r in rules if r.bundle == bundle]
        if not in_b:
            continue
        _out("")
        _out("[%s]" % bundle)
        for rule in in_b:
            ok, detail = checkability(rule.text)
            mark = "check:%s" % detail if ok else "unsupported"
            flag = "" if rule.status == "active" else " (%s)" % rule.status
            _out("  %s  %s%s" % (rule.id, rule.text, flag))
            if args.verbose:
                _out("        %s" % mark)

    dupes = find_duplicates(store.active())
    conflicts = find_conflicts(store.active())
    if dupes:
        _out("")
        _out("DUPLICATES (%d — same rule in more than one bundle)" % len(dupes))
        for first, second in dupes:
            _out("  %s ↔ %s  %s" % (first.id, second.id, _clip(second.text)))
    if conflicts:
        _out("")
        _out("CONFLICTS (%d — flagged, never auto-resolved)" % len(conflicts))
        for c in conflicts:
            _out("  %s" % c.describe())
    return 0


def cmd_rules_add(args: argparse.Namespace) -> int:
    from .store import add_rule, checkability, load_store, save_store

    store = load_store()
    rule, reason = add_rule(store, args.text, bundle=args.bundle, source="manual")
    if rule is None:
        _err("NOTE  Not added — %s" % reason)
        return 0
    save_store(store)
    ok, detail = checkability(rule.text)
    _out("Added %s to bundle '%s'" % (rule.id, rule.bundle))
    _out("  %s" % rule.text)
    _out("  %s" % ("mechanically checkable (%s)" % detail if ok
                   else "not mechanically checkable — %s" % detail))
    return 0


def cmd_rules_rm(args: argparse.Namespace) -> int:
    from .store import load_store, remove_rule, save_store

    store = load_store()
    rule = remove_rule(store, args.id)
    if rule is None:
        _err("ERROR  No rule matching '%s' (ambiguous prefixes are refused)" % args.id)
        return 2
    save_store(store)
    _out("Removed %s from '%s'" % (rule.id, rule.bundle))
    _out("  %s" % rule.text)
    return 0


def cmd_rules_compose(args: argparse.Namespace) -> int:
    from .store import compose, is_generated, load_store

    store = load_store()
    if not store.rules:
        _err("ERROR  The store is empty — nothing to compose.\n"
             "       Import a rules file first:  mutalyze rules import")
        return 3

    missing = [b for b in (args.bundle or []) if not store.in_bundle(b)]
    if missing:
        _err("ERROR  No such bundle(s): %s\n"
             "       Known bundles: %s" % (", ".join(missing), ", ".join(store.bundles())))
        return 2

    result = compose(store, bundles=args.bundle or None, title=args.title)

    if not args.output:
        sys.stdout.write(result.text)
        _err("NOTE  %d rules from [%s]. Nothing written — pass -o FILE to write."
             % (len(result.used), ", ".join(result.bundles)))
        _warn_compose(result)
        return 0

    dest = os.path.abspath(os.path.expanduser(args.output))
    if os.path.exists(dest) and not is_generated(dest) and not args.force:
        _err("ERROR  %s exists and was not generated by mutalyze.\n"
             "       Refusing to overwrite hand-written rules. Re-run with --force "
             "to replace it,\n       or write elsewhere with -o." % dest)
        return 2
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(result.text)
    _out("Wrote %s" % dest)
    _out("  %d rules from [%s]" % (len(result.used), ", ".join(result.bundles)))
    _warn_compose(result)
    return 0


def _warn_compose(result) -> None:
    """Duplicates and conflicts are surfaced, never silently applied."""
    for first, second in result.duplicates:
        _err("  dropped duplicate %s (already present as %s): %s"
             % (second.id, first.id, _clip(second.text)))
    for c in result.conflicts:
        _err("  CONFLICT %s" % c.describe())
    if result.conflicts:
        _err("  Conflicting rules were BOTH kept — resolve them in the store.")


def _print_help(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return 0


def _clip(text: str, width: int = 72) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


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

    # -- rules: the persistent, cross-project rule store --------------------
    r = sub.add_parser("rules", help="manage the persistent rule store (import / list / compose)")
    rsub = r.add_subparsers(dest="rules_command")
    # bare `mutalyze rules` shows its own help rather than failing on a missing func
    r.set_defaults(func=lambda _args, _p=r: _print_help(_p))

    ri = rsub.add_parser("import", help="import rules from a CLAUDE.md / AGENTS.md into the store")
    ri.add_argument("path", nargs="?", help="rules file or repo dir (default: this repo)")
    ri.add_argument("--repo", help="repo root to import from")
    ri.add_argument("--bundle", default=STORE_DEFAULT_BUNDLE,
                    help="bundle to file these under (default: %s)" % STORE_DEFAULT_BUNDLE)
    ri.set_defaults(func=cmd_rules_import)

    rl = rsub.add_parser("list", help="list stored rules, with duplicate and conflict flags")
    rl.add_argument("--bundle", help="only this bundle")
    rl.add_argument("--verbose", action="store_true", help="show whether each rule is checkable")
    rl.set_defaults(func=cmd_rules_list)

    ra = rsub.add_parser("add", help="add one rule by hand")
    ra.add_argument("text", help="the rule, as you'd write it in a rules file")
    ra.add_argument("--bundle", default=STORE_DEFAULT_BUNDLE,
                    help="bundle to add to (default: %s)" % STORE_DEFAULT_BUNDLE)
    ra.set_defaults(func=cmd_rules_add)

    rr = rsub.add_parser("rm", help="remove a rule by id")
    rr.add_argument("id", help="rule id (or an unambiguous prefix)")
    rr.set_defaults(func=cmd_rules_rm)

    rc = rsub.add_parser("compose", help="stack bundles into one rules file an agent will auto-load")
    rc.add_argument("--bundle", action="append",
                    help="bundle to include, repeatable — order is precedence (default: all)")
    rc.add_argument("-o", "--output", help="write to this file (default: stdout, writes nothing)")
    rc.add_argument("--title", default="Project rules", help="H1 title for the composed file")
    rc.add_argument("--force", action="store_true",
                    help="overwrite an output file mutalyze did not generate")
    rc.set_defaults(func=cmd_rules_compose)

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
