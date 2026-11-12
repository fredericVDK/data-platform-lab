-- =====================================================================
-- 002_staging.sql  -  Staging-schema: landingszone voor ruwe data
-- =====================================================================
-- Stroom per nachtelijke run:
--
--   landing/*.csv ──COPY──▶ staging.*_raw ──validatie──▶ dwh.fact_*
--                                  │
--                                  └──▶ staging.rejected_row (met reden)
--
-- Principes:
--   * Alle bronkolommen zijn TEXT. Een fout getal of datum laat de
--     COPY dus nooit crashen; de validatie vangt het op en logt het.
--   * Staging bevat natuurlijke sleutels ('WW1', 'S355J2'); pas bij
--     het laden naar dwh worden ze omgezet naar surrogaatsleutels.
--   * Elke run krijgt een load_id in staging.load_batch (lineage +
--     monitoring). Ruwe tabellen worden per run geleegd, het logboek
--     en de afgekeurde rijen blijven bewaard.
--   * bi_reader heeft GEEN rechten op staging (zie 01-roles.sh).
-- =====================================================================

\set ON_ERROR_STOP on
SET client_min_messages = warning;   -- geen "already exists"-meldingen bij herhalen

BEGIN;

SET ROLE etl_writer;
SET search_path = staging;

-- Logboek van ETL-runs -------------------------------------------------
CREATE TABLE IF NOT EXISTS load_batch (
    load_id         bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bron            text         NOT NULL,                  -- 'generator', 'webapp', 'herlaad'
    bestand         text,                                   -- 'production_2026-11-12.csv'
    productiedag    date,                                   -- dag waarover de data gaat
    gestart_op      timestamptz  NOT NULL DEFAULT now(),
    beeindigd_op    timestamptz,
    status          text         NOT NULL DEFAULT 'bezig'
                        CHECK (status IN ('bezig', 'geslaagd', 'deels', 'mislukt')),
    rijen_gelezen   integer      NOT NULL DEFAULT 0,
    rijen_geladen   integer      NOT NULL DEFAULT 0,
    rijen_afgekeurd integer      NOT NULL DEFAULT 0,
    foutmelding     text
);
CREATE INDEX IF NOT EXISTS ix_load_batch_dag ON load_batch (productiedag);

-- Ruwe productiedata (1 rij = 1 CSV-regel) ----------------------------
CREATE TABLE IF NOT EXISTS production_raw (
    load_id               bigint  NOT NULL REFERENCES load_batch (load_id),
    regelnummer           integer NOT NULL,                 -- regel in het bronbestand
    coil_id               text,
    productie_datum       text,                             -- '2026-11-12'
    ploeg                 text,                             -- 'vroeg' / 'laat' / 'nacht'
    installatie_code      text,                             -- 'WW1'
    staalkwaliteit        text,                             -- 'S355J2'
    start_ts              text,                             -- ISO 8601 met tijdzone
    weight_t              text,
    thickness_mm          text,
    target_thickness_mm   text,
    temp_c                text,
    duration_min          text,
    is_defect             text,                             -- 'true'/'false'/'1'/'0'
    defect_type           text,
    PRIMARY KEY (load_id, regelnummer)
);

-- Ruwe stilstanddata ---------------------------------------------------
CREATE TABLE IF NOT EXISTS downtime_raw (
    load_id               bigint  NOT NULL REFERENCES load_batch (load_id),
    regelnummer           integer NOT NULL,
    event_id              text,
    bron                  text,                             -- 'sensor' / 'app'
    installatie_code      text,
    reden_code            text,                             -- 'MECH-LAGER' (mag leeg)
    start_ts              text,
    end_ts                text,
    gepland               text,                             -- 'j'/'n', 'true'/'false'
    opmerking             text,
    PRIMARY KEY (load_id, regelnummer)
);

-- Afgekeurde rijen: nooit stilletjes data weggooien --------------------
CREATE TABLE IF NOT EXISTS rejected_row (
    rejected_id     bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    load_id         bigint       NOT NULL REFERENCES load_batch (load_id),
    bron_tabel      text         NOT NULL,                  -- 'production_raw' / 'downtime_raw'
    regelnummer     integer      NOT NULL,
    regel           text         NOT NULL,                  -- welke validatieregel faalde, bv. 'R03_dikte_buiten_kwaliteit'
    reden           text         NOT NULL,                  -- leesbare uitleg
    rij             jsonb        NOT NULL,                  -- de volledige ruwe rij
    afgekeurd_op    timestamptz  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_rejected_load  ON rejected_row (load_id);
CREATE INDEX IF NOT EXISTS ix_rejected_regel ON rejected_row (regel);

COMMENT ON SCHEMA staging IS 'Landingszone: ruwe data (alles TEXT) + ETL-logboek. Niet voor rapportering.';
COMMENT ON TABLE  load_batch     IS '1 rij per ETL-run; load_id komt terug in dwh.fact_* voor lineage.';
COMMENT ON TABLE  production_raw IS 'Ruwe productie-CSV, per run geleegd (TRUNCATE).';
COMMENT ON TABLE  downtime_raw   IS 'Ruwe stilstand-CSV, per run geleegd (TRUNCATE).';
COMMENT ON TABLE  rejected_row   IS 'Rijen die de validatie niet doorstonden, met reden.';

RESET ROLE;
COMMIT;
