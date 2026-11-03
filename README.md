# SolvedOnce

**AI coding models are at risk of a knowledge freeze. The public sources they learned from are drying up, because people now ask AI instead of posting publicly. SolvedOnce is a proposed solution: a verified channel that feeds newly solved problems from agent sessions back into model training.**

> Solved once by one user. Known by every model after the next training run.

Author: Kahraman Gülbağlar · License: MIT · Status: Concept / RFC

---

## 1. The problem: models are running out of new things to learn

Coding models learned from public Q&A. That public Q&A is disappearing because of the models themselves. Left alone, this loop ends with models that know everything up to a certain year and almost nothing after it.

Public developer knowledge is drying up at the source.

- For roughly 15 years, Stack Overflow, GitHub issues, and forums were the public record of *novel* bugs and their fixes. That record is a major part of what today's coding models learned from.
- Developers now take their problems to an assistant instead. The bug gets solved in a private chat session and the knowledge stays there.
- When a framework ships a new version with a new failure mode, there is no longer a public thread where hundreds of people converge on the fix. The next model generation has less to learn from on exactly the problems that matter most: the new ones.

Meanwhile, every day, millions of agent sessions (Claude Code, Codex, Gemini CLI, Grok, etc.) work through genuinely novel bugs to a *verified* resolution — and that signal is mostly discarded or only weakly captured through generic "use my chats for training" opt-ins and thumbs-up/down.

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
| **Same day** | A searchable index of verified packets (retrieval) | Every agent can find the fix immediately, before any retraining |
| **Days** | The upstream project's issue tracker, and an open public archive | The library gets fixed, and the knowledge stays public for humans and every model, not one provider |
| **Next model release** | Training and eval corpus | The knowledge becomes part of the model itself |

### Scope: only problems other people will also hit

A bug in a company's own private code helps nobody else. A packet is eligible only if the problem lives in software that other people also use: public libraries, frameworks, language runtimes, toolchains, operating systems, drivers, public APIs. Private-code bugs never leave the session.

### What a packet looks like

- Schema: [`schema/fix-packet.schema.json`](schema/fix-packet.schema.json) (draft v0.1, JSON Schema 2020-12)
- Full example: [`examples/example-packet.json`](examples/example-packet.json) (fictional library, real format; its test genuinely fails before the patch and passes after it)

Key fields: `scope`, `target` (ecosystem, package, affected version range), `problem` (symptom, root cause, normalized error signature), `novelty_check` (what was searched, and proof the model could *not* solve it unaided), `reproduction` (minimal test, rewritten by the model), `fix` (patch or workaround), `evidence` (before: fail, after: pass), `provenance` (`authored_by: "model"`), `consent` (approval, public release, upstream report, attribution).

The key design choices:

| Choice | Why |
|---|---|
| The model writes the packet, not the human | Humans cannot inject arbitrary payloads labeled as "the fix." The model summarizes what actually happened in the session. |
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
 (G) Sandboxed execution
        │  test must FAIL on original, PASS with fix, no side effects
        ▼
 (H) Human / expert review (only for packets that passed E–G)
        │
        ├──► (J) Retrieval index ............ same day: agents look it up
        ├──► (K) Public archive ............. if the user allowed public release
        ├──► (L) Upstream issue / PR ........ if the user allowed it, and upstream is public
        └──► (I) Training / eval corpus ..... next model release
