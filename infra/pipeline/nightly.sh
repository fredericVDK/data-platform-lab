#!/bin/sh
# Nachtelijke run: gemiste dagen genereren en alles laden.
#   1. generator: laatste INHAAL_DAGEN volledige dagen (al gearchiveerde dagen worden overgeslagen,
#      dus na een paar dagen zonder stroom wordt de achterstand vanzelf ingehaald)
#   2. ETL: alles in landing/ -> staging -> dwh, push naar Uptime Kuma
# Exitcode van de ETL wordt doorgegeven (0 = ok, 1 = mislukt), zodat systemd het ziet.
set -eu

APP="$(cd "$(dirname "$0")" && pwd)"
[ -d "$APP/generator" ] || APP="$(cd "$APP/../.." && pwd)"   # ook bruikbaar zonder container

for map in "${LANDING_DIR:-}" "${LABELS_DIR:-}" "${ARCHIVE_DIR:-}" "${REJECTED_DIR:-}" "${ETL_LOG_DIR:-}"; do
    if [ -n "$map" ] && ! { mkdir -p "$map" 2>/dev/null && [ -w "$map" ]; }; then
        echo "FOUT: $map bestaat niet of is niet schrijfbaar voor $(id -u):$(id -g)" >&2
        exit 1
    fi
done

echo "$(date -Iseconds) generator: laatste ${INHAAL_DAGEN:-7} dagen aanvullen"
python "$APP/generator/generate.py" --historiek "${INHAAL_DAGEN:-7}"

echo "$(date -Iseconds) ETL"
exec python "$APP/etl/load.py"
