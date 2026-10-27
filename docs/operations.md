# Operations and recovery

## Deployment boundary

Use a separate database, normally `codestra_whatsapp_commerce`, on a PostgreSQL instance selected after capacity and backup verification. The Compose stack is a dedicated local development environment. It does not describe or authorize changes to existing controller infrastructure.

Production admission requires recent CPU, memory, disk/WAL, connection and workload measurements; supported PostgreSQL maintenance; database-specific backup coverage; and a successful restore drill. Evidence about another application's backup job does not certify this database. Record the selected instance, exact source commit, schema revision, restore results and operator approval in the normal deployment system.

The service starts without automatic provisioning, migrations or channel registration. These are explicit operator actions. The runtime uses `codestra_whatsapp_app`, never a database owner or operator account.

## Roles and effective access

- `codestra_whatsapp_owner`: dedicated NOLOGIN schema/database owner.
- `codestra_whatsapp_app`: dedicated LOGIN runtime role, without superuser, database/role creation, owner membership or RLS bypass privileges.
- Operator connection: an explicitly authorized privileged account used only for provisioning, migration and onboarding; not passed into API/worker containers.

The database has 16 tenant tables in `commerce`. Each uses ENABLE/FORCE RLS and tenant-qualified foreign keys. The application sets tenant context with transaction-local `set_config`, so pooled connections do not retain another request's tenant. A JWT verifies identity; an active database Membership authorizes access to the selected business. The application database credential is a trusted service credential, not a credential to distribute to individual businesses.

**PostgreSQL PUBLIC CONNECT matters:** creating a dedicated database and revoking its public grants does not remove the application's inherited CONNECT permission on unrelated databases. Install reviewed `pg_hba.conf` rules before broad allow rules, covering every actual network path. [The production template](../scripts/pg_hba-commerce.example) is an example requiring deployment-specific source addresses. The development Compose HBA covers its isolated container network. CI uses a test-only database-name regex; never copy that wildcard to production.

After HBA review/reload, independently prove application connections succeed to exactly the commerce database and fail to `postgres`, `template1` and each relevant unrelated database. Do not revoke global PUBLIC privileges or alter controller database ownership as a shortcut.

## Configuration

| Variable | Purpose |
| --- | --- |
| `COMMERCE_DATABASE_URL` | Runtime PostgreSQL/psycopg3 URL for the dedicated app role. |
| `COMMERCE_EXPECTED_DATABASE_NAME` | Exact expected commerce database name; defaults to `codestra_whatsapp_commerce`. |
| `COMMERCE_ENVIRONMENT` | `local`, `test` or `production`. |
| `COMMERCE_JWT_ISSUER`, `COMMERCE_JWT_AUDIENCE` | Required verified token issuer/audience. |
| `COMMERCE_JWKS_URL` or `COMMERCE_JWT_PUBLIC_KEY` | Exactly one RS256 verification source; use configured HTTPS JWKS or trusted PEM public key. |
| `COMMERCE_INGRESS_SECRET` | At least 32 bytes, shared only with the trusted normalization/signing service. |
| `COMMERCE_WORKER_TENANT_IDS` | Explicit JSON list of business UUIDs assigned to this worker; no global unscoped tenant scan. |
| `COMMERCE_OUTBOUND_ENABLED` | Defaults false; requires completed integration/operational gates before activation. |
| `COMMERCE_POLICY_BASE_URL`, `COMMERCE_POLICY_BEARER_TOKEN` | Policy API origin and service credential; needed only for active submission. |
| `COMMERCE_ALLOW_INSECURE_LOCAL_HTTP` | Explicit local/test allowance; prohibited in production. Compose leaves it false. |
| `COMMERCE_JWKS_TIMEOUT_SECONDS` | Total fetch budget, default 5, allowed 0.1–10 seconds. |
| `COMMERCE_JWKS_CACHE_SECONDS`, `COMMERCE_JWKS_MIN_REFRESH_SECONDS` | Bounded key cache/refresh rate; defaults 300/5 seconds. |

Operator-only variables are `COMMERCE_ADMIN_DATABASE_URL`, `COMMERCE_OWNER_DATABASE_URL` and `COMMERCE_APP_PASSWORD`. Do not inject them into runtime processes. DSNs require an explicit database path and reject query options that could override it. Operator actions compare the effective connected database with the explicit target before mutation.

Use a secret manager to inject production values, rotate signing/integration credentials through coordinated upstream/downstream changes, and retain prior key material only within an explicitly planned rotation window. Never place customer content or bearer credentials in URL query strings, repository files, issue comments or logs. Python reads environment variables; `.env` loading is not automatic.

## Migration and rollout

1. Keep outbound delivery disabled. Verify the existing backup/restore gate and database/role/HBA target.
2. Run explicit `commerce provision --database ...` only for the reviewed database. Existing role passwords are not silently rotated by rerunning this command.
3. Run `commerce migrate` using the operator connection. Revision `0001_commerce` is an immutable SQL snapshot. The installed wheel includes the canonical migration resources. Reapplying the same migration is non-destructive.
4. Run `commerce onboard-business` with explicit name, currency, IANA timezone, verified owner subject and tax basis points. Repeating the same name/owner/configuration returns the existing business; conflicting configuration is refused.
5. Register verified channels through `commerce register-channel`. Store meaningful verification evidence. A disabled binding requires a fresh reference before reactivation.
6. Start the API with the restricted role; check `/healthz` and `/readyz`, perform authenticated role/tenant smoke tests, then start workers with an explicit tenant allowlist.
7. Run the real staging integration cases in [the contract](integration-contract.md). Review queued replies before changing effect flags, so a backlog does not become an unintended campaign.

