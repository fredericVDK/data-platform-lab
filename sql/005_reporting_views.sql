-- =====================================================================
-- 005_reporting_views.sql  -  Views voor Power BI
-- =====================================================================
--   dwh.vw_oee_shift            OEE per dag x ploeg x installatie
--   dwh.vw_daily_oee            OEE per dag x installatie
--   dwh.vw_downtime_by_reason   stilstand per dag x installatie x oorzaak
--
-- OEE = beschikbaarheid x prestatie x kwaliteit
--
--   bemande tijd          duur van de ploeg (8 u) als de installatie die ploeg
--                         draaide (er is productie of stilstand geregistreerd)
--   geplande prod.tijd    bemande tijd - gepland onderhoud
--   looptijd              geplande prod.tijd - alle andere stilstand
--                         (storingen, wissels, wachten op materiaal)
--   beschikbaarheid       looptijd / geplande prod.tijd
--   prestatie             ideale tijd / looptijd
--                         ideale tijd = ton / capaciteit (t/u) van de installatie
--   kwaliteit             ton zonder defect / ton
--
-- De views bevatten ook alle MINUTEN en TONNEN, niet enkel de percentages.
-- Tel in Power BI altijd de minuten op en deel daarna (een gemiddelde van
-- percentages over dagen of installaties is fout).
--
-- Let op: LEAST() negeert NULL in PostgreSQL (LEAST(NULL, 1) = 1), daarom
-- de CASE rond prestatie.
--
-- Vereenvoudiging: een ploeg telt altijd 8 u, ook in de nacht van de
-- overgang naar zomer-/wintertijd (echt 7 of 9 u).
-- =====================================================================

\set ON_ERROR_STOP on
SET client_min_messages = warning;

BEGIN;

SET ROLE etl_writer;
SET search_path = dwh;

DROP VIEW IF EXISTS vw_daily_oee;   -- hangt af van vw_oee_shift
DROP VIEW IF EXISTS vw_oee_shift;

CREATE VIEW vw_oee_shift AS
WITH actief AS (
    SELECT date_key, shift_key, installation_key FROM fact_production
    UNION
    SELECT date_key, shift_key, installation_key FROM fact_downtime
),
prod AS (
    SELECT f.date_key, f.shift_key, f.installation_key,
           count(*)                                         AS rollen,
           count(*) FILTER (WHERE f.is_defect)              AS rollen_defect,
           sum(f.weight_t)                                  AS ton,
           coalesce(sum(f.weight_t) FILTER (WHERE NOT f.is_defect), 0) AS ton_goed,
           sum(f.duration_min)                              AS productie_min,
           sum(f.weight_t / i.capaciteit_t_per_uur * 60)    AS ideale_min
    FROM fact_production f
    JOIN dim_installation i USING (installation_key)
    GROUP BY f.date_key, f.shift_key, f.installation_key
),
stil AS (
    SELECT d.date_key, d.shift_key, d.installation_key,
           coalesce(sum(d.duration_min) FILTER (WHERE r.categorie = 'gepland onderhoud'), 0)  AS onderhoud_min,
           coalesce(sum(d.duration_min) FILTER (WHERE r.categorie <> 'gepland onderhoud'), 0) AS stilstand_min,
           coalesce(sum(d.duration_min) FILTER (WHERE NOT d.planned), 0)                      AS ongepland_min,
           count(*) FILTER (WHERE NOT d.planned)                                              AS storingen
    FROM fact_downtime d
    JOIN dim_downtime_reason r USING (reason_key)
    WHERE d.end_ts IS NOT NULL                          -- lopende stilstanden tellen nog niet mee
    GROUP BY d.date_key, d.shift_key, d.installation_key
),
basis AS (
    SELECT a.date_key, a.shift_key, a.installation_key,
           (extract(epoch FROM CASE WHEN s.eind_tijd > s.start_tijd THEN s.eind_tijd - s.start_tijd
                                    ELSE s.eind_tijd - s.start_tijd + interval '24 hours' END) / 60
           )::numeric                                   AS bemand_min,
           coalesce(p.rollen, 0)                        AS rollen,
           coalesce(p.rollen_defect, 0)                 AS rollen_defect,
           coalesce(p.ton, 0)                           AS ton,
           coalesce(p.ton_goed, 0)                      AS ton_goed,
           coalesce(p.productie_min, 0)                 AS productie_min,
           coalesce(p.ideale_min, 0)                    AS ideale_min,
           coalesce(st.onderhoud_min, 0)                AS onderhoud_min,
           coalesce(st.stilstand_min, 0)                AS stilstand_min,
           coalesce(st.ongepland_min, 0)                AS ongepland_min,
           coalesce(st.storingen, 0)                    AS storingen
    FROM actief a
    JOIN dim_shift s USING (shift_key)
    LEFT JOIN prod p  USING (date_key, shift_key, installation_key)
    LEFT JOIN stil st USING (date_key, shift_key, installation_key)
),
tijden AS (
    SELECT b.*,
           greatest(b.bemand_min - b.onderhoud_min, 0)                    AS gepland_min,
           greatest(b.bemand_min - b.onderhoud_min - b.stilstand_min, 0)  AS looptijd_min
    FROM basis b
)
SELECT date_key, shift_key, installation_key,
       bemand_min, onderhoud_min, gepland_min, stilstand_min, ongepland_min, looptijd_min,
       productie_min, ideale_min, storingen,
       rollen, rollen_defect, round(ton, 3) AS ton, round(ton_goed, 3) AS ton_goed,
       round(looptijd_min / nullif(gepland_min, 0), 4)                      AS beschikbaarheid,
       round(CASE WHEN looptijd_min > 0 THEN least(ideale_min / looptijd_min, 1) END, 4) AS prestatie,
       round(ton_goed / nullif(ton, 0), 4)                                  AS kwaliteit,
       -- NULL als er geen geplande productietijd was (bv. hele ploeg onderhoud);
       -- 0 als er wel gepland was maar niets geproduceerd (bv. hele ploeg wachten)
       round(CASE WHEN gepland_min > 0 THEN
                 looptijd_min / gepland_min
               * CASE WHEN looptijd_min > 0 THEN least(ideale_min / looptijd_min, 1) ELSE 0 END
               * coalesce(ton_goed / nullif(ton, 0), 1)
             END, 4)                                                        AS oee
