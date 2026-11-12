-- =====================================================================
-- 004_etl_run_log.sql  -  Logboek per ETL-run
-- =====================================================================
--   staging.etl_run_log   1 rij per uitvoering van etl/load.py
--   staging.load_batch    1 rij per verwerkt bestand (bestond al), nu met run_id
--   dwh.vw_etl_status     leesbaar voor bi_reader ("data bijgewerkt op ...")
-- =====================================================================

\set ON_ERROR_STOP on
SET client_min_messages = warning;

BEGIN;

SET ROLE etl_writer;

CREATE TABLE IF NOT EXISTS staging.etl_run_log (
    run_id            bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    gestart_op        timestamptz  NOT NULL DEFAULT now(),
    beeindigd_op      timestamptz,
    status            text         NOT NULL DEFAULT 'bezig'
                          CHECK (status IN ('bezig', 'geslaagd', 'deels', 'mislukt', 'niets te doen')),
    bestanden         integer      NOT NULL DEFAULT 0,
    bestanden_mislukt integer      NOT NULL DEFAULT 0,
    rijen_gelezen     integer      NOT NULL DEFAULT 0,
    rijen_geladen     integer      NOT NULL DEFAULT 0,
    rijen_afgekeurd   integer      NOT NULL DEFAULT 0,
    host              text,
    foutmelding       text
);
CREATE INDEX IF NOT EXISTS ix_etl_run_log_gestart ON staging.etl_run_log (gestart_op DESC);

ALTER TABLE staging.load_batch
    ADD COLUMN IF NOT EXISTS run_id           bigint REFERENCES staging.etl_run_log (run_id),
    ADD COLUMN IF NOT EXISTS rijen_nieuw      integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS rijen_bijgewerkt integer NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS ix_load_batch_run ON staging.load_batch (run_id);

COMMENT ON TABLE staging.etl_run_log IS '1 rij per ETL-run (etl/load.py). Zie ook staging.load_batch per bestand.';
COMMENT ON COLUMN staging.load_batch.rijen_nieuw      IS 'Rijen die nieuw in dwh kwamen.';
COMMENT ON COLUMN staging.load_batch.rijen_bijgewerkt IS 'Rijen die al bestonden en overschreven werden (herladen).';

-- bi_reader heeft geen rechten op staging; een view in dwh draait met de
-- rechten van de eigenaar (etl_writer) en toont enkel deze kolommen.
CREATE OR REPLACE VIEW dwh.vw_etl_status AS
SELECT run_id, gestart_op, beeindigd_op, status, bestanden, bestanden_mislukt,
       rijen_geladen, rijen_afgekeurd
FROM staging.etl_run_log;

COMMENT ON VIEW dwh.vw_etl_status IS 'ETL-runs, voor "laatst bijgewerkt" in Power BI en monitoring.';

RESET ROLE;
COMMIT;
