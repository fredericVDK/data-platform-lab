"""
Integratietest tegen een ECHTE database (schema's 001-004 moeten bestaan).

Draait enkel als TEST_DATABASE_URL gezet is. Gebruik NOOIT de productiedatabase:
de test laadt data in dwh.fact_*.

    TEST_DATABASE_URL=postgresql+psycopg://etl_writer:...@127.0.0.1:5432/steelline_test \
        python -m pytest -q etl/tests/test_load_db.py
"""
from datetime import date
from pathlib import Path
import os
import sys

import pandas as pd
import pytest

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL niet gezet")

HIER = Path(__file__).resolve().parent
sys.path.insert(0, str(HIER.parent))
sys.path.insert(0, str(HIER.parents[1] / "generator"))


@pytest.fixture()
def omgeving(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.delenv("ETL_KUMA_PUSH_URL", raising=False)
    import generate as g
    import load
    g.simuleer.cache_clear()
    mappen = {k: tmp_path / k for k in ("landing", "labels", "archive", "rejected", "logs")}
    gen = lambda *extra: g.main(["--van", "2025-03-03", "--tot", "2025-03-05", "--landing", str(mappen["landing"]),
                                 "--labels", str(mappen["labels"]), "--vuil", "0.02", "-q", *extra])
    etl = lambda: load.main(["--landing", str(mappen["landing"]), "--archief", str(mappen["archive"]),
                             "--afgewezen", str(mappen["rejected"]), "--logs", str(mappen["logs"]), "-q"])
    return gen, etl, mappen, load.maak_engine()


def tel(engine):
    return pd.read_sql("""
        SELECT (SELECT count(*) FROM dwh.fact_production WHERE date_key BETWEEN 20250303 AND 20250305) AS prod,
               (SELECT count(*) FROM dwh.fact_downtime  WHERE date_key BETWEEN 20250303 AND 20250305) AS stil""",
                       engine).iloc[0].tolist()


def test_laden_is_idempotent_en_keurt_juist_af(omgeving):
    gen, etl, mappen, engine = omgeving
    gen()
    assert etl() == 0
    eerste = tel(engine)
    assert eerste[0] > 0 and eerste[1] > 0
    assert not list(mappen["landing"].glob("*.csv"))           # alles verplaatst
    assert len(list(mappen["archive"].rglob("*.csv"))) == 6

    gen("--overschrijven")                                       # zelfde dagen opnieuw
    assert etl() == 0
    assert tel(engine) == eerste                                 # geen dubbels

    run = pd.read_sql("SELECT * FROM staging.etl_run_log ORDER BY run_id DESC LIMIT 1", engine).iloc[0]
    vuil = pd.concat(pd.read_csv(f) for f in mappen["labels"].glob("vuile_rijen_*.csv"))
    assert run.status in ("geslaagd", "deels")
    assert run.rijen_afgekeurd == len(vuil)


def test_kapot_bestand_naar_rejected(omgeving):
    _, etl, mappen, engine = omgeving
    mappen["landing"].mkdir(parents=True)
    (mappen["landing"] / "production_2025-03-06.csv").write_text("coil_id,foo\n1,2\n")
    assert etl() == 1
    assert (mappen["rejected"] / "production_2025-03-06.csv").exists()
    status = pd.read_sql("SELECT status FROM staging.etl_run_log ORDER BY run_id DESC LIMIT 1", engine).status[0]
    assert status == "mislukt"