FROM tijden;

CREATE VIEW vw_daily_oee AS
WITH som AS (
    SELECT date_key, installation_key,
           count(*)            AS ploegen,
           sum(bemand_min)     AS bemand_min,
           sum(onderhoud_min)  AS onderhoud_min,
           sum(gepland_min)    AS gepland_min,
           sum(stilstand_min)  AS stilstand_min,
           sum(ongepland_min)  AS ongepland_min,
           sum(looptijd_min)   AS looptijd_min,
           sum(productie_min)  AS productie_min,
           sum(ideale_min)     AS ideale_min,
           sum(storingen)      AS storingen,
           sum(rollen)         AS rollen,
           sum(rollen_defect)  AS rollen_defect,
           sum(ton)            AS ton,
           sum(ton_goed)       AS ton_goed
    FROM vw_oee_shift
    GROUP BY date_key, installation_key
)
SELECT s.*,
       d.datum, i.installation_code, i.naam AS installatie,
       round(looptijd_min / nullif(gepland_min, 0), 4)                      AS beschikbaarheid,
       round(CASE WHEN looptijd_min > 0 THEN least(ideale_min / looptijd_min, 1) END, 4) AS prestatie,
       round(ton_goed / nullif(ton, 0), 4)                                  AS kwaliteit,
       -- NULL als er geen geplande productietijd was (bv. hele ploeg onderhoud);
       -- 0 als er wel gepland was maar niets geproduceerd (bv. hele ploeg wachten)
       round(CASE WHEN gepland_min > 0 THEN
                 looptijd_min / gepland_min
               * CASE WHEN looptijd_min > 0 THEN least(ideale_min / looptijd_min, 1) ELSE 0 END
               * coalesce(ton_goed / nullif(ton, 0), 1)
             END, 4)                                                        AS oee,
       round(rollen_defect::numeric / nullif(rollen, 0), 4)                 AS uitval_pct
FROM som s
JOIN dim_date d USING (date_key)
JOIN dim_installation i USING (installation_key);

CREATE OR REPLACE VIEW vw_downtime_by_reason AS
SELECT d.date_key, d.installation_key, d.reason_key,
       dt.datum, i.installation_code, r.reason_code, r.categorie, r.omschrijving,
       d.planned,
       count(*)                          AS aantal,
       round(sum(d.duration_min), 2)     AS minuten,
       round(avg(d.duration_min), 2)     AS gem_minuten,
       round(max(d.duration_min), 2)     AS max_minuten
FROM fact_downtime d
JOIN dim_date dt            USING (date_key)
JOIN dim_installation i     USING (installation_key)
JOIN dim_downtime_reason r  USING (reason_key)
WHERE d.end_ts IS NOT NULL
GROUP BY d.date_key, d.installation_key, d.reason_key,
         dt.datum, i.installation_code, r.reason_code, r.categorie, r.omschrijving, d.planned;

COMMENT ON VIEW vw_oee_shift          IS 'OEE-bouwstenen per dag x ploeg x installatie (minuten, tonnen, ratio''s).';
COMMENT ON VIEW vw_daily_oee          IS 'OEE per dag x installatie. Tel minuten op en deel daarna; middel geen percentages.';
COMMENT ON VIEW vw_downtime_by_reason IS 'Stilstand per dag x installatie x oorzaak x gepland.';

-- Views vallen onder de DEFAULT PRIVILEGES "ON TABLES" uit 01-roles.sh;
-- voor de zekerheid nog eens expliciet:
GRANT SELECT ON vw_oee_shift, vw_daily_oee, vw_downtime_by_reason TO bi_reader;

RESET ROLE;
COMMIT;
