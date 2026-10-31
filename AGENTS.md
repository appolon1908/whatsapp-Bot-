# Codestra WhatsApp Bot agent contract

- Scope: the Python commerce API, worker, commerce data, and tests in this repository.
- Provider effects go through the existing WhatsApp policy API and Middleware V3. Never call Meta, Evolution or Baileys directly.
- Production sends and AI automatic replies default OFF. Do not activate real channels as part of source implementation.
- Commerce uses a separate PostgreSQL database. Do not modify controller, installation-authority, CRM, or unrelated databases.
- Authenticate staff with verified JWTs and enforce database-backed business memberships. Never trust unsigned tenant or role claims.
- Runtime roles must not own tables, be superusers, or bypass row-level security. Use transaction-local tenant context, composite tenant foreign keys, and real PostgreSQL tests.
- Preserve orders and inventory consistency when messages, retries, or workers fail. Consent and human takeover must be enforced at dispatch time.
- No credentials, customer records, provider tokens or live connection strings in commits or logs.
- Publication: atomic_task -> subsection -> section -> development -> testing -> staging -> production. No force pushes, protection bypass, invented certification, or direct protected-branch implementation.
- One writer for each assigned file set. Record exact source SHA and meaningful test evidence. Root coordinates commits and publication.
