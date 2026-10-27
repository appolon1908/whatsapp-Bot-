# Implementation verification — 2026-10-27

## Local result

The reviewed implementation passed **193 tests in 9.68 seconds** on Python 3.12.14 and a separate, disposable PostgreSQL 16.15 cluster. This extends the original 148-test baseline with named-migrator and operational-security regressions. Migrations and operator actions authenticate as `codestra_whatsapp_migrator`; runtime assertions use `codestra_whatsapp_app`. The isolated administrator is used for bootstrap, recovery and deliberate privilege-drift probes. No production database, customer record or external provider send was involved.

The published pull request records the exact source commit and remote tree verification. These checks establish the source behavior exercised here; they do not certify a deployed environment or substitute for required GitHub/authority reviews.

| Verification | Observed result |
| --- | --- |
| Complete pytest suite | 193 passed; one third-party Starlette warning about future TestClient transport compatibility. |
| Ruff lint and format | Passed across the repository, including Python code examples. |
| Wheel build | `codestra_whatsapp_commerce-0.1.0-py3-none-any.whl` built successfully. |
| Installed wheel migration smoke | Imported outside the source checkout; the actual named migrator CLI applied/reran packaged migrations and onboarded a business. All 16 FORCE RLS tables and `public.alembic_version` retained owner identity at revision `0001_commerce`; the unique test database was removed. |
| Frozen dependency audit | No known vulnerabilities reported for the exported locked development/runtime dependency set. This is a point-in-time advisory result. |
| Generated OpenAPI | 31 paths generated from the application in `docs/openapi.json`. |
| Compose/CI validation | YAML and shell syntax, action pins, environment separation, profiles, localhost bindings and HBA policies verified. |
| Docker execution | Not run locally: Docker/Compose executables are unavailable. Image build and isolated nonroot startup/readiness are CI gates. |

## Meaningful scenarios exercised

- All 16 tenant tables use ENABLE/FORCE RLS with dedicated ownership. No-context access, cross-tenant reads/writes/foreign keys, owner/DDL privilege attempts and context reuse after commit/error are checked with real PostgreSQL.
- Both app and migrator connections to unrelated databases fail under the test HBA even while PostgreSQL's default PUBLIC CONNECT grant remains true. A second isolated cluster also validated the actual CI and Compose HBA templates for both logins, including Unix-socket denials and zero parsing errors. Production HBA installation remains a separate operator gate.
- The named migrator has SET-only owner membership and no inherited owner authority. Fresh and repeated migrations, a follow-up revision, failed-upgrade rollback, onboarding and channel registration run through its actual login. The version table and all commerce objects retain dedicated ownership. The initial SQL snapshot remains byte-identical to the original PR.
- The legacy version-table handoff accepts only the authenticated provisioning administrator's original table and known revision. Unexpected owners, columns, defaults, constraints, triggers, rewrite rules, inheritance or partitioning are refused. Runtime admission rejects administrator sessions disguised as the app, ownership/elevation drift and both persistent and temporary object-creation privileges.
- Two concurrent checkouts for the last stock unit cannot oversell. Reconfirmation returns the existing order, accepted alias keys remain bound to their original quote, and cancellation/expiry/collection adjust inventory exactly once.
- Tax, hours, price, cart, zone and expiry changes invalidate checkout as appropriate. Concurrent staff acceptance, settings writes and membership demotion have explicit regression coverage.
- JWT signature, algorithm, issuer, audience, expiry, malformed/nested claims and bounded JWKS failure paths fail closed. A tenant header never authorizes access without a persisted active membership.
- Signed ingress rejects malformed/signature/stale/oversized content; a single event replayed 100 times remains one durable event. Customer streams retain FIFO under concurrent workers and pending retries, while unrelated customers progress.
- Staff opt-out wins over an older queued START. Human takeover serializes with in-flight submission and prevents later queued bot replies. Conversation/order identifiers cannot escape their customer or tenant scope.
- Policy retries retain command/idempotency IDs; dead letters preserve identity on replay. Asynchronous delivery references match only the same tenant/channel/command, and mismatches remain visible.
- Raw response sizes, slow headers/body streams and executor-backed DNS respect the caller deadline. The previously failing 0.03-second policy deadline returned in 0.0292 seconds after correction; resolver completion caused no late submission in the reviewer regression. Worker/queue/admission capacity and shutdown are bounded.
- A real `pg_dump -Fc` / `pg_restore` into a unique recovery database preserved pending inbox/outbox records, command IDs, dedicated table ownership and FORCE RLS. The disposable recovery database was removed after validation.

## Independent review

Separate reviewers assessed application/persistence authorization and the messaging/identity boundary. All reported material findings were reproduced and corrected. The application reviewer verified 11 focused correction checks; the messaging reviewer verified 14 PostgreSQL regressions and a later 46-test HTTP/auth/runtime correction suite. Details and design rulings are summarized in `ledger.md`.

The next-phase migrator review independently reproduced a missed `TEMPORARY` database privilege: an app with no persistent CREATE permission could create temporary tables and pass the old guard. After correction, the reviewer confirmed clean access succeeds, the extra privilege causes refusal, and revocation restores safe admission. No unresolved material findings remained in that review.

These are independent technical reviews within the implementation session. Required external repository reviews, status checks and promotion approvals remain governed by the project's normal branch process.

## Commands and prerequisites

The full suite was executed with an explicitly isolated `TEST_DATABASE_ADMIN_URL`, a `codestra_whatsapp_commerce_test...` target, matching PostgreSQL client binaries, and HBA restrictions for both dedicated logins. Credentials were read privately by the runner and were not printed or committed.

```bash
uv run --frozen --extra dev pytest -q
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev ruff format --check .
uv build --wheel
uv export --frozen --extra dev --no-emit-project --format requirements-txt --output-file /tmp/commerce-audit-requirements.txt
uv run --frozen --extra dev pip-audit --disable-pip --no-deps -r /tmp/commerce-audit-requirements.txt
```

## External gates still required

The implementation keeps outbound delivery disabled. Production admission requires the actual policy/middleware/provider contract tests, campaign propagation, durable downstream idempotency, asynchronous delivery correlation, per-business channel ownership, hosted database capacity and effective role/HBA checks, and production-specific backup/restore evidence. See `docs/integration-contract.md` and `docs/operations.md` for the concrete requirements.
