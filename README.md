# SolvedOnce

**AI coding models are at risk of a knowledge freeze. The public sources they learned from are drying up, because people now ask AI instead of posting publicly. SolvedOnce is a proposed solution: a verified channel that feeds newly solved problems from agent sessions back into model training.**

> Solved once by one user. Findable by every agent once verified, and learned by the next generation of models that take part.

Author: Kahraman Gülbağlar · License: MIT · Status: Concept / RFC

**TL;DR**
- **Problem:** public developer Q&A, a key source of what coding models learned, is collapsing because developers now ask AI privately. New bugs get solved in private sessions and the knowledge is lost.
- **Proposal:** when an agent solves a genuinely new bug in public software, it writes a structured *fix packet*: minimal reproduction as a failing test, the fix, and fail→pass evidence.
- **Verification:** packets are scrubbed, deduplicated, rate-limited, scanned, re-run in a sandbox, checked against the provider's own session records, and reviewed by humans. Nothing rests on trusting the user's machine.
- **Use:** verified packets go to a retrieval index (agents find the fix right away), to the upstream project and an open archive (public knowledge again), and into training, with a held-out share kept as fresh evals.
- **Status:** design document, draft packet schema, a worked example and a tested reference validator are in this repo; a sandbox runner is next.

---

## 1. The problem: models are running out of new things to learn

Coding models learned from public Q&A. That public Q&A is disappearing because of the models themselves. Left alone, this loop ends with models that know everything up to a certain year and almost nothing after it.

Public developer knowledge is drying up at the source, and this is measured, not assumed:

- A peer-reviewed study found that within six months of ChatGPT's release, posting activity on Stack Overflow fell by about 25% relative to comparable platforms where ChatGPT was less accessible, with larger drops for the most widely used languages such as Python and JavaScript [1].
- Stack Overflow's own public data shows monthly question volume back at the level of its 2009 launch by May 2025 [2]. To be fair, the decline started earlier (around 2014, and faster from mid-2020), but it accelerated sharply after late 2022 [2].
- Training on model-generated content instead of genuine human data degrades models over generations ("model collapse"), and the authors note that data from genuine human interactions becomes *more* valuable as AI-generated text spreads [3]. Verified fixes from real sessions are exactly that kind of data.

In more detail:

- For roughly 15 years, Stack Overflow, GitHub issues, and forums were the public record of *novel* bugs and their fixes. That record is a major part of what today's coding models learned from.
- Developers now take their problems to an assistant instead. The bug gets solved in a private chat session and the knowledge stays there.
- When a framework ships a new version with a new failure mode, there is no longer a public thread where hundreds of people converge on the fix. The next model generation has less to learn from on exactly the problems that matter most: the new ones.

Meanwhile, every day, millions of agent sessions (Claude Code, Codex CLI, Gemini CLI, Cursor, and others) work through genuinely novel bugs to a *verified* resolution — and that signal is mostly discarded or only weakly captured through generic "use my chats for training" opt-ins and thumbs-up/down.

**SolvedOnce** proposes a narrow, structured, verifiable channel for that signal.

## 2. Core idea

When an agent and a user resolve a problem the agent could not solve from existing knowledge, the **agent itself** (not the human) produces a structured *fix packet*:

1. A minimal description of the problem and environment
2. A minimal reproduction, packaged as a **failing test**
3. The fix
4. Evidence that the test **fails before** and **passes after** the fix

The packet is scrubbed, deduplicated, rate-limited, scanned, executed in a sandbox, and only then reviewed by humans. A verified packet is then used in three ways, on three time scales:

| When | Where it goes | Effect |
|---|---|---|
| **As soon as reviewed** (hours to days) | A searchable index of verified packets (retrieval) | Every agent can find the fix long before any retraining |
| **Days to weeks** | The upstream project's issue tracker, and an open public archive | The library gets fixed, and the knowledge stays public for humans and every model, not one provider |
| **Next model release** | Training corpus, plus a held-out eval set | The knowledge becomes part of the model itself, and new models are measured on bugs they never saw |

### Scope: only problems other people will also hit

A bug in a company's own private code helps nobody else. A packet is eligible only if the problem lives in software that other people also use: public libraries, frameworks, language runtimes, toolchains, operating systems, drivers, public APIs. Private-code bugs never leave the session.

### Trust boundary: the user's machine is not trusted

Coding agents run partly on the user's machine, so anything the client sends could in principle be forged: someone could hand-write a packet and label it `authored_by: "model"`, or invent failed attempts. The design therefore never relies on the client's word:

