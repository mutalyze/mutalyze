# Telemetry — the derived-signals contract

**Status: locked before any hosted/team tier ships. Do not widen without a
schema version bump.**

Transcripts contain source code, credentials, and internal architecture. No
security team will allow uploading them. So the client extracts locally and
transmits **derived signals only**. This is the entire basis on which a team can
adopt the tool, and it is structurally impossible to retrofit — a single leaky
field in v1 poisons trust permanently. Decide it now.

## The only thing that may leave the machine

`ruleguard check --signals` emits exactly this and nothing else:

```json
{
  "schema_version": 1,
  "session_uid": "3b1cb9b5aede5b2c",
  "totals": { "turns": 717, "tool_calls": 190, "rules_compiled": 5, "rules_unsupported": 2 },
  "rules": [
    { "rule_uid": "367f23e0891b2cf5", "rule_type": "command",
      "verdict": "violated", "violations": 51, "turns": [9, 12, 17, ...] }
  ]
}
```

| Field | What it is | Why it's safe |
|---|---|---|
| `session_uid` | salted SHA-256 of the session id | the id is a random uuid; the hash is pseudonymous and carries no content |
| `rule_uid` | salted SHA-256 of the **normalized rule text** | lets the server group the same rule across users **without ever seeing the rule** |
| `rule_type` | `command` / `content` / `ordering` | a category, not content |
| `verdict` | `held` / `violated` | a boolean |
| `violations`, `turns[]` | a count and integer turn indices | numbers, not evidence |
| `totals.*` | turn / tool-call / rule counts | integers |

## Never transmitted

Commands · file paths · rule text · evidence strings · branch names · cwd · line
contents · uuids · anything free-text from the transcript.

## Enforcement, not promise

`ruleguard/signals.py` is the single function that builds the payload, and
`assert_clean()` re-checks the serialized result against every evidence string
and rule text and **raises if any token leaked**. The CLI calls it on every
`--signals` run before printing. The guarantee is executed, not documented.

```
$ ruleguard check --signals | grep -iE 'grep|/Users/|console|cargo'
clean — no raw content in payload
```

## Open schema questions (decide before, not after, the first customer)

- **`rule_uid` is a content hash.** Identical rule text hashes identically across
  users — that's the aggregation value ("this rule breaks everywhere"). It also
  means the server can confirm a *guessed* rule by hashing it. Rule text is
  policy, not secret, so this is acceptable; if a customer disagrees, add a
  per-org salt (kills cross-org grouping — a real tradeoff, name it).
- **`turns[]` is arguably content-adjacent** (reveals session shape). If a
  customer objects, ship a `--signals-minimal` that drops per-turn indices and
  keeps only counts.
- **No timestamps.** Deliberately — they'd allow correlation. Keep it that way.
