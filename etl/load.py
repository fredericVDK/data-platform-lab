#!/usr/bin/env python3
"""
SteelLine ETL
=============

Laadt alle nieuwe bestanden uit landing/ in de database:

    landing/production_YYYY-MM-DD.csv ─┐
    landing/downtime_YYYY-MM-DD.csv  ──┤
                                       ▼
    1. staging.load_batch          nieuwe rij, status 'bezig'          (eigen transactie)
    2. CSV lezen + valideren       validate.py (zonder database)
    3. ── één transactie per bestand ──────────────────────────────────────────────
       TRUNCATE staging.*_raw  →  COPY ruwe rijen
       afgekeurde rijen         →  staging.rejected_row
       goede rijen              →  INSERT … ON CONFLICT DO UPDATE in dwh.fact_*
       load_batch               →  'geslaagd' of 'deels' + tellingen
       ───────────────────────────────────────────────────────────────────────────
    4. bestand → archive/YYYY-MM/   (bij een fout: load_batch 'mislukt', bestand → rejected/)

Idempotent: dezelfde dag opnieuw laden werkt bestaande rijen bij (zelfde coil_id +
installatie, of zelfde bron + event_id) en maakt nooit dubbels.

Per run: 1 rij in staging.etl_run_log, log in logs/etl.log, optioneel een push naar
Uptime Kuma (ETL_KUMA_PUSH_URL). Een advisory lock voorkomt dat twee runs tegelijk lopen.

Gebruik
-------
    python etl/load.py                 # alles in landing/
    python etl/load.py --controle      # enkel valideren, niets in de database
    python etl/load.py --bestand landing/production_2026-11-11.csv
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import re
import shutil
import socket
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Connection, Engine

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate import (PROD_KOLOMMEN, STIL_KOLOMMEN, Referentie,  # noqa: E402
                      valideer_productie, valideer_stilstand)

REPO = Path(__file__).resolve().parent.parent
BESTAND_RE = re.compile(r"^(production|downtime)_(\d{4}-\d{2}-\d{2})\.csv$")
LOCK_ID = 7_445_201  # willekeurig, maar vast: pg_advisory_lock voor de ETL

log = logging.getLogger("etl")


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------
UPSERT_PRODUCTIE = """
INSERT INTO dwh.fact_production AS f
      (coil_id, date_key, shift_key, installation_key, steel_grade_key, start_ts,
       weight_t, thickness_mm, target_thickness_mm, temp_c, duration_min,
       is_defect, defect_type, load_id)
SELECT trim(r.coil_id),
       to_char(trim(r.productie_datum)::date, 'YYYYMMDD')::integer,
       s.shift_key, i.installation_key, g.grade_key,
       trim(r.start_ts)::timestamptz,
       trim(r.weight_t)::numeric, trim(r.thickness_mm)::numeric, trim(r.target_thickness_mm)::numeric,
       nullif(trim(r.temp_c), '')::numeric, trim(r.duration_min)::numeric,
       lower(trim(r.is_defect)) IN ('true', 't', '1', 'j', 'ja', 'y', 'yes'),
       nullif(trim(r.defect_type), ''),
       r.load_id
FROM staging.production_raw r
JOIN dwh.dim_shift           s ON s.ploeg = lower(trim(r.ploeg))
JOIN dwh.dim_installation    i ON i.installation_code = upper(trim(r.installatie_code))
JOIN dwh.dim_steel_grade     g ON g.grade_code = upper(trim(r.staalkwaliteit))
WHERE r.load_id = :load_id
  AND NOT EXISTS (SELECT 1 FROM staging.rejected_row x
                  WHERE x.load_id = r.load_id AND x.bron_tabel = 'production_raw'
                    AND x.regelnummer = r.regelnummer)
ON CONFLICT (coil_id, installation_key) DO UPDATE SET
       date_key = EXCLUDED.date_key, shift_key = EXCLUDED.shift_key,
       steel_grade_key = EXCLUDED.steel_grade_key, start_ts = EXCLUDED.start_ts,
       weight_t = EXCLUDED.weight_t, thickness_mm = EXCLUDED.thickness_mm,
       target_thickness_mm = EXCLUDED.target_thickness_mm, temp_c = EXCLUDED.temp_c,
       duration_min = EXCLUDED.duration_min, is_defect = EXCLUDED.is_defect,
       defect_type = EXCLUDED.defect_type, load_id = EXCLUDED.load_id, loaded_at = now()
