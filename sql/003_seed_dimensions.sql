-- =====================================================================
-- 003_seed_dimensions.sql  -  Vaste inhoud van de dimensies
-- =====================================================================
-- Herhaalbaar: ON CONFLICT op de natuurlijke sleutel. Pas je hier een
-- ploegleider of capaciteit aan, dan overschrijft een nieuwe run de
-- oude waarde (SCD type 1).
-- =====================================================================

\set ON_ERROR_STOP on
SET client_min_messages = warning;   -- geen "already exists"-meldingen bij herhalen

BEGIN;

SET ROLE etl_writer;
SET search_path = dwh;

-- ---------------------------------------------------------------------
-- dim_date: 2024-01-01 t/m 2030-12-31, met Belgische wettelijke feestdagen
-- ---------------------------------------------------------------------
WITH jaren AS (
    SELECT generate_series(2024, 2030) AS j
),
-- Paasdatum (anoniem Gregoriaans algoritme / Meeus)
pasen AS (
    SELECT j,
           make_date(j, ((h + l - 7*m + 114) / 31), ((h + l - 7*m + 114) % 31) + 1) AS paasdag
    FROM (
        SELECT j, h, l, (a + 11*h + 22*l) / 451 AS m
        FROM (
            SELECT j, a, h, (32 + 2*e + 2*i - h - k) % 7 AS l
            FROM (
                SELECT j, a, e, i, k, (19*a + b - d - g + 15) % 30 AS h
                FROM (
                    SELECT j, j % 19 AS a, j / 100 AS b, j % 100 AS c,
                           (j / 100) / 4 AS d, (j / 100) % 4 AS e,
                           ((j / 100) - ((j / 100) + 8) / 25 + 1) / 3 AS g,
                           (j % 100) / 4 AS i, (j % 100) % 4 AS k
                    FROM jaren
                ) s1
            ) s2
        ) s3
    ) s4
),
feestdagen AS (
    SELECT make_date(j, 1, 1)   AS datum, 'Nieuwjaar'                AS naam FROM jaren
    UNION ALL SELECT paasdag + 1,  'Paasmaandag'                     FROM pasen
    UNION ALL SELECT make_date(j, 5, 1),   'Feest van de Arbeid'     FROM jaren
    UNION ALL SELECT paasdag + 39, 'O.L.H. Hemelvaart'               FROM pasen
    UNION ALL SELECT paasdag + 50, 'Pinkstermaandag'                 FROM pasen
    UNION ALL SELECT make_date(j, 7, 21),  'Nationale feestdag'      FROM jaren
    UNION ALL SELECT make_date(j, 8, 15),  'O.L.V. Hemelvaart'       FROM jaren
    UNION ALL SELECT make_date(j, 11, 1),  'Allerheiligen'           FROM jaren
    UNION ALL SELECT make_date(j, 11, 11), 'Wapenstilstand'          FROM jaren
    UNION ALL SELECT make_date(j, 12, 25), 'Kerstmis'                FROM jaren
),
dagen AS (
    SELECT d::date AS datum
    FROM generate_series(date '2024-01-01', date '2030-12-31', interval '1 day') AS d
)
INSERT INTO dim_date (date_key, datum, jaar, kwartaal, maand, maand_naam, iso_jaar, iso_week,
                      dag_van_week, dag_naam, is_weekend, is_feestdag, feestdag_naam, werkdag)
SELECT to_char(dg.datum, 'YYYYMMDD')::integer,
       dg.datum,
       extract(year    FROM dg.datum)::smallint,
       extract(quarter FROM dg.datum)::smallint,
       extract(month   FROM dg.datum)::smallint,
       (ARRAY['januari','februari','maart','april','mei','juni','juli',
              'augustus','september','oktober','november','december'])[extract(month FROM dg.datum)],
       extract(isoyear FROM dg.datum)::smallint,
       extract(week    FROM dg.datum)::smallint,
       extract(isodow  FROM dg.datum)::smallint,
       (ARRAY['maandag','dinsdag','woensdag','donderdag','vrijdag','zaterdag','zondag'])[extract(isodow FROM dg.datum)],
       extract(isodow FROM dg.datum) >= 6,
       f.naam IS NOT NULL,
       f.naam,
       extract(isodow FROM dg.datum) < 6 AND f.naam IS NULL
FROM dagen dg
LEFT JOIN feestdagen f ON f.datum = dg.datum
ON CONFLICT (date_key) DO UPDATE
   SET is_feestdag = EXCLUDED.is_feestdag,
       feestdag_naam = EXCLUDED.feestdag_naam,
       werkdag = EXCLUDED.werkdag;

