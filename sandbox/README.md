# SolvedOnce reference sandbox runner

Roadmap step 4. It answers one question about a packet: **does the bug really happen, and does the fix really fix it?**

It rebuilds the packet's environment from the pinned dependencies, runs the minimal reproduction **before** and **after** applying the patch, compares what it saw with what the packet claims, and writes a result record that can be **signed** and later **verified** by anyone with the public key.

## Quick start

```bash
pip install -r requirements.txt

# Run the repository's example in Docker. The fictional 'tinycsv' comes from a local
# source tree that stands in for a package mirror.
python sandbox/solvedonce_run.py run examples/example-packet.json \
    --package-dir tinycsv=examples/fixtures/tinycsv --out result.json
# verdict: verified

# Signed results
python sandbox/solvedonce_run.py keygen --out-dir keys
python sandbox/solvedonce_run.py run examples/example-packet.json \
    --package-dir tinycsv=examples/fixtures/tinycsv --sign-key keys/runner_ed25519.pem --out result.json
python sandbox/solvedonce_run.py verify result.json --public-key keys/runner_ed25519.pub.pem
# OK  valid signature by ed25519:...
```

Exit codes: `0` verified · `1` not verified · `2` usage or environment error.

## What happens, step by step

1. **Validate first.** The packet goes through the reference validator. If it has any error, the runner refuses to run it.
2. **Build the environment** (Docker image from `python:<version>-slim`). Pinned dependencies are installed, and the installed versions are checked against the pins. This is the only step with network access, standing in for the pre-fetched package mirror of stage (G).
3. **Run before the fix** with no network, a read-only root filesystem, all capabilities dropped, `no-new-privileges`, an unprivileged user (65534), 512 MB memory, 1 CPU, 256 processes and a timeout.
4. **Apply the patch** with [`apply_patch.py`](apply_patch.py), a strict applier: exact context only, no fuzz, no file creation or deletion, no paths outside the package tree, and all-or-nothing.
5. **Run after the fix** under the same restrictions.
6. **Decide.** `verified` only if the test failed before, the patch applied, the test passed after, and both results match what the packet claims. A test that could not even be collected (pytest exit codes 2–5) counts as an error, not a failure, so a broken import cannot pass as "the bug reproduces".
7. **Sign** (optional): Ed25519 over the canonical JSON of the record.

## Verdicts

| Verdict | Meaning |
|---|---|
| `verified` | Failed before, patch applied cleanly, passed after, and the packet's claims match |
| `reproduced_only` | The fix is a workaround or configuration change, not a patch: the failure was reproduced, the fix could not be checked automatically |
| `not_verified` | Anything else; `reasons` says why |

## Backends

| Backend | Isolation | Use |
|---|---|---|
| `docker` (default) | Yes, as described above | Real use |
| `local-unsafe` | **None**: a throwaway virtualenv on the host | Developing and testing the runner itself, only with packets you wrote |

## Tested behavior

The test suite and CI cover, among others:

- The example packet is `verified` (local backend and Docker).
- A packet whose patch does not actually fix the bug is `not_verified`.
- A **lying packet** (it claims a bug, but the package it runs against is already fixed) is caught: the test passes before the fix, so the claim does not match.
- Inside Docker, a test that tries to open a network connection fails.
- A tampered result, or one checked with the wrong key, fails signature verification.
- Malicious patches (path escape, file creation, wrong context, re-application) are rejected without writing anything.

## Not done here, by design

- **`session_ref` checking** and the **novelty re-check with the provider's own model** need the provider's internal systems. The result record lists both under `not_checked`.
- **Running the target's full test suite** needs the target's source and tests, which a packet does not carry.
- **Ecosystems other than Python.** The runner rejects other languages for now.
- Docker isolation is strong but not absolute; a production deployment would add a stronger boundary such as gVisor or Firecracker microVMs, as the main document notes.
