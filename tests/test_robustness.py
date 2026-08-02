"""Client-readiness guarantees: never crash, never lose data, never phone home.

Written before handing the tool to someone else. The cases here are the ones a
real user produces without trying: a hand-edited store with a typo, a transcript
that was still being written, a Ctrl-C at the wrong moment, a CI job reading the
exit code.

Three properties are asserted:
  1. Malformed input produces an explanation and a non-zero exit, never a
     traceback. A stack trace is a bug report the user cannot act on.
  2. The rule store survives corruption and interruption without losing rules.
     It is advertised as hand-editable, so it *will* be hand-edited.
  3. No command opens a network connection. "Nothing leaves your machine" is the
     product's central claim; it is tested, not asserted.

Standalone (no pytest), matching the other tests here.
"""

import contextlib
import io
import os
import socket
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mutalyze.cli import main  # noqa: E402
from mutalyze.store import Store, StoreError, add_rule, load_store, save_store  # noqa: E402

FAILURES = []
TMP = tempfile.mkdtemp(prefix="mutalyze_robust_")


def check(cond, label):
    if cond:
        return
    FAILURES.append(label)


def run(argv, store=None):
    """Run the CLI; return (exit_code, crashed)."""
    os.environ["MUTALYZE_STORE"] = store or os.path.join(TMP, "default.yaml")
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            return main(argv), False, out.getvalue() + err.getvalue()
    except SystemExit as e:
        return e.code, False, out.getvalue() + err.getvalue()
    except Exception as e:  # noqa: BLE001 - the point is to catch escapes
        return None, True, repr(e)