This initial schema has no destructive automatic downgrade. Before a future migration, plan the supported rollback/forward-fix procedure and backup compatibility. Roll back application code only to a release compatible with the current schema. Do not drop the schema or production volume to roll back an application.

## Monitoring and incident handling

`/healthz` is process liveness. `/readyz` checks database access, safe runtime role, expected tables, identity configuration and ingress configuration. It does not fetch the identity issuer on every probe or establish that the policy/provider path is ready.

Authenticated owners/supervisors can inspect `/commerce/v1/queue` and `/commerce/v1/queue/{inbox|outbox}/dead`. Dead-letter views contain safe metadata, not message bodies or credentials. Owners can read `/commerce/v1/audit`. Integrate monitoring for process restarts, readiness failures, queue backlog age, dead-letter count, policy errors, overdue reservations, database connections, storage growth and backup/restore freshness. Apply ingress limits and bounded request/read timeouts at the reverse proxy; preserve the raw signed request body.

For an incident:

1. Disable outbound delivery at the worker configuration and restart/drain workers through the normal process supervisor. Keep the inbox available if safe so signed events remain durable.
2. For one conversation, use `/conversations/{id}/handover`; for a channel-wide stop, the owner can disable the channel. Accepted remote commands may still finish.
3. Identify the failing record and its safe error code. Correct configuration/contract issues before replaying. Preserve original command IDs, idempotency keys and provider correlation.
4. Replay only a dead-letter event with `POST /commerce/v1/queue/{kind}/{id}/replay`. Replay is audited, resets attempts and retains its identity. Dispatch rechecks current consent/mode/eligibility.
5. A failed head event blocks subsequent pending messages in that customer stream until it succeeds or reaches dead-letter state. Inspect the whole affected conversation before replaying an old command; replay can intentionally revisit a customer action.

Transient failures use bounded exponential retries, then dead-letter after eight attempts by default. A disabled/unconfigured outbound gate leaves work pending with a safe reason; it does not consume failure attempts. Acknowledged policy submissions are never replayed through the administrative dead-letter endpoint.

Identity verification and policy submission have bounded HTTP bodies/deadlines and a shared bounded transport runtime. An OS resolver operation already in progress cannot be forcibly interrupted by Python; it may finish in the fixed daemon resolver pool after its caller returns. Cancelled requests cannot continue to submit after resolution. Supervise processes with a bounded shutdown grace period and monitor resolver failures; do not enlarge concurrency to hide an unhealthy resolver.

## Backups and restore drills

Back up the **commerce database**, including schema, data, ACLs, pending inbox/outbox records and stable external IDs. Use an authorized backup role with appropriate access; the restricted RLS runtime role is not a full-database backup identity. Keep credentials in a protected PostgreSQL service/credential file or secret manager. Encrypt backup storage, define retention and recovery objectives, and include WAL/PITR if the agreed recovery point requires it.

Example operator commands after verifying the service definition points at the intended commerce database:

```bash
PGSERVICE=commerce_backup psql --no-psqlrc --command 'SELECT current_database(), current_user;'
PGSERVICE=commerce_backup pg_dump --format=custom --file=/secure-backup/commerce.dump
```

Restore into a newly provisioned isolated recovery database, never over the live database. Use matching PostgreSQL tooling and retain dedicated ownership/ACLs:

```bash
PGSERVICE=commerce_restore pg_restore --exit-on-error --dbname=codestra_whatsapp_commerce_restore_check /secure-backup/commerce.dump
```

The restore operator must have already created the recovery database and required dedicated roles. Verify all 16 tables still have `codestra_whatsapp_owner` ownership and ENABLE/FORCE RLS; app role access and no-context/cross-tenant restrictions; row counts and representative order/stock totals; pending inbox/outbox states; and unchanged command/idempotency/operation identifiers. Start recovery application processes with outbound disabled. Reconciliation with downstream accepted operations is required before any replay after recovery. Record restore duration and the recovered data cutoff.

The integration suite performs an actual isolated dump/restore and validates pending work and RLS. That is source-level recovery evidence; it is not certification of a production schedule, encrypted storage, retention or recovery objective.

## Running the full tests outside CI

Use an expendable PostgreSQL 16+ cluster under your control. `TEST_DATABASE_ADMIN_URL` must point at a privileged bootstrap database. `COMMERCE_TEST_DATABASE_NAME` must start with `codestra_whatsapp_commerce_test`. The test fixture creates dedicated roles and the test database; it refuses a normal production database name. Never aim it at a shared controller instance.

Before running tests, install app-only HBA rules allowing the test database prefix on loopback and rejecting all other app-role database/path combinations. Place them before broader allows and reload. The CI setup script applies its rules only inside the workflow's disposable service container. Install PostgreSQL 16 client tools; `TEST_POSTGRES_BIN` can name their binary directory when they are not on PATH. The restore test uses a unique disposable restore database and drops only that database after its assertions.

```bash
export COMMERCE_TEST_DATABASE_NAME=codestra_whatsapp_commerce_test_local
# Inject TEST_DATABASE_ADMIN_URL securely; optional TEST_DATABASE_APP_PASSWORD
uv run --frozen --extra dev pytest
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev ruff format --check .
uv build --wheel
```

## Data handling

Conversation text, customer references, delivery addresses, consent evidence and audit actors are persisted in the commerce database. Access is scoped by tenant and role; agents are additionally scoped to assigned conversations, while fulfillment staff do not receive conversation history. Configure transport encryption, storage/backup encryption, access auditing and a business-approved retention/deletion policy before real customer onboarding. No automatic destructive retention job or bulk customer export is enabled in this initial implementation; those workflows require an explicit policy and review.