- **Authorship is checked server-side.** The model's own outputs in an agent session are produced on the provider's servers, so the provider already holds the session record. Each packet carries a `session_ref` to that record; the provider checks that the packet matches what its model actually produced and what was actually run in that session. Packets with no matching session are rejected. Ideally the provider generates the final packet itself from its own session record, and the client only adds the scrubbed reproduction and the user's approval.
- **Evidence is re-run, not believed** (G): the fail→pass result is reproduced in the provider's sandbox.
- **Novelty is re-measured, not believed** (G): the provider's current model gets the reproduction without the fix.

Forging a packet therefore gains nothing: every claim that matters is either matched to the provider's own records or re-measured.

### What a packet looks like

Quick start, checking the example with the reference validator:

```bash
pip install -r requirements.txt
python validator/solvedonce_validate.py examples/example-packet.json
# OK  examples/example-packet.json  (0 errors, 0 warnings)
```

What the validator checks, and what it deliberately does not, is documented in [`validator/README.md`](validator/README.md).

- Schema: [`schema/fix-packet.schema.json`](schema/fix-packet.schema.json) (draft v0.3, JSON Schema 2020-12, strict: unknown fields are rejected, because packets are untrusted input)
- Full example: [`examples/example-packet.json`](examples/example-packet.json) (fictional library, real format; it passes the reference validator in strict mode, its patch applies cleanly, and its test fails before the patch and passes after it)
- Reference validator: [`validator/solvedonce_validate.py`](validator/solvedonce_validate.py), with tests in [`tests/`](tests/)

