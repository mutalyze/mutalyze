"""Generate the demo session (examples/session.jsonl) for the README / asciinema clip.

It stamps THIS checkout's absolute path as the transcript's cwd, so repo-scoped
checks (like `rg`-not-`grep`) fire no matter where you cloned the repo — and no
machine-specific path is committed. Regenerate anytime:

    python examples/make_demo.py
    mutalyze watch --replay examples/session.jsonl --repo examples
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
lines, parent, n = [], [None], [0]


def add(etype, msg=None, extra=None):
    n[0] += 1
    u = "u%d" % n[0]
    o = {"uuid": u, "parentUuid": parent[0], "type": etype, "gitBranch": "main",
         "cwd": HERE, "timestamp": "2026-07-20T15:%02d:00.000Z" % (n[0] * 3), "sessionId": "demo0001"}
    if msg is not None:
        o["message"] = msg
    if extra:
        o.update(extra)
    lines.append(o)
    parent[0] = u


def user(t):
    add("user", {"role": "user", "content": [{"type": "text", "text": t}]})


def tool(name, inp):
    add("assistant", {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t%d" % n[0], "name": name, "input": inp}]})


user("Wire up the new checkout endpoint and clear the build cache.")
tool("Read", {"file_path": HERE + "/src/checkout.ts"})
tool("Edit", {"file_path": HERE + "/src/checkout.ts",
              "old_string": "export const checkout = 1",
              "new_string": "export function checkout(cart){ return cart.total }"})
tool("Bash", {"command": "npm run build"})
tool("Bash", {"command": 'grep -rn "TODO" src/'})              # ✗ rule says use rg, not grep
tool("Read", {"file_path": HERE + "/src/cart.ts"})
tool("Bash", {"command": "rm -rf ~/.cache/checkout-build"})    # ⚠ danger: rm -rf a home path
tool("Bash", {"command": "git push origin feature/checkout"})  # held — not force, not main

with open(os.path.join(HERE, "session.jsonl"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
print("wrote", os.path.join(HERE, "session.jsonl"))
