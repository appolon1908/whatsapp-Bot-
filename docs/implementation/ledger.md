# Implementation ledger

## Authorization and decisions

- 2026-10-27: User: "work on the repos bot and lets focus on it firs implement all that is needed". This selects implementation of the previously reviewed Python/PostgreSQL commerce design in whatsapp-Bot- first.
- Ruling: proceed with the approved design without another brainstorming/plan approval cycle; user explicitly chose implementation after the detailed review.
- Ruling: use isolated repository checkout and disjoint file ownership for parallel work. This follows the session's proactive delegation requirement while avoiding shared-file writers.
- Ruling: bootstrap empty remote with README only, then scoped branches/PR. No existing branch protections/rulesets were present in the fresh read. Do not weaken or synthesize status checks.
- Ruling: local ephemeral PostgreSQL testing is authorized; production CRM installation and live sending are not part of this source-focused task.

## Preflight conflict scan

| Tasks | Shared interface/files | Finding and ruling |
|---|---|---|
| 1 / 2 | ORM fields, tenant_session, CommerceService | Exact names/transaction ownership pinned in interfaces.md; Task1 writes these files, Task2 consumes only. |
| 1 / 3 | DB bootstrap, models, session and errors | Task1 owns shared PostgreSQL fixtures; root owns API authorization and the identity task owns verification. No runtime owner credentials permitted. |
| 2 / 3 | IngressService, Worker, PolicyClient | Public signatures pinned; root mounts routes/CLI, Task2 owns behavior. |
| 1 / 4 | Migrations and provisioning | Migrations implement schema only; packaging never runs them implicitly during API start. |
| 2 / 4 | Effect flags and queue config | All sends off by default; integration mismatch remains documented until sibling contracts are certified. |
| 3 / 4 | Dependency manifest and entrypoints | Root alone writes packaging and manifest to prevent concurrent edits. |
| 1 self | RLS tests vs privileged setup | Test setup admin separate from runtime assertions; no SQLite substitutions. |
| 2 self | Handover vs HTTP dispatch | Conversation lock spans bounded submission; takeover invalidates pending work, accepted external commands cannot be recalled. |
| 3 self | Header tenant vs trusted identity | Header selects among DB-verified memberships; never authorizes by itself. |
| 4 self | Source completion vs production certification | CI/local evidence applies only tested SHA/runtime; no live or host backup claims inferred. |

## Progress

- Initial remote README commit created; implementation not yet published.
- Isolated local PostgreSQL 16.15 runtime available for real integration tests.

## Review corrections and rulings

- Messaging round 1: independent PostgreSQL regressions exposed delayed START overriding staff opt-out and concurrent inbox processing overtaking an earlier customer command. Persisted consent watermarks, stream advisory locks and predecessor eligibility corrected these behaviors; FIFO also covers delayed retries without blocking unrelated customer streams.
- Messaging round 1: added bounded raw policy responses and signed command/operation delivery correlation for asynchronous acknowledgements. Conflicting references remain retryable/dead-letter evidence, not silently consumed updates.
- Messaging round 2: a simulated executor-backed DNS call proved `asyncio.run` cleanup could exceed the HTTP deadline while dispatch held database locks. A shared bounded HTTP runtime now returns within the caller budget, cancels pending continuations and bounds resolver/task admission. Policy and identity fetches share this boundary. Already-running OS resolution is uninterruptible but confined to the fixed daemon pool.
- Identity review: explicit RecursionError handling protects the token parsing boundary across the supported JWT behavior. JWKS rejects compressed/oversized responses and uses the total transport deadline.
- Root/API regression: stale order state allowed an assigned agent to cancel after concurrent staff acceptance. Permissions now inspect refreshed state under conversation→order locks; assigned mutation checks hold the conversation lock.
- Application review A1: opening-hours API and commerce format disagreed. Canonical mon..sun, same-day increasing HH:MM ranges now match and have an HTTP-to-checkout regression.
- Application review A2: retained ORM Business instances could ignore newer tax/hours and silently discard settings updates. Checkout refreshes and share-locks configuration; settings mutations refresh their locked instance. Both failure modes were reproduced before correction.
- Application review A3: DSN query options could redirect operator connections away from the reviewed path. The engine rejects target override options, and operator mutations compare the actual connected database to the explicit target. Runtime also verifies exact database and dedicated role privileges.
- Application review A4: overlapping role changes could restore stale permissions. Staff requests acquire a shared transaction advisory authorization lock before reading membership; membership writes acquire its exclusive mode first, with no lock upgrade. Concurrent HTTP regression verifies an authorized serial outcome and denial after demotion.
- All review writers handed files back to the root before repository-wide formatting, verification and publication. Reviewers worked read-only in source and kept evidence in scratch reports.
- Finishing-workflow ruling: the user's implementation instruction and established scoped-PR workflow authorize publishing the source branch and review PR. Do not repeat a merge/push menu or ask for approval already implied by that work. No protected-branch merge, deployment, production migration or live send is authorized by this source task.
