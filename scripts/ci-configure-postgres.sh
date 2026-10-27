#!/usr/bin/env bash
set -euo pipefail
# Only the disposable GitHub Actions PostgreSQL service may be configured here.
: "${PG_SERVICE_CONTAINER:?Expected the job.services.postgres.id container}"
: "${PGPASSWORD:?Expected the disposable service bootstrap password}"
: "${GITHUB_ENV:?Expected a GitHub Actions environment file}"
export PGHOST=127.0.0.1 PGPORT=55483 PGUSER=commerce_ci_admin PGDATABASE=postgres

docker exec --user postgres -i "$PG_SERVICE_CONTAINER" sh -c 'cat > "$PGDATA/pg_hba.conf"' <<'HBA'
# Test databases include the uniquely named restore targets. First match wins.
host "/^codestra_whatsapp_commerce_test.*$" codestra_whatsapp_app 0.0.0.0/0 scram-sha-256
host "/^codestra_whatsapp_commerce_test.*$" codestra_whatsapp_app ::/0 scram-sha-256
host all codestra_whatsapp_app 0.0.0.0/0 reject
host all codestra_whatsapp_app ::/0 reject
local all codestra_whatsapp_app reject
local all all scram-sha-256
host all all 0.0.0.0/0 scram-sha-256
host all all ::/0 scram-sha-256
HBA

psql -X -v ON_ERROR_STOP=1 -c 'SELECT pg_reload_conf()' >/dev/null
admin_password="$(python -c 'import secrets; print(secrets.token_hex(32))')"
app_password="$(python -c 'import secrets; print(secrets.token_hex(32))')"
printf '::add-mask::%s\n' "$admin_password"
printf '::add-mask::%s\n' "$app_password"
# psql variables quote the fresh password as a SQL literal; never print it.
psql -X -v ON_ERROR_STOP=1 --set=new_password="$admin_password" >/dev/null <<'SQL'
ALTER ROLE commerce_ci_admin PASSWORD :'new_password';
SQL
printf 'TEST_DATABASE_ADMIN_URL=postgresql+psycopg://commerce_ci_admin:%s@127.0.0.1:55483/postgres\n' "$admin_password" >> "$GITHUB_ENV"
printf 'TEST_DATABASE_APP_PASSWORD=%s\n' "$app_password" >> "$GITHUB_ENV"
printf 'TEST_POSTGRES_BIN=/usr/lib/postgresql/16/bin\n' >> "$GITHUB_ENV"
printf 'COMMERCE_TEST_DATABASE_NAME=codestra_whatsapp_commerce_test_ci\n' >> "$GITHUB_ENV"
export PGPASSWORD="$admin_password"
psql -X -v ON_ERROR_STOP=1 <<'SQL'
SELECT version();
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_hba_file_rules WHERE error IS NOT NULL) THEN
    RAISE EXCEPTION 'CI HBA contains invalid rules';
  END IF;
END; $$;
SQL
/usr/lib/postgresql/16/bin/pg_dump --version
/usr/lib/postgresql/16/bin/pg_restore --version
