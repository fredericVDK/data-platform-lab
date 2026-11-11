#!/bin/bash
# Draait ENKEL bij de eerste start (lege pgdata-volume).
# Maakt de schema's en de twee applicatiegebruikers aan.
set -euo pipefail

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v db="$POSTGRES_DB" \
     -v etl_pw="$ETL_WRITER_PASSWORD" \
     -v bi_pw="$BI_READER_PASSWORD" <<'EOSQL'

-- 1. Gebruikers
CREATE ROLE etl_writer LOGIN PASSWORD :'etl_pw';
CREATE ROLE bi_reader  LOGIN PASSWORD :'bi_pw';

-- 2. Niemand mag standaard verbinden of iets aanmaken in public
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO etl_writer, bi_reader;
REVOKE ALL ON SCHEMA public FROM PUBLIC;

-- 3. Schema's: staging (ruwe loads) en dwh (sterschema)
--    etl_writer is eigenaar -> mag tabellen maken, laden, truncaten
CREATE SCHEMA staging AUTHORIZATION etl_writer;
CREATE SCHEMA dwh     AUTHORIZATION etl_writer;

-- 4. bi_reader: alleen lezen op het sterschema (niet op staging)
GRANT USAGE ON SCHEMA dwh TO bi_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA dwh TO bi_reader;
-- ook voor tabellen/views die etl_writer LATER aanmaakt
ALTER DEFAULT PRIVILEGES FOR ROLE etl_writer IN SCHEMA dwh
  GRANT SELECT ON TABLES TO bi_reader;

-- 5. Extra vangnet voor de leesgebruiker
ALTER ROLE bi_reader SET default_transaction_read_only = on;
ALTER ROLE bi_reader SET statement_timeout = '60s';

-- handig: zoekpad zodat je dwh.* niet altijd hoeft te typen
ALTER ROLE etl_writer SET search_path = dwh, staging;
ALTER ROLE bi_reader  SET search_path = dwh;
EOSQL

echo "01-roles.sh: etl_writer en bi_reader aangemaakt."