RETURNING (xmax = 0) AS nieuw
"""

# Productiedag en ploeg worden afgeleid uit start_ts (lokale tijd), met de
# start-/eindtijden uit dim_shift. Nacht: eind_tijd < start_tijd.
UPSERT_STILSTAND = """
WITH r AS (
    SELECT r.*, trim(r.start_ts)::timestamptz AS ts,
           (trim(r.start_ts)::timestamptz AT TIME ZONE 'Europe/Brussels') AS lokaal
    FROM staging.downtime_raw r
    WHERE r.load_id = :load_id
      AND NOT EXISTS (SELECT 1 FROM staging.rejected_row x
                      WHERE x.load_id = r.load_id AND x.bron_tabel = 'downtime_raw'
                        AND x.regelnummer = r.regelnummer)
)
INSERT INTO dwh.fact_downtime AS f
      (source, source_event_id, date_key, shift_key, installation_key, reason_key,
       start_ts, end_ts, planned, opmerking, load_id)
SELECT lower(trim(r.bron)), trim(r.event_id),
       to_char((r.lokaal - interval '6 hours')::date, 'YYYYMMDD')::integer,
       s.shift_key, i.installation_key, coalesce(d.reason_key, -1),
       r.ts, nullif(trim(r.end_ts), '')::timestamptz,
       CASE WHEN lower(trim(r.gepland)) IN ('true', 't', '1', 'j', 'ja', 'y', 'yes') THEN true
            WHEN lower(trim(r.gepland)) IN ('false', 'f', '0', 'n', 'nee', 'no')  THEN false
            ELSE coalesce(d.standaard_gepland, false) END,
       nullif(trim(r.opmerking), ''),
       r.load_id
FROM r
JOIN dwh.dim_installation i ON i.installation_code = upper(trim(r.installatie_code))
JOIN dwh.dim_shift s ON CASE WHEN s.eind_tijd > s.start_tijd
                             THEN r.lokaal::time >= s.start_tijd AND r.lokaal::time < s.eind_tijd
                             ELSE r.lokaal::time >= s.start_tijd OR  r.lokaal::time < s.eind_tijd END
LEFT JOIN dwh.dim_downtime_reason d ON d.reason_code = upper(trim(r.reden_code))
ON CONFLICT (source, source_event_id) DO UPDATE SET
       date_key = EXCLUDED.date_key, shift_key = EXCLUDED.shift_key,
       installation_key = EXCLUDED.installation_key, reason_key = EXCLUDED.reason_key,
       start_ts = EXCLUDED.start_ts, end_ts = EXCLUDED.end_ts, planned = EXCLUDED.planned,
       opmerking = EXCLUDED.opmerking, load_id = EXCLUDED.load_id, loaded_at = now()
