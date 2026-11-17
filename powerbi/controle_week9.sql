-- =====================================================================
-- controle_week9.sql  -  dezelfde getallen als de week 9-metingen, in SQL
-- =====================================================================
-- Gebruik: in DBeaver (als bi_reader), of op de server:
--   docker exec -i postgres psql -U bi_reader -d steelline -f - < powerbi/controle_week9.sql
-- Zet in Power BI dezelfde filters (zie de kop van elke query) en vergelijk.
-- Kleine afrondingsverschillen zijn normaal; alles daarboven is een fout
-- in een meting of een relatie.
-- =====================================================================

-- 1. Vorige kalendermaand per installatie
--    Power BI: tabel met Installatie[installation_code] en de metingen,
--    slicer Datum = vorige maand.
--    Storingen = alleen mechanisch en elektrisch (zie measures.dax).
WITH periode AS (
    SELECT date_key FROM dwh.dim_date
    WHERE datum >= date_trunc('month', current_date) - interval '1 month'
      AND datum <  date_trunc('month', current_date)
),
oee AS (
    SELECT installation_key,
           sum(ton) ton, sum(ton_goed) ton_goed, sum(rollen) rollen, sum(rollen_defect) rollen_defect,
           sum(gepland_min) gepland, sum(looptijd_min) looptijd, sum(ideale_min) ideaal
    FROM dwh.vw_oee_shift JOIN periode USING (date_key)
    GROUP BY installation_key
),
storing AS (
    SELECT installation_key, sum(aantal) aantal, sum(minuten) minuten
    FROM dwh.vw_downtime_by_reason JOIN periode USING (date_key)
    WHERE NOT planned AND categorie IN ('mechanisch', 'elektrisch')
    GROUP BY installation_key
)
SELECT i.installation_code                                            AS installatie,
       round(o.ton)                                                   AS "Productie (t)",
       round(o.looptijd / 60, 1)                                      AS "Draaiuren (u)",
       round(o.ton / nullif(o.looptijd / 60, 0), 1)                   AS "Doorvoer (t/u)",
       round(100.0 * o.rollen_defect / nullif(o.rollen, 0), 2)        AS "Uitval %",
       s.aantal                                                       AS "Storingen",
       round(o.looptijd / nullif(s.aantal, 0))                        AS "MTBF (min)",
       round(s.minuten / nullif(s.aantal, 0), 1)                      AS "MTTR (min)",
       round(100 * o.looptijd / nullif(o.gepland, 0)
                 * least(o.ideaal / nullif(o.looptijd, 0), 1)
                 * o.ton_goed / nullif(o.ton, 0), 1)                  AS "OEE %"
FROM oee o
JOIN dwh.dim_installation i USING (installation_key)
LEFT JOIN storing s USING (installation_key)
ORDER BY i.lijn DESC, i.volgorde_in_lijn;

-- 2. % ongepland per installatie, vorige kalendermaand
--    Power BI: [% ongepland] en [Stilstand (min)] per installatie.
SELECT i.installation_code                                                        AS installatie,
       round(sum(s.minuten))                                                      AS "Stilstand (min)",
       round(sum(s.minuten) FILTER (WHERE NOT s.planned))                         AS "ongepland (min)",
       round(100 * sum(s.minuten) FILTER (WHERE NOT s.planned) / nullif(sum(s.minuten), 0), 1) AS "% ongepland"
FROM dwh.vw_downtime_by_reason s
JOIN dwh.dim_date d         USING (date_key)
JOIN dwh.dim_installation i USING (installation_key)
WHERE d.datum >= date_trunc('month', current_date) - interval '1 month'
  AND d.datum <  date_trunc('month', current_date)
GROUP BY i.installation_code
ORDER BY 4 DESC;

-- 3. Deze maand tot gisteren tegenover dezelfde dagen vorige maand (alle installaties)
--    Power BI: kaart [OEE], [OEE vorige maand], [OEE verschil vs vorige maand (pp)],
--    slicer Datum = 1e van deze maand t/m gisteren.
WITH per AS (
    SELECT CASE WHEN d.datum >= date_trunc('month', current_date) THEN 'deze maand' ELSE 'vorige maand' END AS periode,
           sum(o.gepland_min) g, sum(o.looptijd_min) l, sum(o.ideale_min) i,
           sum(o.ton) t, sum(o.ton_goed) tg, sum(o.rollen) r, sum(o.rollen_defect) rd
    FROM dwh.vw_oee_shift o JOIN dwh.dim_date d USING (date_key)
    WHERE (d.datum >= date_trunc('month', current_date) AND d.datum < current_date)
       OR (d.datum >= date_trunc('month', current_date) - interval '1 month'
           AND d.datum < (current_date - interval '1 month')::date)
    GROUP BY 1
)
SELECT periode,
       round(t)                                                       AS "Productie (t)",
       round(100 * l / g * least(i / l, 1) * tg / t, 1)               AS "OEE %",
       round(100.0 * rd / r, 2)                                       AS "Uitval %"
FROM per ORDER BY periode;

-- 4. OEE 7d (gewogen) voor de laatste 10 dagen, alle installaties
--    Power BI: lijngrafiek Datum[datum] x [OEE 7d].
WITH dag AS (
    SELECT d.datum, sum(o.gepland_min) g, sum(o.looptijd_min) l, sum(o.ideale_min) i,
           sum(o.ton) t, sum(o.ton_goed) tg
    FROM dwh.vw_oee_shift o JOIN dwh.dim_date d USING (date_key)
    GROUP BY d.datum
)
SELECT a.datum,
       round(100 * sum(b.l) / sum(b.g) * least(sum(b.i) / sum(b.l), 1) * sum(b.tg) / sum(b.t), 1) AS "OEE 7d %"
FROM dag a
JOIN dag b ON b.datum BETWEEN a.datum - 6 AND a.datum
WHERE a.datum >= current_date - 10
GROUP BY a.datum
ORDER BY a.datum;
