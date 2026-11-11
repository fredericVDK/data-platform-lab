#!/usr/bin/env bash
# Nachtelijke back-up van PostgreSQL: database steelline (pg_dump, custom formaat)
# + rollen en hun wachtwoorden (pg_dumpall --globals-only).
#
# Cron (crontab -e, gebruiker andre), elke nacht om 03:15 na de ETL van 02:00:
#   15 3 * * * /home/andre/data-platform-lab/infra/backup.sh >> /home/andre/backups/backup.log 2>&1
#
# Instellingen komen uit infra/.env (zie .env.example). Herstellen: docs/restore.md
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/.env" ]]; then set -a; source "$SCRIPT_DIR/.env"; set +a; fi

CONTAINER="${PG_CONTAINER:-postgres}"
DB="${PG_DB:-steelline}"
PG_ADMIN="${PG_ADMIN_USER:-admin}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/postgres}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-7}"
STAMP="$(date +%Y-%m-%d_%H%M)"

DUMP="$BACKUP_DIR/${DB}_${STAMP}.dump"
GLOBALS="$BACKUP_DIR/globals_${STAMP}.sql"
SUMS="$BACKUP_DIR/SHA256SUMS_${STAMP}"

log()  { echo "$(date '+%F %T') $*"; }
push() {  # meld de status aan een Uptime Kuma push-monitor (optioneel)
  [[ -n "${KUMA_PUSH_URL:-}" ]] || return 0
  curl -fsS -m 10 --get --data-urlencode "status=$1" --data-urlencode "msg=$2" \
       "$KUMA_PUSH_URL" >/dev/null || log "WAARSCHUWING: Uptime Kuma niet bereikbaar"
}
fail() { trap - ERR; log "FOUT: $*"; push down "$*"; rm -f "$BACKUP_DIR"/*.part; exit 1; }
trap 'fail "regel $LINENO: $BASH_COMMAND"' ERR

# nooit twee back-ups tegelijk
exec 9>/tmp/pg-backup.lock
flock -n 9 || fail "vorige back-up loopt nog"

umask 077
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

health="$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo ontbreekt)"
[[ "$health" == healthy ]] || fail "container $CONTAINER is niet healthy ($health)"

log "start back-up van $DB"
docker exec "$CONTAINER" pg_dump -U "$PG_ADMIN" -d "$DB" -Fc > "$DUMP.part"
docker exec "$CONTAINER" pg_dumpall -U "$PG_ADMIN" --globals-only > "$GLOBALS.part"

# controle: archief moet leesbaar zijn en niet leeg
docker exec -i "$CONTAINER" pg_restore --list < "$DUMP.part" > /dev/null
[[ -s "$DUMP.part" && -s "$GLOBALS.part" ]] || fail "lege dump"

mv "$DUMP.part" "$DUMP"
mv "$GLOBALS.part" "$GLOBALS"
( cd "$BACKUP_DIR" && sha256sum "${DUMP##*/}" "${GLOBALS##*/}" > "${SUMS##*/}" )
size="$(du -h "$DUMP" | cut -f1)"
log "dump klaar: ${DUMP##*/} ($size)"

# lokaal: KEEP_DAYS dagen bewaren
find "$BACKUP_DIR" -maxdepth 1 -type f \
     \( -name '*.dump' -o -name 'globals_*.sql' -o -name 'SHA256SUMS_*' \) \
     -mtime +"$((KEEP_DAYS - 1))" -print -delete | sed 's/^/verwijderd: /'

# kopie naar tweede schijf
if [[ -n "${BACKUP_COPY_DIR:-}" ]]; then
  [[ -f "$BACKUP_COPY_DIR/.backup-target" ]] \
    || fail "tweede schijf niet gekoppeld ($BACKUP_COPY_DIR/.backup-target ontbreekt)"
  cp "$DUMP" "$GLOBALS" "$SUMS" "$BACKUP_COPY_DIR/"
  find "$BACKUP_COPY_DIR" -maxdepth 1 -type f \
       \( -name '*.dump' -o -name 'globals_*.sql' -o -name 'SHA256SUMS_*' \) \
       -mtime +"$(( ${BACKUP_COPY_KEEP_DAYS:-30} - 1 ))" -delete
  log "kopie naar $BACKUP_COPY_DIR"
fi

# kopie naar de cloud (copy, geen sync: een lokaal gewiste map wist de cloud niet mee)
if [[ -n "${RCLONE_REMOTE:-}" ]]; then
  rclone copy "$BACKUP_DIR" "$RCLONE_REMOTE" --include "*_${STAMP}*"
  rclone delete "$RCLONE_REMOTE" --min-age "${RCLONE_KEEP_DAYS:-30}d"
  log "kopie naar $RCLONE_REMOTE"
fi

push up "OK $DB $size"
log "back-up geslaagd"
