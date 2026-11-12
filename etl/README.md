# ETL-pipeline

`load.py` laadt alle nieuwe bestanden uit `landing/` via `staging` in het sterschema `dwh`.
`validate.py` bevat de validatieregels, los van de database.

```mermaid
flowchart LR
    L["landing/*.csv"] --> V{"validate.py"}
    V -->|alle rijen| RAW["staging.*_raw<br/>(COPY, alles tekst)"]
    V -->|foute rijen| REJ["staging.rejected_row<br/>regel + reden + rij als jsonb"]
    RAW -->|"goede rijen<br/>INSERT … ON CONFLICT"| DWH["dwh.fact_production<br/>dwh.fact_downtime"]
    L -->|ok| A["archive/YYYY-MM/"]
    L -->|bestand onbruikbaar| R["rejected/"]
    LOG["staging.etl_run_log<br/>staging.load_batch<br/>logs/etl.log"]
```

## Eenmalig installeren (op de server)

```bash
cd ~/data-platform-lab && git pull
docker exec -i postgres psql -U admin -d steelline -f - < sql/004_etl_run_log.sql

. .venv/bin/activate                    # zelfde venv als de generator
pip install -r etl/requirements.txt
python -m pytest -q etl/tests generator/tests
```

De ETL verbindt als `etl_writer` met `127.0.0.1:5432/steelline` en leest het wachtwoord
(`ETL_WRITER_PASSWORD`) uit `infra/.env`. Andere instellingen kunnen via omgevingsvariabelen
of `infra/.env`:

| Variabele | Standaard |
| --- | --- |
| `DATABASE_URL` | opgebouwd uit `PGHOST`, `PGPORT`, `PGUSER`, `PGDATABASE`, `ETL_WRITER_PASSWORD` |
| `PGHOST` / `PGPORT` | `127.0.0.1` / `5432` (in een container later: `postgres`) |
| `LANDING_DIR`, `ARCHIVE_DIR`, `REJECTED_DIR`, `ETL_LOG_DIR` | `landing/`, `archive/`, `rejected/`, `logs/` in de repo |
| `ETL_KUMA_PUSH_URL` | leeg = geen push naar Uptime Kuma |

## Gebruik

```bash
python generator/generate.py --historiek 365   # 12 maanden data (± 15 s)
python etl/load.py                             # alles laden (± 40 s voor een jaar)
python etl/load.py --controle                  # enkel valideren, niets laden of verplaatsen
tail -f logs/etl.log
```

Exitcode `0` = geslaagd of deels (rijen afgekeurd), `1` = minstens één bestand mislukt,
`2` = er liep al een run (advisory lock).

## Stappen per bestand

1. Rij in `staging.load_batch` (status `bezig`), in een eigen transactie: ook bij een fout blijft die zichtbaar.
2. CSV lezen als tekst en kolommen controleren. Verkeerde kolommen of een onleesbaar bestand → `rejected/`, status `mislukt`.
3. Validatie (`validate.py`) → lijst met afgekeurde regelnummers + reden.
4. **Eén transactie:** `TRUNCATE` + `COPY` naar `staging.*_raw`, afgekeurde rijen naar `staging.rejected_row`,
   goede rijen met `INSERT … ON CONFLICT DO UPDATE` naar `dwh`, tellingen in `load_batch`.
   Klopt `geladen + afgekeurd ≠ gelezen`, dan gaat alles terug (vangnet).
5. Bestand naar `archive/YYYY-MM/`.

**Idempotent:** de unieke sleutels `(coil_id, installation_key)` en `(source, source_event_id)` zorgen dat
herladen rijen bijwerkt in plaats van dubbel toevoegt. `load_batch.rijen_nieuw` en `rijen_bijgewerkt` tonen wat er gebeurde.

**Productiedag en ploeg van een stilstand** worden in SQL afgeleid uit `start_ts` in lokale tijd en de
uren in `dim_shift`: 01:30 op 12/11 → nachtploeg van productiedag 11/11.

## Validatieregels

Een rij wordt afgekeurd op de **eerste** regel die faalt.

| Regel | Productie | Stilstand |
| --- | --- | --- |
| `01` verplicht veld leeg | alles behalve `temp_c`, `defect_type` | `event_id`, `bron`, `installatie_code`, `start_ts` |
| `02` ongeldig type | getallen (punt als decimaalteken), datum `YYYY-MM-DD`, tijdstip ISO 8601 **met tijdzone**, ja/nee | tijdstippen, `gepland` |
| `03` onbekende code | installatie, staalkwaliteit, ploeg | bron (`sensor`/`app`), installatie, reden (leeg mag → `-1`) |
| `04` datum buiten kalender | niet in `dim_date` | productiedag niet in `dim_date` |
| `05` buiten bereik | gewicht 0–60 t, duur 0–1440 min, temp -20–1400 °C, dikte > 0 | einde na begin, max. 7 dagen |
| `06` dikte buiten kwaliteit | dikte en doeldikte binnen min/max van `dim_steel_grade` | |
| `07` defect inconsistent | `is_defect` ja ⇔ `defect_type` ingevuld | |
| `08` dubbele rij | zelfde rol + installatie: eerste blijft | zelfde bron + `event_id` |

Hoofdletters en spaties maken niet uit (`" ww1 "` = `WW1`). De grenzen komen overeen met de CHECK-constraints
in `sql/001_schema.sql`: wat de validatie doorlaat, aanvaardt de database ook.

De generator logt elke bewust foute rij in `labels/vuile_rijen_*.csv`. De test
`test_generator_vuile_rijen_worden_exact_gevonden` controleert dat de ETL precies die rijen afkeurt.

## Opvolgen

```sql
-- laatste runs
SELECT * FROM staging.etl_run_log ORDER BY run_id DESC LIMIT 5;
-- per bestand
SELECT bestand, status, rijen_gelezen, rijen_nieuw, rijen_bijgewerkt, rijen_afgekeurd
FROM staging.load_batch ORDER BY load_id DESC LIMIT 10;
-- waarom afgekeurd?
SELECT regel, count(*) FROM staging.rejected_row GROUP BY regel ORDER BY 2 DESC;
SELECT b.bestand, r.regelnummer, r.reden, r.rij FROM staging.rejected_row r JOIN staging.load_batch b USING (load_id)
ORDER BY r.rejected_id DESC LIMIT 20;
```

Voor Power BI (`bi_reader`) is er `dwh.vw_etl_status`: daarmee kun je "laatst bijgewerkt" op het rapport tonen.

## Tests

```bash
python -m pytest -q etl/tests                  # validatieregels, zonder database
TEST_DATABASE_URL=postgresql+psycopg://etl_writer:...@127.0.0.1:5432/steelline_test \
    python -m pytest -q etl/tests/test_load_db.py   # integratie, NOOIT tegen productie
```
