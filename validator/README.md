# SolvedOnce reference validator

A small command-line tool that checks a fix packet before anything else touches it. It is roadmap step 3.

**It never executes anything from the packet.** No test code, no patch, no commands. Every check is static. Running a packet is the job of a sandbox (roadmap step 4).

## Usage

```bash
pip install -r requirements.txt

python validator/solvedonce_validate.py examples/example-packet.json
python validator/solvedonce_validate.py --strict packets/*.json   # warnings also fail
python validator/solvedonce_validate.py --json packet.json        # machine-readable
```

Exit codes: `0` all valid · `1` errors (or warnings with `--strict`) · `2` unreadable file, invalid JSON or usage error.

Python 3.10+ is required. Tests: `python -m pytest -q tests`.

## What it checks

Checks run in order. If the schema check fails, the rest are skipped, because they assume a well-formed packet.

| Code | Level | Check |
|---|---|---|
| SO000 | error | File is not UTF-8 JSON, or exceeds 256 KB |
| SO001 | error | JSON Schema violation (strict: unknown fields rejected) |
| SO101–SO104 | error | Evidence: must fail before and pass after the fix; outcome and exit code must agree |
| SO105 / SO106 | error / warning | Fix breaks the existing test suite / suite not run |
| SO201–SO203 | error | Failed attempts too vague, empty, or duplicated |
| SO204 | warning | A "failed" attempt reads like a success |
| SO301 | error | Dependency not pinned to one exact version |
| SO302 | error | Python reproduction does not parse |
| SO303–SO306 | warning | No assertion found; language not syntax-checked; reproduction over 200 lines; target not pinned |
| SO401 | warning | Version-range checks only implemented for PyPI so far |
| SO402–SO406 | error | Invalid range; pinned version outside the affected range; `fixed_in_version` inside it |
| SO501 | error | `upstream_patch` without a patch |
| SO502 | error | Malformed unified diff (hunk header line counts must match the body) |
| SO503 | error | Patch writes outside the target tree (absolute or `../` path) |
| SO504 / SO505 | warning | Patch to proprietary target marked public; upstream report allowed without a repository |
| SO601–SO608 | error | Secrets: private keys, AWS / GitHub / Slack / Google / `sk-` keys, JWTs, credentials in URLs |
| SO611–SO614 | warning | Personal or internal data: emails (except example domains), private IPs, home paths, internal hostnames |
| SO621 | error | Text that reads as an instruction to a model (prompt injection) |
| SO622 | error | Invisible or bidirectional control characters |
| SO631 | warning | Reproduction uses network, shell, `eval`/`exec`, encoded payloads, credentials or destructive file operations |
| SO632 | error | `run_command` contains shell metacharacters (`;`, `&`, `\|`, `` ` ``, `$`) |
| SO701 | warning | Attribution given but public release not allowed |
| SO702 / SO703 | error / warning | `session_ref` encodes identity / shares a value with `packet_id` |
| SO801 | error | `created_at` is not an RFC 3339 date-time with timezone |
| SO802 | error | URL fields must be `https` |

## Limits, stated honestly

- Pattern-based security checks catch common, careless mistakes. They do not stop a determined attacker; that is what sandboxing (G) and human review (H) are for.
- Version-range checks currently cover PyPI only. Other ecosystems get a warning, not a silent pass.
- The patch is checked for structure, not applied: the validator does not have the target's source code.
- `session_ref` can only be verified by the provider that issued it; this tool checks only that it is opaque.