```

## 4. Stage details and real-world precedents

None of the individual components are new. The proposal is to assemble them into one pipeline aimed at model training.

### (A) Client-side novelty check
Before spending the user's quota, the agent checks whether the issue is already known: web search, the package's issue tracker, release notes, advisory databases.

- Precedents: Stack Overflow's duplicate-question suggestions while typing; GitHub's "similar issues" suggestions; vulnerability databases such as **OSV.dev** and **CVE** that act as a canonical "known issues" registry.
- If known: the agent tells the user, links the existing fix, and nothing is submitted.

### (B) Model-generated packet in fail→pass form
This is the same format already used to *evaluate* coding models.

- Precedent: **SWE-bench** builds tasks from real GitHub issues, each with tests that fail before the reference patch and pass after it. SolvedOnce produces SWE-bench-style instances continuously, from live sessions, for the bugs the current model could not solve unaided.
- These packets are therefore valuable twice: as training data, and as fresh, contamination-resistant evaluation data.

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
- Run the test in an isolated environment (e.g. **gVisor**, **Firecracker** microVMs, or the containerized harnesses already used for SWE-bench-style evals).
- Accept only if: test fails on the original, passes with the fix, and the fix does not break the package's existing test suite where available.
- No network, no secrets, resource limits.

### (H) Human review
Only packets that survived E–G reach a reviewer, so expert time is spent on a small, high-quality, already-verified set. Large clusters are reviewed first.

### (I) Training admission
Accepted packets enter the corpus for the next training cycle and/or the eval suite. No claim is made about online weight updates: models are not patched per example (catastrophic forgetting, cost, safety review). The point is a **clean, verified, steadily growing source** for the regular retraining cadence.

### (J) Retrieval index: don't wait for the next model
Retraining takes months. Verified packets therefore also go into a searchable index, keyed by `error_signature`, `target` and version range. When an agent hits an error, it queries the index before guessing; a match returns the verified fix and its test. The index is also what stage (A) checks against, so the novelty check gets better as the index grows.

- Precedent: retrieval-augmented generation (RAG), already used by assistants for docs and web search; and advisory feeds like **OSV.dev** that tools query by package and version.
- Version ranges matter: a fix is only served to agents working with an affected version, and retired once `fixed_in_version` is released.

### (K) Public archive
With the user's consent, verified packets are published in an open archive under an open license. This is the actual replacement for public Q&A: humans can read it, search engines index it, and every model provider, large or small, can learn from it.

### (L) Upstream report
If the bug is in an open source project, the most useful place for the fix is the project itself. With the user's consent, the packet is turned into an issue (with the failing test) or a pull request (with the patch) on the upstream tracker, after a check that no equivalent issue is already open.

- Upstream reports must be rate-limited and human-reviewed. Maintainers are already overloaded by low-quality automated reports; a SolvedOnce report is only acceptable because it arrives verified, minimal, and with a failing test attached.
- Once upstream ships the fix, the packet is marked resolved in (J) and stops being served for new versions.

## 5. Why the model should write the packet

- **Integrity:** the human never authors "the fix" as free text. The model reports what actually happened in the session and what was actually executed.
- **Privacy:** the model is in the best position to reduce a large private codebase to a minimal public repro.
- **Calibration:** the model knows whether it already "knew" the answer. A packet is only worth generating if the session shows the model genuinely failed first and the resolution required new information.
- **Format:** packets are uniform, machine-checkable, and immediately usable by the training and eval pipelines.

## 6. Threat model (summary)

| Threat | Mitigation |
|---|---|
| Poisoned "fixes" with backdoors | Model-authored packets, static scan (E), sandbox fail→pass (G), human review (H) |
| Prompt injection aimed at the pipeline or future models | Text-field scanning (E), structured schema, reviewer gate |
| Spam / flooding | Client novelty check (A), quotas (D), dedup (F) |
| Sybil accounts bypassing quotas | Trust-weighted quotas, acceptance-rate reputation (D) |
| Leaking proprietary code or secrets | Minimal repro rewrite + secret/PII scanning (C), user approval |
| Coordinated campaigns to push a wrong "consensus" | Cluster size raises priority but never bypasses G and H |
| Flooding open source maintainers with automated reports | Upstream reports only after G and H, rate-limited, duplicate-checked against open issues (L) |
| Stale fixes served after upstream fixed the bug | Version ranges in `target`; packets retired from (J) once `fixed_in_version` ships |
| Private-code bugs leaking out as "public" packets | `scope` must name a public component; reviewer rejects anything else |

## 7. Cross-provider angle

Nothing here is provider-specific. A shared, open packet schema (problem, environment, failing test, fix, evidence) would let a fix verified once be usable by any provider that runs the same verification — an open replacement for what public Q&A sites used to provide to the whole ecosystem. An open, MIT-licensed schema and reference verifier would be the natural first step.

## 8. Incentives: why would anyone do this?

**Users.** Approving a packet costs ten seconds, but there should still be a reason:
- Public credit: optional attribution on the archive entry and the upstream issue, like a Stack Overflow answer or a merged PR.
- Small usage credits or extended limits per accepted packet (accepted, not submitted, so spam earns nothing).
- The practical reward: the next time anyone, including them, hits that bug, it is already solved.

**Providers.** The honest tension: verified fix data is a competitive asset, and a provider may prefer to keep it private. Arguments for the open version anyway:
- All providers face the same knowledge freeze. A shared commons is cheaper than each provider rebuilding a weaker private one.
- Packets are a source of fresh, contamination-resistant eval data, which every lab needs.
- An open format can still allow each provider to run its own pipeline. Openness can be adopted in steps: open schema first, then the open archive for packets where users consented.

**Maintainers.** Fewer duplicate, vague bug reports; more verified ones with a failing test attached.

## 9. Open questions

- Who owns a packet derived from a private session: the user, the provider, or the public? What license should the archive use?
- Will providers adopt a shared format, or each build a private version? Is a neutral steward needed (a foundation, as with OSV or SPDX)?
- How to handle bugs that only reproduce with proprietary dependencies or special hardware, where the sandbox cannot run them?
- What is the right quota: a fixed time window, or adaptive based on acceptance rate?
- How should conflicting packets for the same bug be resolved, e.g. two different workarounds that both pass?
- How to keep upstream reporting from adding to maintainer burden in practice, not just in principle?

## 10. Origin of the idea

This idea came out of a conversation about why Stack Overflow is fading. People no longer post their problems publicly, because an assistant answers instantly. But the assistant learned from those public posts in the first place. If nobody posts publicly anymore, future models lose their source of new knowledge — unless the private sessions that solve new problems can feed back into the commons in a safe, verified way.

There is a Turkish saying for the "everyone thinks their bug is the first" problem. Roughly: *everyone thinks their own trouble is the biggest one in the world.* Stage (A) and stage (F) exist because of that saying.

---

*Feedback, criticism and implementations welcome. MIT licensed — take it, build it, improve it.*
