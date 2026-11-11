#!/usr/bin/env bash
# Restore-test: zet de nieuwste back-up (of het opgegeven .dump-bestand) terug in een
# WEGWERP-container en vergelijkt schema's, rollen en rijen met de productiedatabase.
# Productie wordt niet aangeraakt.
#
#   infra/restore-test.sh
#   infra/restore-test.sh ~/backups/postgres/steelline_2026-11-12_0315.dump
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/.env" ]]; then set -a; source "$SCRIPT_DIR/.env"; set +a; fi

PROD="${PG_CONTAINER:-postgres}"
DB="${PG_DB:-steelline}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/postgres}"
TEST=pg_restore_test

DUMP="${1:-$(ls -1t "$BACKUP_DIR"/"${DB}"_*.dump 2>/dev/null | head -n1 || true)}"
[[ -f "$DUMP" ]] || { echo "Geen back-up gevonden in $BACKUP_DIR"; exit 1; }
DIR="$(dirname "$DUMP")"
STAMP="$(basename "$DUMP" .dump)"; STAMP="${STAMP#"${DB}"_}"
GLOBALS="$DIR/globals_${STAMP}.sql"

echo "== Back-up: $(basename "$DUMP")"
echo "== 1. Checksums controleren"
( cd "$DIR" && sha256sum -c "SHA256SUMS_${STAMP}" )

echo "== 2. Wegwerp-container starten"
docker rm -f "$TEST" >/dev/null 2>&1 || true
trap 'docker rm -f "$TEST" >/dev/null 2>&1 || true' EXIT
SECONDS=0
docker run -d --name "$TEST" -e POSTGRES_USER=admin \
       -e POSTGRES_PASSWORD="$(openssl rand -hex 16)" postgres:16 >/dev/null
# -h 127.0.0.1: tijdens de init luistert de server enkel op de socket
for _ in $(seq 60); do
  docker exec "$TEST" pg_isready -h 127.0.0.1 -U admin -q 2>/dev/null && break
  sleep 1
done
docker exec "$TEST" pg_isready -h 127.0.0.1 -U admin -q

echo "== 3. Rollen terugzetten (de fout 'role \"admin\" already exists' is normaal)"
docker exec -i "$TEST" psql -q -U admin -d postgres < "$GLOBALS" 2>&1 \
  | grep -v 'role "admin" already exists' || true

echo "== 4. Database terugzetten"
docker exec -i "$TEST" pg_restore -U admin -d postgres --create --exit-on-error < "$DUMP"
duur=$SECONDS

echo "== 5. Vergelijken met productie"
CHECK_SQL=$(cat <<'SQL'
SELECT 'schema ' || nspname FROM pg_namespace
 WHERE nspname NOT LIKE 'pg\_%' AND nspname <> 'information_schema'
UNION ALL
SELECT 'rol ' || rolname FROM pg_roles WHERE rolname NOT LIKE 'pg\_%'
UNION ALL
SELECT 'tabel ' || table_schema || '.' || table_name || ' = ' ||
       (xpath('/row/c/text()', query_to_xml(
          format('SELECT count(*) AS c FROM %I.%I', table_schema, table_name),
          false, true, '')))[1]::text
  FROM information_schema.tables
 WHERE table_type = 'BASE TABLE'
   AND table_schema NOT IN ('pg_catalog', 'information_schema')
ORDER BY 1;
SQL
)
q() { docker exec "$1" psql -U admin -d "$DB" -AtX -c "$CHECK_SQL"; }
q "$TEST" > /tmp/restore_test.txt
cat /tmp/restore_test.txt

grep -qx 'schema dwh' /tmp/restore_test.txt && grep -qx 'schema staging' /tmp/restore_test.txt \
  || { echo "MISLUKT: schema's dwh/staging ontbreken"; exit 1; }

if docker ps --format '{{.Names}}' | grep -qx "$PROD"; then
  if diff <(q "$PROD") /tmp/restore_test.txt > /tmp/restore_diff.txt; then
    echo "Identiek aan productie."
  else
    echo "Verschillen met productie (normaal als er sinds de back-up data is bijgekomen):"
    cat /tmp/restore_diff.txt
  fi
fi

echo
echo "GESLAAGD in ${duur}s. Regel voor docs/restore.md:"
echo "| $(date +%F) | $(basename "$DUMP") | ${duur}s | geslaagd (script) | |"