RETURNING (xmax = 0) AS nieuw
"""


@dataclass(frozen=True)
class Soort:
    raw: str
    kolommen: list[str]
    valideer: object
    upsert: str


SOORTEN = {
    "production": Soort("production_raw", PROD_KOLOMMEN, valideer_productie, UPSERT_PRODUCTIE),
    "downtime": Soort("downtime_raw", STIL_KOLOMMEN, valideer_stilstand, UPSERT_STILSTAND),
}


@dataclass
class Totaal:
    bestanden: int = 0
    mislukt: int = 0
    gelezen: int = 0
    geladen: int = 0
    afgekeurd: int = 0
    fouten: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.bestanden == 0:
            return "niets te doen"
        if self.mislukt:
            return "mislukt"
        return "deels" if self.afgekeurd else "geslaagd"


class BestandFout(Exception):
    """Het bestand zelf is onbruikbaar (kolommen, codering, ...)."""


# ---------------------------------------------------------------------------
# Configuratie
# ---------------------------------------------------------------------------
def lees_env_bestand(pad: Path) -> dict[str, str]:
    """Minimale .env-lezer (KEY=waarde, # commentaar)."""
    uit: dict[str, str] = {}
    if pad.exists():
        for regel in pad.read_text(encoding="utf-8").splitlines():
            regel = regel.strip()
            if regel and not regel.startswith("#") and "=" in regel:
                k, v = regel.split("=", 1)
                uit[k.strip()] = v.strip().strip('"').strip("'")
    return uit


def instelling(naam: str, standaard: str | None = None) -> str | None:
    return os.environ.get(naam) or lees_env_bestand(REPO / "infra" / ".env").get(naam) or standaard


def maak_engine() -> Engine:
    url = instelling("DATABASE_URL")
    if not url:
        url = URL.create(
            "postgresql+psycopg",
            username=instelling("PGUSER", "etl_writer"),
            password=instelling("ETL_WRITER_PASSWORD"),
            host=instelling("PGHOST", "127.0.0.1"),
            port=int(instelling("PGPORT", "5432")),
            database=instelling("PGDATABASE", "steelline"),
        )
    return create_engine(url, pool_pre_ping=True, connect_args={"application_name": "steelline-etl"})


def stel_logging_in(logmap: Path, stil: bool) -> None:
    logmap.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    bestand = logging.handlers.RotatingFileHandler(logmap / "etl.log", maxBytes=5_000_000, backupCount=5,
                                                   encoding="utf-8")
    bestand.setFormatter(fmt)
    scherm = logging.StreamHandler()
    scherm.setFormatter(fmt)
    scherm.setLevel(logging.WARNING if stil else logging.INFO)
    log.handlers[:] = [bestand, scherm]
    log.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Database-stappen
# ---------------------------------------------------------------------------
def lees_referentie(conn: Connection) -> Referentie:
    q = lambda sql: conn.execute(text(sql)).all()  # noqa: E731
    eerste, laatste = q("SELECT min(datum), max(datum) FROM dwh.dim_date")[0]
    if eerste is None:
        raise RuntimeError("dwh.dim_date is leeg: voer eerst sql/003_seed_dimensions.sql uit")
    return Referentie(
        installaties=frozenset(r[0] for r in q("SELECT installation_code FROM dwh.dim_installation")),
        ploegen=frozenset(r[0] for r in q("SELECT ploeg FROM dwh.dim_shift")),
        redenen=frozenset(r[0] for r in q("SELECT reason_code FROM dwh.dim_downtime_reason")),
        kwaliteiten={r[0]: (float(r[1]), float(r[2]))
                     for r in q("SELECT grade_code, min_dikte_mm, max_dikte_mm FROM dwh.dim_steel_grade")},
        eerste_dag=eerste, laatste_dag=laatste,
    )


def kopieer_ruw(conn: Connection, soort: Soort, load_id: int, df: pd.DataFrame) -> None:
    """COPY is veel sneller dan INSERT: ± 1 s voor een dag."""
    kolommen = ", ".join(soort.kolommen)
    cur = conn.connection.driver_connection.cursor()
    with cur.copy(f"COPY staging.{soort.raw} (load_id, regelnummer, {kolommen}) FROM STDIN") as cp:
        for nr, rij in enumerate(df.itertuples(index=False, name=None), start=1):
            cp.write_row((load_id, nr, *rij))


def bewaar_afgekeurd(conn: Connection, soort: Soort, load_id: int, df: pd.DataFrame, af: pd.DataFrame) -> None:
    if af.empty:
        return
    rijen = df.reset_index(drop=True)
    conn.execute(
        text("INSERT INTO staging.rejected_row (load_id, bron_tabel, regelnummer, regel, reden, rij) "
             "VALUES (:load_id, :tabel, :nr, :regel, :reden, CAST(:rij AS jsonb))"),
        [dict(load_id=load_id, tabel=soort.raw, nr=int(a.regelnummer), regel=a.regel, reden=a.reden,
              rij=json.dumps(rijen.iloc[a.regelnummer - 1].to_dict(), ensure_ascii=False))
         for a in af.itertuples(index=False)],
    )


def lees_csv(pad: Path, kolommen: list[str]) -> pd.DataFrame:
    try:
        df = pd.read_csv(pad, dtype=str, keep_default_na=False, encoding="utf-8")
    except Exception as e:  # leeg, binair, kapotte quotes, ...
        raise BestandFout(f"kan het bestand niet lezen: {e}") from e
    if list(df.columns) != kolommen:
        raise BestandFout(f"verkeerde kolommen {list(df.columns)}, verwacht {kolommen}")
    return df


def verplaats(pad: Path, doelmap: Path) -> Path:
    doelmap.mkdir(parents=True, exist_ok=True)
    doel = doelmap / pad.name
    if doelmap.name == "rejected" and doel.exists():  # vorige mislukte versie niet overschrijven
        doel = doelmap / f"{pad.stem}.{time.strftime('%Y%m%dT%H%M%S')}{pad.suffix}"
    shutil.move(str(pad), doel)
    return doel


def verwerk_bestand(engine: Engine, pad: Path, run_id: int, ref: Referentie,
                    archief: Path, afgewezen: Path, totaal: Totaal) -> None:
    m = BESTAND_RE.match(pad.name)
    soort, dag = SOORTEN[m.group(1)], date.fromisoformat(m.group(2))
    totaal.bestanden += 1
    t0 = time.perf_counter()

    with engine.begin() as conn:
        load_id = conn.execute(
            text("INSERT INTO staging.load_batch (run_id, bron, bestand, productiedag) "
                 "VALUES (:run, 'generator', :bestand, :dag) RETURNING load_id"),
            dict(run=run_id, bestand=pad.name, dag=dag)).scalar_one()

    try:
        df = lees_csv(pad, soort.kolommen)
        af = soort.valideer(df, ref)
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE staging.{soort.raw}"))
            kopieer_ruw(conn, soort, load_id, df)
            bewaar_afgekeurd(conn, soort, load_id, df, af)
            resultaat = conn.execute(text(soort.upsert), dict(load_id=load_id)).all()
            nieuw = sum(1 for r in resultaat if r.nieuw)
            if len(resultaat) + len(af) != len(df):  # vangnet: rij verdwenen bij een join
                raise RuntimeError(f"{len(df)} gelezen, maar {len(resultaat)} geladen + {len(af)} afgekeurd")
            status = "deels" if len(af) else "geslaagd"
            conn.execute(
                text("UPDATE staging.load_batch SET beeindigd_op = now(), status = :status, "
                     "rijen_gelezen = :gelezen, rijen_geladen = :geladen, rijen_afgekeurd = :afgekeurd, "
                     "rijen_nieuw = :nieuw, rijen_bijgewerkt = :bijgewerkt WHERE load_id = :load_id"),
                dict(status=status, gelezen=len(df), geladen=len(resultaat), afgekeurd=len(af),
                     nieuw=nieuw, bijgewerkt=len(resultaat) - nieuw, load_id=load_id))
    except Exception as e:
        totaal.mislukt += 1
        totaal.fouten.append(f"{pad.name}: {e}")
        log.error("%s MISLUKT (load_id %s): %s", pad.name, load_id, e, exc_info=not isinstance(e, BestandFout))
        with engine.begin() as conn:
            conn.execute(text("UPDATE staging.load_batch SET beeindigd_op = now(), status = 'mislukt', "
                              "foutmelding = :fout WHERE load_id = :load_id"),
                         dict(fout=str(e)[:4000], load_id=load_id))
        doel = verplaats(pad, afgewezen)
        log.error("%s verplaatst naar %s", pad.name, doel)
        return

    totaal.gelezen += len(df)
    totaal.geladen += len(resultaat)
    totaal.afgekeurd += len(af)
    verplaats(pad, archief / f"{dag:%Y-%m}")
    log.info("%-30s %6d gelezen  %6d nieuw  %6d bijgewerkt  %4d afgekeurd  (%.1f s, load_id %d)",
             pad.name, len(df), nieuw, len(resultaat) - nieuw, len(af), time.perf_counter() - t0, load_id)
    if len(af):
        for regel, n in af.regel.value_counts().items():
            log.info("    afgekeurd %-28s %d", regel, n)


def push_kuma(totaal: Totaal, duur: float) -> None:
    url = instelling("ETL_KUMA_PUSH_URL")
    if not url:
        return
    # ook "niets te doen" is up: alles is al geladen. Crasht de generator, dan
    # stopt nightly.sh vóór de ETL en komt er geen push -> Kuma meldt na 25 u.
    up = totaal.status != "mislukt"
    msg = (f"{totaal.status}: {totaal.bestanden} bestanden, {totaal.geladen} geladen, "
           f"{totaal.afgekeurd} afgekeurd" + (f" | {totaal.fouten[0]}" if totaal.fouten else ""))
    query = urllib.parse.urlencode({"status": "up" if up else "down", "msg": msg[:250], "ping": round(duur * 1000)})
    try:
        urllib.request.urlopen(f"{url}?{query}", timeout=10).read()
    except Exception as e:  # monitoring mag de ETL nooit laten falen
        log.warning("push naar Uptime Kuma mislukt: %s", e)


# ---------------------------------------------------------------------------
# Hoofdprogramma
# ---------------------------------------------------------------------------
def zoek_bestanden(landing: Path) -> list[Path]:
    """Gesorteerd op dag, dan stilstand vóór productie."""
    gevonden = [p for p in landing.glob("*.csv") if BESTAND_RE.match(p.name)]
    return sorted(gevonden, key=lambda p: (BESTAND_RE.match(p.name).group(2), p.name))


def controle(bestanden: list[Path]) -> int:
    """Valideren zonder database, met referentiedata uit de seed."""
    sys.path.insert(0, str(REPO / "generator"))
    import generate as g
    ref = Referentie(frozenset(g.INSTALLATIES), frozenset(g.PLOEGEN), frozenset(
        ["ONBEKEND", "MECH-LAGER", "MECH-HYDR", "MECH-WALS", "ELEK-MOTOR", "ELEK-PLC", "ELEK-NET",
         "WISS-WALS", "WISS-PROD", "WACHT-MAT", "WACHT-KRAAN", "ONDH-GEPLAND"]),
        {"S235JR": (1.5, 20), "S355J2": (2, 25), "HC340LA": (0.5, 2.5), "DC01": (0.35, 3),
         "DC04": (0.4, 2.5), "DX51D": (0.4, 3)}, date(2024, 1, 1), date(2030, 12, 31))
    for pad in bestanden:
        soort = SOORTEN[BESTAND_RE.match(pad.name).group(1)]
        df = lees_csv(pad, soort.kolommen)
        af = soort.valideer(df, ref)
        print(f"{pad.name}: {len(df)} rijen, {len(af)} afgekeurd")
        for a in af.itertuples(index=False):
            print(f"   regel {a.regelnummer:>5}  {a.regel:<28} {a.reden}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SteelLine ETL: landing/ -> staging -> dwh")
    ap.add_argument("--landing", type=Path, default=Path(instelling("LANDING_DIR", str(REPO / "landing"))))
    ap.add_argument("--archief", type=Path, default=Path(instelling("ARCHIVE_DIR", str(REPO / "archive"))))
    ap.add_argument("--afgewezen", type=Path, default=Path(instelling("REJECTED_DIR", str(REPO / "rejected"))))
    ap.add_argument("--logs", type=Path, default=Path(instelling("ETL_LOG_DIR", str(REPO / "logs"))))
    ap.add_argument("--bestand", type=Path, action="append", help="enkel dit bestand (mag meermaals)")
    ap.add_argument("--controle", action="store_true", help="enkel valideren, niets laden of verplaatsen")
    ap.add_argument("-q", "--stil", action="store_true", help="enkel waarschuwingen op het scherm")
    args = ap.parse_args(argv)

    bestanden = args.bestand or zoek_bestanden(args.landing)
    for p in bestanden:
        if not BESTAND_RE.match(p.name):
            ap.error(f"{p.name}: naam moet production_YYYY-MM-DD.csv of downtime_YYYY-MM-DD.csv zijn")
    if args.controle:
        return controle(bestanden)

    stel_logging_in(args.logs, args.stil)
    t0 = time.perf_counter()
    totaal = Totaal()
    engine = maak_engine()

    with engine.connect() as slot:
        if not slot.execute(text("SELECT pg_try_advisory_lock(:id)"), dict(id=LOCK_ID)).scalar_one():
            log.warning("er loopt al een ETL-run; gestopt")
            return 2
        slot.commit()
        try:
            with engine.begin() as conn:
                run_id = conn.execute(text("INSERT INTO staging.etl_run_log (host) VALUES (:h) RETURNING run_id"),
                                      dict(h=socket.gethostname())).scalar_one()
            log.info("run %d gestart: %d bestand(en) in %s", run_id, len(bestanden), args.landing)
            try:
                with engine.connect() as conn:
                    ref = lees_referentie(conn)
                for pad in bestanden:
                    verwerk_bestand(engine, pad, run_id, ref, args.archief, args.afgewezen, totaal)
            except Exception as e:  # onverwacht (database weg, ...): run als mislukt markeren
                totaal.mislukt += 1
                totaal.fouten.append(str(e))
                log.exception("run %d afgebroken", run_id)
            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE staging.etl_run_log SET beeindigd_op = now(), status = :status, "
                         "bestanden = :b, bestanden_mislukt = :m, rijen_gelezen = :g, rijen_geladen = :l, "
                         "rijen_afgekeurd = :a, foutmelding = :f WHERE run_id = :run_id"),
                    dict(status=totaal.status, b=totaal.bestanden, m=totaal.mislukt, g=totaal.gelezen,
                         l=totaal.geladen, a=totaal.afgekeurd, f="\n".join(totaal.fouten)[:4000] or None,
                         run_id=run_id))
        finally:
            slot.execute(text("SELECT pg_advisory_unlock(:id)"), dict(id=LOCK_ID))
            slot.commit()

    duur = time.perf_counter() - t0
    log.info("run %d %s: %d bestanden, %d geladen, %d afgekeurd, %d mislukt (%.1f s)",
             run_id, totaal.status, totaal.bestanden, totaal.geladen, totaal.afgekeurd, totaal.mislukt, duur)
    push_kuma(totaal, duur)
    return 1 if totaal.status == "mislukt" else 0


if __name__ == "__main__":
    sys.exit(main())