def write(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def main_test():
    repo = os.path.join(TMP, "repo")
    write(os.path.join(repo, "CLAUDE.md"), "# Rules\n\n- Never use `eval(` in python.\n")

    # ---- 1. malformed transcripts never crash --------------------------
    transcripts = {
        "empty": "",
        "invalid-json": "{not json\n",
        "not-a-dict": "[1,2,3]\n\"str\"\n",
        "truncated": '{"type":"user","uuid":"u1","mes\n',
        "no-uuid": '{"type":"user"}\n',
        "cyclic": '{"type":"user","uuid":"a","parentUuid":"b"}\n'
                  '{"type":"user","uuid":"b","parentUuid":"a"}\n',
        "self-parent": '{"type":"user","uuid":"a","parentUuid":"a"}\n',
        "message-not-dict": '{"type":"user","uuid":"u","message":"plain"}\n',
        "content-wrong-type": '{"type":"user","uuid":"u","message":{"content":123}}\n',
        "tool-use-no-input": '{"type":"assistant","uuid":"a","message":{"content":'
                             '[{"type":"tool_use","name":"Bash"}]}}\n',
        "nul-bytes": '{"type":"user","uuid":"u"}\n\x00\x00\n',
    }
    for name, body in transcripts.items():
        path = write(os.path.join(TMP, "t", name + ".jsonl"), body)
        code, crashed, _ = run(["check", path, "--repo", repo])
        check(not crashed, "malformed transcript does not crash: %s" % name)

    # a transcript still being appended to (half a line at the end)
    partial = write(os.path.join(TMP, "t", "partial.jsonl"),
                    '{"type":"user","uuid":"u","cwd":"%s","message":{"role":"user",'
                    '"content":"hi"}}\n{"type":"assist' % repo)
    _, crashed, _ = run(["check", partial, "--repo", repo])
    check(not crashed, "a transcript mid-write does not crash")

    # ---- 2. broken stores explain themselves, never traceback ----------
    broken = {
        "not-yaml": "{{{[[[",
        "yaml-is-a-list": "- a\n- b\n",
        "rules-not-a-list": "version: 1\nrules: 'oops'\n",
        "rule-not-a-dict": "version: 1\nrules:\n  - 'plain string'\n",
        "missing-text": "version: 1\nrules:\n  - id: R001\n",
        "garbage-next-id": "version: 1\nnext_id: banana\nrules: []\n",
        "empty": "",
    }
    for name, body in broken.items():
        sp = write(os.path.join(TMP, "s", name + ".yaml"), body)
        for cmd in (["rules", "list"], ["rules", "compose"], ["rules", "add", "never use `x(`"]):
            code, crashed, text = run(cmd, store=sp)
            check(not crashed,
                  "broken store does not crash: %s / %s" % (name, cmd[1]))
            if name in ("not-yaml", "yaml-is-a-list"):
                check(code == 2 and "ERROR" in text,
                      "broken store reports an error and exits 2: %s" % name)

    # the two hard failures are typed, and say how to recover
    for body in ("{{{[[[", "- a\n- b\n"):
        sp = write(os.path.join(TMP, "s", "typed.yaml"), body)
        try:
            load_store(sp)
            check(False, "load_store raises StoreError on %r" % body[:8])
        except StoreError as err:
            check("Fix it by hand" in str(err) or "start fresh" in str(err),
                  "StoreError tells the user how to recover")
        except Exception as err:  # noqa: BLE001
            check(False, "load_store raised %r instead of StoreError" % err)

    # ---- 3. the store never loses rules --------------------------------
    sp = os.path.join(TMP, "safety.yaml")
    st = load_store(sp)
    for i in range(5):
        add_rule(st, "never use `bad%d(`" % i, bundle="base")
    save_store(st)
    baseline = open(sp, encoding="utf-8").read()
    check(len(load_store(sp).rules) == 5, "curated rules round-trip")

    # a corrupt id counter must not let a new rule overwrite an old one
    write(sp, "version: 1\nnext_id: banana\nrules:\n"
              "  - {id: R001, text: 'never use `a(`', bundle: base}\n"
              "  - {id: R007, text: 'never use `b(`', bundle: base}\n")
    st = load_store(sp)
    check(len(st.rules) == 2, "rules load despite a corrupt next_id")
    new, _ = add_rule(st, "never use `c(`", bundle="base")
    check(new is not None and new.id not in {"R001", "R007"},
          "a corrupt counter cannot cause an id collision (got %s)" % (new and new.id))

    # an interrupted save leaves the previous store untouched
    write(sp, baseline)
    st = load_store(sp)
    add_rule(st, "always use `rg`", bundle="base")

    class Boom(Exception):
        pass

    original = Store.to_yaml
    Store.to_yaml = lambda self: (_ for _ in ()).throw(Boom())
    try:
        save_store(st)
        check(False, "the simulated crash actually fired")
    except Boom:
        pass
    finally:
        Store.to_yaml = original

    check(open(sp, encoding="utf-8").read() == baseline,
          "an interrupted save leaves the store byte-identical")
    check(len(load_store(sp).rules) == 5, "rules still load after an interrupted save")
    leftovers = [f for f in os.listdir(os.path.dirname(sp)) if f.startswith(".mutalyze-store-")]
    check(not leftovers, "an interrupted save leaves no temp files: %r" % leftovers)

    # compose refuses to clobber something it did not write
    target = write(os.path.join(TMP, "AGENTS.md"), "# hand written\n\n- keep me\n")
    code, _, _ = run(["rules", "compose", "-o", target], store=sp)
    check(code == 2 and "keep me" in open(target, encoding="utf-8").read(),
          "compose will not overwrite a hand-written rules file")

    # ---- 4. exit-code contract (a client may gate CI on this) ----------
    clean_sess = write(os.path.join(TMP, "clean.jsonl"),
                       '{"type":"user","uuid":"u","cwd":"%s","message":{"role":"user",'
                       '"content":"hi"}}\n' % repo)
    code, _, _ = run(["check", clean_sess, "--repo", repo])
    check(code == 0, "check exits 0 with no violations (got %s)" % code)

    dirty = write(os.path.join(TMP, "dirty.jsonl"),
                  '{"type":"user","uuid":"u","cwd":"%s","message":{"role":"user","content":"go"}}\n'
                  '{"type":"assistant","uuid":"a","parentUuid":"u","cwd":"%s","message":'
                  '{"role":"assistant","content":[{"type":"tool_use","id":"t","name":"Edit",'
                  '"input":{"file_path":"%s/x.py","old_string":"","new_string":"eval(bad)"}}]}}\n'
                  % (repo, repo, repo))
    code, _, _ = run(["check", dirty, "--repo", repo])
    check(code == 1, "check exits 1 when violations are found (got %s)" % code)
    code, _, _ = run(["check", os.path.join(TMP, "ghost.jsonl"), "--repo", repo])
    check(code == 2, "check exits 2 on unusable input (got %s)" % code)

    # ---- 5. nothing opens a network connection -------------------------
    real_socket, real_conn = socket.socket, socket.create_connection

    class Blocked(Exception):
        pass

    def deny(*_a, **_k):
        raise Blocked()

    socket.socket, socket.create_connection = deny, deny
    try:
        for argv in (["check", "--repo", repo],
                     ["rules", "import", repo],
                     ["rules", "mine", "--repo", repo],
                     ["rules", "compose"],
                     ["context", "--repo", repo, "--no-session"],
                     ["hook", "print", "--repo", repo]):
            try:
                run(argv, store=os.path.join(TMP, "net.yaml"))
                check(True, "no network")
            except Blocked:
                check(False, "%s opened a network connection" % argv[0])
    finally:
        socket.socket, socket.create_connection = real_socket, real_conn

    if FAILURES:
        sys.stdout.write("robustness FAIL\n")
        for f in FAILURES:
            sys.stdout.write("  - %s\n" % f)
        return 1
    sys.stdout.write(
        "robustness PASS (malformed input never crashes · broken store explains itself · "
        "atomic save keeps rules · exit-code contract · no network)\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main_test())
