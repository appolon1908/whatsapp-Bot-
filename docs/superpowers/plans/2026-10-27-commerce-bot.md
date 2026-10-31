# WhatsApp Commerce Bot Implementation Plan

> **For agentic workers:** Execute the assigned task with the subagent-driven-development workflow. Root coordinates integration and independent reviews. Do not modify files assigned to another writer.

**Goal:** Implement a persistent multi-business commerce bot backend with isolated data, deterministic ordering, safe messaging contracts and executable tests.

**Architecture:** One Python API and worker with PostgreSQL as authoritative store. The bot calls the existing policy API; Middleware retains all provider transport authority. Staff identity uses verified JWTs plus persisted memberships.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0, psycopg3, Alembic, PyJWT, pytest, PostgreSQL16+.

**Spec:** `docs/architecture/commerce-platform.md`; concrete shared contracts in `docs/implementation/interfaces.md`.

## Global Constraints

- Focus this repository first; do not modify other repositories or servers.
- External sends and AI replies default off; no direct provider access or invented certification.
- Separate commerce database, non-owner runtime role, FORCE RLS, transaction-local context, tenant composite foreign keys.
- Money in integer minor units; USD/DOP; quote TTL15m; submitted reservation TTL30m; preserve immutable order totals.
- Existing architecture/implementation authorization is sufficient; continue without repeating design approval questions.
- Use independent file ownership for parallel tasks. Root alone publishes through atomic_task -> subsection -> section -> development -> testing -> staging -> production.

## Review Focus

1. Connection pooling must not retain tenant context after commit/error (Task1 RLS tests).
2. A repeated confirmation or two shoppers buying the last item must not duplicate/oversell (Task1 concurrency tests).
3. Customer-provided order/quote IDs must not cross conversation or tenant boundaries (Task2 bot negative tests).
4. Human takeover/opt-out between generation and dispatch must suppress replies (Task2 worker concurrency tests).
5. Staff token identity, active membership and role must all be checked; privileged owner DB connections rejected at startup (Task3 auth/API tests).

### Task 1: Persistence and transactional commerce

Files: `src/whatsapp_bot/{db,models,commerce,errors,provision}.py`, `migrations/`, `alembic.ini`, `tests/conftest.py`, `tests/test_{rls,commerce,migrations,concurrency}.py`, `scripts/pg_hba-commerce.example`.
Interfaces: produce the persistence/domain APIs and model fields specified in `docs/implementation/interfaces.md`; consumes caller-owned transaction and validated actor.

- [x] Write meaningful failing tests for tenant isolation, scoped foreign keys, quote expiry/repricing, idempotency, stock reservation and release.
- [x] Run targeted tests and record expected failure before implementing.
- [x] Implement schema, migration, narrow routing lookup, restricted role provisioning, commerce operations and transactional audit/outbox.
- [x] Run actual PostgreSQL tests including concurrent final-unit checkout and migration upgrade/re-run.
- [x] Submit exact files/test evidence to root for independent review and integration.

### Task 2: Inbox, guided bot and outbound worker

Files: `src/whatsapp_bot/{messaging,bot,worker,policy}.py`, `tests/test_{ingress,bot,worker,policy}.py`.
Interfaces: consume Task1 models/services/session context; expose IngressService, Worker, PolicyClient and process_inbound as specified in interfaces.md.

- [x] Write failing tests for signatures/replay/content collisions, channel routing, conversation-scoped IDs and deterministic ordering.
- [x] Implement bounded HMAC ingress and durable dedup, guided English/Spanish commands, outbox worker and safe policy client.
- [x] Test replay100 times, transaction rollback, stale/human/opted-out replies, retry exhaustion and stable command IDs against real PostgreSQL.
- [x] Submit evidence and blockers; no provider calls or sibling repository edits.

### Task 3: Staff API, identity and operator CLI

Files: package/config/auth/API/schema/CLI modules not assigned above; `tests/test_{auth,api,readiness,cli}.py`.
Interfaces: consume Task1/Task2 contracts, provide create_app and operational commands. Root owns this task and shared dependency manifest.

- [x] Create failing JWT, membership, role, target DB and readiness tests.
- [x] Implement strict configuration, RS256 verification, role/assignment checks, validated API requests and operator-only provisioning/onboarding commands.
- [x] Run whole-system local integration via real authenticated HTTP test client and real PostgreSQL.
- [x] Generate OpenAPI from application; document actual implemented routes.

### Task 4: Packaging, validation and publication

Files: locked dependencies, Docker/Compose, CI, CODEOWNERS, governance metadata, README and runbooks, release evidence.

- [x] Provide API/worker images with non-root execution, safe defaults and separate migration step; no production credentials in examples.
- [x] Configure CI to run lint, unit/integration tests against PostgreSQL and dependency audit, without enabling deployment.
- [x] Exercise backup/restore into separate ephemeral database and keep production backup readiness explicitly unverified.
- [x] Independent full review; fix material findings and re-run relevant plus full regression suite.
- [ ] Publish source to scoped branch and PR into subsection; verify remote tree/SHA and inspect CI.
- [ ] Report completed source behavior, tests, exact commit/PR and remaining external integration/production-readiness gates honestly.
