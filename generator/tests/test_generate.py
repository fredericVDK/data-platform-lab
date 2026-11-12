"""Tests voor de datagenerator:  cd generator && python -m pytest -q"""
from datetime import date, datetime, timedelta
from pathlib import Path
import sys

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import generate as g  # noqa: E402

DAGEN = [date(2026, 3, 23) + timedelta(days=n) for n in range(28)]  # incl. overgang naar zomertijd (29/3)


@pytest.fixture(scope="module")
def data():
    g.simuleer.cache_clear()
    prod, stil = [], []
    for d in DAGEN:
        p, s, _, _ = g.genereer_dag(d, vuil=0)
        prod.append(p)
        stil.append(s)
    p, s = pd.concat(prod, ignore_index=True), pd.concat(stil, ignore_index=True)
    p["st"] = pd.to_datetime(p.start_ts, utc=True)
    p["en"] = p.st + pd.to_timedelta(p.duration_min.astype(float), unit="m")
    s["st"] = pd.to_datetime(s.start_ts, utc=True)
    s["en"] = pd.to_datetime(s.end_ts, utc=True)
    return p, s


def test_deterministisch():
    d = date(2026, 5, 4)
    g.simuleer.cache_clear()
    a = g.genereer_dag(d, vuil=0.01)
    g.simuleer.cache_clear()
    b = g.genereer_dag(d, vuil=0.01)
    for x, y in zip(a, b):
        pd.testing.assert_frame_equal(x, y)


def test_kolommen_gelijk_aan_staging(data):
    p, s = data
    assert list(p.columns[:13]) == g.PROD_KOLOMMEN
    assert list(s.columns[:8]) == g.STIL_KOLOMMEN


def test_geen_overlap_per_installatie(data):
    p, s = data
    alles = pd.concat([p[["installatie_code", "st", "en"]], s[["installatie_code", "st", "en"]]])
    for code, groep in alles.sort_values("st").groupby("installatie_code"):
        assert (groep.st.values[1:] >= groep.en.values[:-1]).all(), code


def test_stops_hebben_positieve_duur(data):
    _, s = data
    assert (s.en > s.st).all()


def test_rol_uniek_per_installatie(data):
    p, _ = data
    assert not p.duplicated(["coil_id", "installatie_code"]).any()


def test_dikte_binnen_kwaliteit(data):
    p, _ = data
    grenzen = {"S235JR": (1.5, 20), "S355J2": (2, 25), "HC340LA": (0.5, 2.5),
               "DC01": (0.35, 3), "DC04": (0.4, 2.5), "DX51D": (0.4, 3)}
    dikte = p.thickness_mm.astype(float)
    lo = p.staalkwaliteit.map(lambda k: grenzen[k][0])
    hi = p.staalkwaliteit.map(lambda k: grenzen[k][1])
    assert ((dikte >= lo) & (dikte <= hi)).all()


def test_koudroute_volgt_warmwals(data):
    """Elke rol op de koudwals kwam eerder over de warmwals en de beitslijn."""
    p, _ = data
    ww = set(p.loc[p.installatie_code == "WW1", "coil_id"])
    kw = p[(p.installatie_code == "KW1") & (pd.to_datetime(p.productie_datum) >= pd.Timestamp(DAGEN[2]))]
    assert set(kw.coil_id) <= ww


def test_zondag_enkel_warmwals(data):
    p, _ = data
    zon = p[pd.to_datetime(p.productie_datum).dt.dayofweek == 6]
    assert set(zon.installatie_code) <= {"WW1"}
    assert set(zon.ploeg) <= {"vroeg"}


def test_nachtploeg_meer_defecten():
    g.simuleer.cache_clear()
    p = pd.concat([g.genereer_dag(date(2026, 1, 5) + timedelta(days=n), vuil=0)[0] for n in range(90)])
    pct = (p.is_defect == "true").groupby(p.ploeg).mean()
    assert pct["nacht"] > pct["vroeg"] and pct["nacht"] > pct["laat"]


def test_vuile_rijen_worden_gelogd():
    g.simuleer.cache_clear()
    p, s, _, v = g.genereer_dag(date(2026, 6, 10), vuil=0.05)
    assert len(v) > 0
    assert set(v.bestand) <= {"production", "downtime"}
    assert (p.installatie_code == "XX9").sum() == (v.soort == "onbekende_installatie").sum()


@pytest.mark.parametrize("nu, verwacht", [
    ("2026-11-12T02:00", date(2026, 11, 10)),  # nachtploeg van 11/11 loopt nog
    ("2026-11-12T06:30", date(2026, 11, 11)),
])
def test_laatste_volledige_dag(nu, verwacht):
    assert g.laatste_volledige_dag(datetime.fromisoformat(nu).replace(tzinfo=g.TZ)) == verwacht


def test_cli_schrijft_en_slaat_over(tmp_path):
    args = ["--dag", "2026-02-02", "--landing", str(tmp_path / "l"), "--labels", str(tmp_path / "b"), "-q"]
    assert g.main(args) == 0
    bestand = tmp_path / "l" / "production_2026-02-02.csv"
    mtime = bestand.stat().st_mtime_ns
    g.main(args)  # tweede keer: bestaat al -> niet opnieuw geschreven
    assert bestand.stat().st_mtime_ns == mtime
    assert not list((tmp_path / "l").glob("*.tmp"))