-- ---------------------------------------------------------------------
-- dim_shift (fictieve ploegleiders)
-- ---------------------------------------------------------------------
INSERT INTO dim_shift (shift_code, ploeg, start_tijd, eind_tijd, ploegleider) VALUES
    ('VROEG', 'vroeg', '06:00', '14:00', 'Els Peeters'),
    ('LAAT',  'laat',  '14:00', '22:00', 'Koen Janssens'),
    ('NACHT', 'nacht', '22:00', '06:00', 'Nadia El Amrani')
ON CONFLICT (shift_code) DO UPDATE
   SET ploeg = EXCLUDED.ploeg, start_tijd = EXCLUDED.start_tijd,
       eind_tijd = EXCLUDED.eind_tijd, ploegleider = EXCLUDED.ploegleider;

-- ---------------------------------------------------------------------
-- dim_installation: twee lijnen, processtappen in volgorde
-- (capaciteiten zijn gesimuleerd, grootteorde realistisch)
-- ---------------------------------------------------------------------
INSERT INTO dim_installation (installation_code, naam, soort, lijn, volgorde_in_lijn, capaciteit_t_per_uur) VALUES
    ('WW1', 'Warmwals 1',          'warmwals',  'Warmband', 1, 600),
    ('BL1', 'Beitslijn 1',         'beitslijn', 'Koudband', 1, 250),
    ('KW1', 'Koudwals 1',          'koudwals',  'Koudband', 2, 200),
    ('GO1', 'Continue gloeioven 1','gloeioven', 'Koudband', 3, 90)
ON CONFLICT (installation_code) DO UPDATE
   SET naam = EXCLUDED.naam, soort = EXCLUDED.soort, lijn = EXCLUDED.lijn,
       volgorde_in_lijn = EXCLUDED.volgorde_in_lijn,
       capaciteit_t_per_uur = EXCLUDED.capaciteit_t_per_uur;

-- ---------------------------------------------------------------------
-- dim_steel_grade (bestaande EN-normnamen, diktebereik vereenvoudigd)
-- ---------------------------------------------------------------------
INSERT INTO dim_steel_grade (grade_code, kwaliteit, familie, min_dikte_mm, max_dikte_mm) VALUES
    ('S235JR',  'Constructiestaal S235JR',            'constructiestaal', 1.500, 20.000),
    ('S355J2',  'Constructiestaal S355J2',            'constructiestaal', 2.000, 25.000),
    ('HC340LA', 'Microgelegeerd hoogsterkte HC340LA', 'hoogsterkte',      0.500,  2.500),
    ('DC01',    'Koudgewalst zacht staal DC01',       'dieptrekstaal',    0.350,  3.000),
    ('DC04',    'Dieptrekstaal DC04',                 'dieptrekstaal',    0.400,  2.500),
    ('DX51D',   'Staal voor verzinken DX51D',         'bekledingsstaal',  0.400,  3.000)
ON CONFLICT (grade_code) DO UPDATE
   SET kwaliteit = EXCLUDED.kwaliteit, familie = EXCLUDED.familie,
       min_dikte_mm = EXCLUDED.min_dikte_mm, max_dikte_mm = EXCLUDED.max_dikte_mm;

-- ---------------------------------------------------------------------
-- dim_downtime_reason (incl. vaste 'onbekend'-rij met key -1)
-- ---------------------------------------------------------------------
INSERT INTO dim_downtime_reason (reason_key, reason_code, categorie, omschrijving, standaard_gepland)
OVERRIDING SYSTEM VALUE
VALUES (-1, 'ONBEKEND', 'onbekend', 'Nog geen oorzaak opgegeven', false)
ON CONFLICT (reason_key) DO NOTHING;

INSERT INTO dim_downtime_reason (reason_code, categorie, omschrijving, standaard_gepland) VALUES
    ('MECH-LAGER',   'mechanisch',           'Lagerdefect',                         false),
    ('MECH-HYDR',    'mechanisch',           'Hydraulisch lek',                     false),
    ('MECH-WALS',    'mechanisch',           'Walsbreuk of walsschade',             false),
    ('ELEK-MOTOR',   'elektrisch',           'Storing aandrijfmotor',               false),
    ('ELEK-PLC',     'elektrisch',           'PLC- of sensorstoring',               false),
    ('ELEK-NET',     'elektrisch',           'Spanningsdip / netstoring',           false),
    ('WISS-WALS',    'wissel',               'Walswissel',                          true),
    ('WISS-PROD',    'wissel',               'Productwissel (andere kwaliteit/dikte)', true),
    ('WACHT-MAT',    'wachten op materiaal', 'Geen rollen van vorige installatie',  false),
    ('WACHT-KRAAN',  'wachten op materiaal', 'Wachten op kraan / transport',        false),
    ('ONDH-GEPLAND', 'gepland onderhoud',    'Gepland preventief onderhoud',        true)
ON CONFLICT (reason_code) DO UPDATE
   SET categorie = EXCLUDED.categorie, omschrijving = EXCLUDED.omschrijving,
       standaard_gepland = EXCLUDED.standaard_gepland;

RESET ROLE;
COMMIT;