Key fields: `scope`, `target` (ecosystem, package, affected version range, license), `problem` (symptom, root cause, normalized error signature or null for silent bugs), `novelty_check` (sources searched, and the failed attempts recorded in the session), `reproduction` (minimal test with pinned dependencies, rewritten by the model), `fix` (patch or workaround), `evidence` (fail before, pass after, produced by running the minimal reproduction itself), `provenance` (`authored_by: "model"`, `session_ref` to the provider's session record), `consent` (approval, public release, upstream report, attribution).

The key design choices:

| Choice | Why |
|---|---|
| The model writes the packet, not the human | Nobody hand-writes "the fix." The model summarizes what actually happened in the session. This raises the bar for poisoning; it does not remove it (a user can still try to steer the model), so scanning, sandboxing and review still apply. |
| Packets are executable tests | Verification becomes mechanical (fail→pass), not a matter of opinion. |
| Novelty is checked before submission | Most "world-first" bugs are "first-for-me" bugs. |
| Hard per-account submission quota | A reasonable developer does not discover a genuinely new bug every day. |
| Duplicates are counted, not discarded | 300 independent reports of the same bug is a strong prioritization signal. |

## 3. Pipeline

```
 [User + Agent session]
        │  problem resolved after real effort
        ▼
 (A) Novelty check (client side)
        │  known? → tell the user, link the known fix, quota NOT consumed
        ▼
 (B) Packet generation by the model
        │  problem summary, env, repro-as-test, fix, fail→pass evidence
        ▼
 (C) Privacy scrubbing (client side)
        │  secrets, PII, internal hostnames, proprietary code reduced to a minimal repro
        ▼
 (D) User consent + quota check
        │  e.g. 1 packet / 3–7 days per account, weighted by account trust
        ▼
 ───────────── submitted to the provider ─────────────
        ▼
 (E) Static security scan
        │  malicious patterns, exfiltration, prompt injection in text fields
        ▼
 (F) Semantic deduplication + clustering
        │  near-duplicate → increment cluster counter, attach as variant
        ▼
 (G) Sandboxed verification
        │  session_ref matches the provider's record; test FAILS on original, PASSES with fix;
        │  provider's current model cannot fix it unaided (novelty re-check)
        ▼
 (H) Human / expert review (only for packets that passed E–G)
        │
        ├──► (J) Retrieval index ............ as soon as reviewed: agents look it up
        ├──► (K) Public archive ............. if the user allowed public release (license permitting)
        ├──► (L) Upstream issue / PR ........ if the user allowed it, and upstream is public
        └──► (I) Training corpus + held-out eval set ..... next model release
```

## 4. Stage details and real-world precedents

None of the individual components are new. The proposal is to assemble them into one pipeline that turns privately solved problems back into shared knowledge: for retrieval, for the public, for upstream projects, and for training.

### (A) Client-side novelty check
Before spending the user's quota, the agent checks whether the issue is already known: web search, the package's issue tracker, release notes, advisory databases.

- Precedents: Stack Overflow's duplicate-question suggestions while typing; GitHub's "similar issues" suggestions; vulnerability databases such as **OSV.dev** and **CVE** that act as a canonical "known issues" registry.
- If known: the agent tells the user, links the existing fix, and nothing is submitted.

### (B) Model-generated packet in fail→pass form
This is the same format already used to *evaluate* coding models.

- Precedent: **SWE-bench** builds tasks from real GitHub issues, each with tests that fail before the reference patch and pass after it. SolvedOnce produces SWE-bench-style instances continuously, from live sessions, for the bugs the current model could not solve unaided.
- These packets are therefore valuable twice: as training data, and as fresh evaluation data. The two uses must not overlap: a packet used for training is no longer a clean test. See (I) for the held-out split.
- **Novelty is evidenced, not introspected.** Models are poorly calibrated about what they "know," so the packet does not rely on the model's own claim. It records the approaches the agent actually tried in the session that did not fix the failing test (`failed_attempts`). Stage (G) re-checks novelty objectively.
- **Evidence comes from the minimal reproduction.** The bug was first observed in the user's code, but that code never leaves the machine. After the model rewrites the problem into a minimal reproduction (C), the agent runs *that* reproduction locally, before and after the fix, and only those runs count as evidence (`evidence.ran_on: "minimal_reproduction"`). If the minimal version does not fail, there is no packet.

### (C) Privacy scrubbing
The packet must contain a *minimal* repro, not the user's codebase.

- Secret scanning: tools such as **gitleaks** and **TruffleHog**.
- PII detection/redaction: e.g. **Microsoft Presidio**.
- The model rewrites the repro to remove business identifiers (company names, hostnames, customer data) while preserving the failure. The user sees the final packet and approves it before submission.

### (D) Consent and quotas
- Explicit, per-packet opt-in. No silent collection.
- Quota per account (e.g. one packet every 3–7 days). Novel bugs are rare; quotas match reality and block spam.
- Sybil resistance: quota and priority weighted by account age, paid status, and historical packet acceptance rate. Precedent: reputation systems on Stack Overflow and trust levels on Discourse.

### (E) Static security scan
Every packet is treated as untrusted supply-chain input.

- Direct precedent: **NVIDIA SkillSpector** (open source, Apache-2.0), a scanner that vets AI agent skills before installation for malicious patterns, prompt injection, data exfiltration and privilege escalation, using static analysis with optional LLM semantic review. The same approach applies to fix packets: both are text + code meant to influence an agent.
- Additional tooling: **Semgrep**, **CodeQL**, and malicious-package detection of the kind used by **Socket** / **Snyk** for dependency registries.
- Natural-language fields are scanned for injection aimed at the training pipeline or future models.

### (F) Semantic deduplication and clustering
- Embedding-based similarity plus near-duplicate detection (e.g. MinHash/LSH, as used in large-scale training-data deduplication).
- Duplicates are not thrown away: they increment a cluster counter. Cluster size = how widespread the bug is = review priority.
- Variants (same root cause, different surface) are attached to the cluster, which yields better generalization data than a single instance.

### (G) Sandboxed verification
- Session check first: `provenance.session_ref` must point to a real session in the provider's records, and the packet must match what the model produced and ran there (see *Trust boundary*).
- Run the test in an isolated environment (e.g. **gVisor**, **Firecracker** microVMs, or the containerized harnesses already used for SWE-bench-style evals).
- Dependencies: the pinned versions in `reproduction.dependencies` are pre-fetched from a package mirror into the sandbox image, the same way SWE-bench harnesses build per-task environments. The test run itself has no network, no secrets, and resource limits.
- Accept only if: test fails on the original, passes with the fix, and the fix does not break the package's existing test suite where available.
- **Objective novelty re-check:** the provider's current model is given the reproduction *without* the fix. If it fixes the test on its own, the packet adds little for training (it may still be useful for the archive and upstream). This replaces trust in the model's self-report with a measurement.

### (H) Human review
Only packets that survived E–G reach a reviewer, so expert time is spent on a small, high-quality, already-verified set. Large clusters are reviewed first.

### (I) Training admission
Accepted packets are split: most enter the corpus for the next training cycle; a held-out share (for example, a random slice of each month's packets) is never trained on and forms a rolling eval set of real, recent, verified bugs. The idea of a continuously refreshed eval set is already used by benchmarks that add new questions over time precisely to stay ahead of training data.

Held-out packets must not leak into anyone's training data through the public archive (K). Two measures:
- **Delayed publication:** held-out packets are published only after the eval window closes (for example, after the next model generation has been measured on them). Until then they can be used privately for retrieval (J) by the provider that holds them.
- **Canary marking:** when published, they carry a canary string, the same mechanism BIG-bench introduced so that dataset builders can filter benchmark data out of training corpora [4]. Canaries are known to be imperfect (models have been shown to reproduce canary strings, which means marked data still got into training) [5], so delayed publication is the primary protection and the canary a tripwire.

No claim is made about online weight updates: models are not patched per example (catastrophic forgetting, cost, safety review). The point is a **clean, verified, steadily growing source** for the regular retraining cadence.

### (J) Retrieval index: don't wait for the next model
Retraining takes months. Verified packets therefore also go into a searchable index, keyed by `target`, version range and `error_signature` (or, for silent wrong-output bugs with no error, by symptom similarity). When an agent hits an error, it queries the index before guessing; a match returns the verified fix and its test. The index is also what stage (A) checks against, so the novelty check gets better as the index grows.

- Precedent: retrieval-augmented generation (RAG), already used by assistants for docs and web search; and advisory feeds like **OSV.dev** that tools query by package and version.
- Version ranges matter: a fix is only served to agents working with an affected version, and retired once `fixed_in_version` is released.

### (K) Public archive
With the user's consent, verified packets are published in an open archive under an open license. This is the actual replacement for public Q&A: humans can read it, search engines index it, and every model provider, large or small, can learn from it.

- Licensing: the packet's own text and reproduction are model-written and can use the archive's license. A patch derived from the target's source code inherits the target's license (`target.license`); a patch to a GPL project, for example, is published under the GPL, not relicensed. Patches to proprietary targets are not published; only the description and workaround are.

### (L) Upstream report
If the bug is in an open source project, the most useful place for the fix is the project itself. With the user's consent, the packet is turned into an issue (with the failing test) or a pull request (with the patch) on the upstream tracker, after a check that no equivalent issue is already open.

- Upstream reports must be rate-limited and human-reviewed. Maintainers are already overloaded by low-quality automated reports; a SolvedOnce report is only acceptable because it arrives verified, minimal, and with a failing test attached.
- Once upstream ships the fix, the packet is marked resolved in (J) and stops being served for new versions.

## 5. Why the model should write the packet

- **Integrity:** nobody hand-writes "the fix" as free text. The model reports what actually happened in the session and what was actually executed. This makes poisoning harder, not impossible: a user can still try to steer the model, which is why stages E, G and H exist.
- **Privacy:** the model is in the best position to reduce a large private codebase to a minimal public repro.
- **Grounded novelty:** a packet is only worth generating if the session shows the model genuinely failed first. That is established from the session trace (recorded failed attempts) and re-measured by the provider in (G), not taken from the model's own sense of what it knew.
- **Format:** packets are uniform, machine-checkable, and immediately usable by the training and eval pipelines.

## 6. Threat model (summary)

| Threat | Mitigation |
|---|---|
| Poisoned "fixes" with backdoors | Model-authored packets, static scan (E), sandbox fail→pass (G), human review (H) |
| Prompt injection aimed at the pipeline or future models | Text-field scanning (E; first-pass patterns in the reference validator), structured schema, reviewer gate |
| Spam / flooding | Client novelty check (A), quotas (D), dedup (F) |
| Sybil accounts bypassing quotas | Trust-weighted quotas, acceptance-rate reputation (D) |
| Leaking proprietary code or secrets | Minimal repro rewrite + secret/PII scanning (C), user approval |
| Coordinated campaigns to push a wrong "consensus" | Cluster size raises priority but never bypasses G and H |
| Flooding open source maintainers with automated reports | Upstream reports only after G and H, rate-limited, duplicate-checked against open issues (L) |
| Stale fixes served after upstream fixed the bug | Version ranges in `target`; packets retired from (J) once `fixed_in_version` ships |
| Private-code bugs leaking out as "public" packets | `scope` must name a public component; reviewer rejects anything else |
| Forged packets written by hand on the user's machine and labeled as model-authored | `session_ref` checked against the provider's own session records; evidence and novelty re-measured in (G) |
| Held-out eval packets leaking into training via the public archive | Delayed publication plus canary marking (I) |
| Inflated novelty claims ("the model didn't know this") | `failed_attempts` from the session trace, plus the provider re-running its own model on the repro without the fix (G) |
| Training data leaking into the eval set | Fixed held-out split of accepted packets, never used for training (I) |
| Malformed or oversized packets used as an attack vector | Strict schema with `additionalProperties: false` and length limits, plus a 256 KB size cap, checked by the reference validator before any other stage |

## 7. Cross-provider angle

Nothing here is provider-specific. A shared, open packet schema (problem, environment, failing test, fix, evidence) would let a fix verified once be usable by any provider that runs the same verification — an open replacement for what public Q&A sites used to provide to the whole ecosystem. A first draft of that schema is already in this repository; the next pieces are listed in the roadmap below.

## 8. Incentives: why would anyone do this?

**Users.** Approving a packet costs ten seconds, but there should still be a reason:
- Public credit: optional attribution on the archive entry and the upstream issue, like a Stack Overflow answer or a merged PR.
- Small usage credits or extended limits per accepted packet (accepted, not submitted, so spam earns nothing).
- The practical reward: the next time anyone, including them, hits that bug, it is already solved.

**Providers.** The honest tension: verified fix data is a competitive asset, and a provider may prefer to keep it private. Arguments for the open version anyway:
- All providers face the same knowledge freeze. A shared commons is cheaper than each provider rebuilding a weaker private one.
- The held-out share of packets is a source of fresh eval data that no model has trained on, which every lab needs.
- An open format can still allow each provider to run its own pipeline. Openness can be adopted in steps: open schema first, then the open archive for packets where users consented.

**Maintainers.** Fewer duplicate, vague bug reports; more verified ones with a failing test attached.

## 9. Open questions

- Who owns a packet derived from a private session: the user, the provider, or the public? What license should the archive use?
- Will providers adopt a shared format, or each build a private version? Is a neutral steward needed (a foundation, as with OSV or SPDX)?
- How to handle bugs that only reproduce with proprietary dependencies or special hardware, where the sandbox cannot run them?
- What is the right quota: a fixed time window, or adaptive based on acceptance rate?
- How should conflicting packets for the same bug be resolved, e.g. two different workarounds that both pass?
- How to keep upstream reporting from adding to maintainer burden in practice, not just in principle?
- Licensing in practice: who checks that `target.license` is correct, and how are patches to projects with unclear or missing licenses handled?
- What share of packets should be held out for evaluation, how long should publication be delayed, and will all providers honor the canary?
- Cross-provider packets: a `session_ref` is only meaningful to the provider that issued it. How should a shared archive verify packets from many providers (signed attestations from each provider)?

## 10. Roadmap

| Step | What | Status |
|---|---|---|
| 1 | Problem statement, pipeline, threat model (this document) | Done |
| 2 | Packet schema and a worked example | Draft v0.3 in this repo |
| 3 | Reference validator: static checks for schema, evidence consistency, novelty evidence, pinned dependencies, version ranges, diff structure, secrets, PII, prompt injection and formats. Never executes packet code. 47 tests, CI on every push | Done: [`validator/`](validator/) |
| 4 | Reference sandbox runner: builds an isolated environment from `reproduction.dependencies`, runs the test before and after the patch, outputs a signed result | Next |
| 5 | Client-side prototype: an agent skill that, after a hard-won fix, drafts a packet, runs the minimal reproduction and shows it to the user for approval | Planned |
| 6 | Feedback from people who run training pipelines and from open source maintainers; schema v1.0 | Open: issues and PRs welcome |

## 11. Origin of the idea

This idea came out of a conversation about why Stack Overflow is fading. People no longer post their problems publicly, because an assistant answers instantly. But the assistant learned from those public posts in the first place. If nobody posts publicly anymore, future models lose their source of new knowledge — unless the private sessions that solve new problems can feed back into the commons in a safe, verified way.

There is a Turkish saying for the "everyone thinks their bug is the first" problem. Roughly: *everyone thinks their own trouble is the biggest one in the world.* Stage (A) and stage (F) exist because of that saying.

---

## References

1. R. M. del Rio-Chanona, N. Laurentsyeva, J. Wachs. *Large language models reduce public knowledge sharing on online Q&A platforms.* PNAS Nexus 3(9), 2024. https://doi.org/10.1093/pnasnexus/pgae400
2. G. Orosz. *Stack Overflow is almost dead.* The Pragmatic Engineer, 2025 (data from the Stack Exchange Data Explorer). https://blog.pragmaticengineer.com/stack-overflow-is-almost-dead
3. I. Shumailov, Z. Shumaylov, Y. Zhao, N. Papernot, R. Anderson, Y. Gal. *AI models collapse when trained on recursively generated data.* Nature 631, 755–759, 2024. https://doi.org/10.1038/s41586-024-07566-y
4. BIG-bench collaboration. *Beyond the Imitation Game: Quantifying and extrapolating the capabilities of language models* (canary string convention). https://github.com/google/BIG-bench
5. *BIG-Bench Canary Contamination in GPT-4.* AI Alignment Forum, 2024. https://alignmentforum.org/posts/kSmHMoaLKGcGgyWzs/big-bench-canary-contamination-in-gpt-4

---

*Feedback, criticism and implementations welcome. MIT licensed — take it, build it, improve it.*
