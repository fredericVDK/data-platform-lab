"""Tests voor de validatieregels (geen database nodig):  python -m pytest -q etl/tests"""
from datetime import date
from pathlib import Path
import sys

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from validate import (PROD_KOLOMMEN, STIL_KOLOMMEN, Referentie,  # noqa: E402
                      valideer_productie, valideer_stilstand)

REF = Referentie(
    installaties=frozenset({"WW1", "BL1", "KW1", "GO1"}),
    ploegen=frozenset({"vroeg", "laat", "nacht"}),
    redenen=frozenset({"ONBEKEND", "MECH-LAGER", "WISS-WALS"}),
    kwaliteiten={"S235JR": (1.5, 20.0), "DC04": (0.4, 2.5)},
    eerste_dag=date(2024, 1, 1), laatste_dag=date(2030, 12, 31),
)

GOEDE_ROL = dict(coil_id="C202611100001", productie_datum="2026-11-10", ploeg="vroeg", installatie_code="WW1",
                 staalkwaliteit="S235JR", start_ts="2026-11-10T06:00:00+01:00", weight_t="23.100",
                 thickness_mm="4.010", target_thickness_mm="4.000", temp_c="880.0", duration_min="2.50",
                 is_defect="false", defect_type="")

GOEDE_STOP = dict(event_id="WW1-20261110-001", bron="sensor", installatie_code="WW1", reden_code="WISS-WALS",
                  start_ts="2026-11-10T10:00:00+01:00", end_ts="2026-11-10T10:25:00+01:00", gepland="j",
                  opmerking="")


def prod(*wijzigingen: dict) -> pd.DataFrame:
    return pd.DataFrame([{**GOEDE_ROL, **w} for w in wijzigingen], columns=PROD_KOLOMMEN)


def stil(*wijzigingen: dict) -> pd.DataFrame:
    return pd.DataFrame([{**GOEDE_STOP, **w} for w in wijzigingen], columns=STIL_KOLOMMEN)


def eerste_regel(af: pd.DataFrame) -> str | None:
    return af.regel.iloc[0] if len(af) else None


# --- productie ---------------------------------------------------------------
def test_goede_rij_wordt_aanvaard():
    assert valideer_productie(prod({}), REF).empty


@pytest.mark.parametrize("wijziging, regel", [
    ({"coil_id": ""}, "P01_verplicht_veld_leeg"),
    ({"coil_id": "   "}, "P01_verplicht_veld_leeg"),
    ({"weight_t": ""}, "P01_verplicht_veld_leeg"),
    ({"weight_t": "n/a"}, "P02_ongeldig_type"),
    ({"weight_t": "23,1"}, "P02_ongeldig_type"),            # komma i.p.v. punt
    ({"weight_t": "inf"}, "P02_ongeldig_type"),
    ({"temp_c": "heet"}, "P02_ongeldig_type"),
    ({"productie_datum": "10/11/2026"}, "P02_ongeldig_type"),
    ({"start_ts": "2026-11-10T06:00:00"}, "P02_ongeldig_type"),  # geen tijdzone
    ({"is_defect": "misschien"}, "P02_ongeldig_type"),
    ({"installatie_code": "XX9"}, "P03_onbekende_code"),
    ({"staalkwaliteit": "S999"}, "P03_onbekende_code"),
    ({"ploeg": "middag"}, "P03_onbekende_code"),
    ({"productie_datum": "2031-01-01", "start_ts": "2031-01-01T06:00:00+01:00"}, "P04_datum_buiten_kalender"),
    ({"weight_t": "-23.1"}, "P05_buiten_bereik"),
    ({"weight_t": "0"}, "P05_buiten_bereik"),
    ({"duration_min": "2000"}, "P05_buiten_bereik"),
    ({"temp_c": "1500"}, "P05_buiten_bereik"),
    ({"thickness_mm": "99.900"}, "P06_dikte_buiten_kwaliteit"),
    ({"staalkwaliteit": "DC04"}, "P06_dikte_buiten_kwaliteit"),   # 4 mm, max 2,5 voor DC04
    ({"is_defect": "true"}, "P07_defect_inconsistent"),
    ({"defect_type": "randscheur"}, "P07_defect_inconsistent"),
])
def test_foute_rol(wijziging, regel):
    assert eerste_regel(valideer_productie(prod(wijziging), REF)) == regel


@pytest.mark.parametrize("wijziging", [
    {"temp_c": ""},                                   # temperatuur mag ontbreken
    {"is_defect": "1", "defect_type": "randscheur"},
    {"is_defect": "J", "defect_type": "randscheur"},
    {"installatie_code": " ww1 ", "staalkwaliteit": "s235jr", "ploeg": "VROEG"},  # hoofdletters/spaties
    {"start_ts": "2026-11-10 06:00+0100"},
    {"start_ts": "2026-11-10T05:00:00Z"},
])
def test_toegelaten_varianten(wijziging):
    assert valideer_productie(prod(wijziging), REF).empty


def test_dubbele_rol_eerste_blijft():
    af = valideer_productie(prod({}, {}, {}), REF)
    assert list(af.regelnummer) == [2, 3]
    assert set(af.regel) == {"P08_dubbele_rij"}


def test_zelfde_rol_op_andere_installatie_is_geen_dubbel():
    assert valideer_productie(prod({}, {"installatie_code": "BL1"}), REF).empty


def test_dubbel_van_afgekeurde_rij_telt_niet():
    """Is de eerste kopie fout, dan is de tweede (goede) kopie geen dubbel."""
    af = valideer_productie(prod({"weight_t": "n/a"}, {}), REF)
    assert list(af.regelnummer) == [1]


def test_eerste_falende_regel_wint():
    af = valideer_productie(prod({"coil_id": "", "installatie_code": "XX9"}), REF)
    assert len(af) == 1 and af.regel.iloc[0] == "P01_verplicht_veld_leeg"


def test_regelnummers_tellen_vanaf_1():
    af = valideer_productie(prod({}, {}, {"installatie_code": "XX9", "coil_id": "X"}), REF)
    assert list(af.regelnummer) == [2, 3]


# --- stilstand ---------------------------------------------------------------
def test_goede_stop_wordt_aanvaard():
    assert valideer_stilstand(stil({}), REF).empty


@pytest.mark.parametrize("wijziging, regel", [
    ({"event_id": ""}, "D01_verplicht_veld_leeg"),
    ({"start_ts": ""}, "D01_verplicht_veld_leeg"),
    ({"start_ts": "gisteren"}, "D02_ongeldig_type"),
    ({"end_ts": "2026-11-10"}, "D02_ongeldig_type"),
    ({"gepland": "soms"}, "D02_ongeldig_type"),
    ({"bron": "email"}, "D03_onbekende_code"),
    ({"installatie_code": "XX9"}, "D03_onbekende_code"),
    ({"reden_code": "XYZ-999"}, "D03_onbekende_code"),
    ({"start_ts": "2023-12-31T10:00:00+01:00", "end_ts": "2023-12-31T11:00:00+01:00"}, "D04_datum_buiten_kalender"),
    ({"end_ts": "2026-11-10T09:00:00+01:00"}, "D05_buiten_bereik"),
    ({"end_ts": "2026-11-10T10:00:00+01:00"}, "D05_buiten_bereik"),  # duur 0
    ({"end_ts": "2026-11-20T10:00:00+01:00"}, "D05_buiten_bereik"),  # > 7 dagen
])
def test_foute_stop(wijziging, regel):
    assert eerste_regel(valideer_stilstand(stil(wijziging), REF)) == regel


@pytest.mark.parametrize("wijziging", [
    {"reden_code": ""},    # onbekende oorzaak -> reason_key -1
    {"end_ts": ""},        # loopt nog
    {"gepland": ""},       # standaard uit dim_downtime_reason
    {"bron": "APP"},
])
def test_toegelaten_stops(wijziging):
    assert valideer_stilstand(stil(wijziging), REF).empty


def test_productiedag_nachtploeg():
    """01:00 op 1/1/2024 hoort bij productiedag 31/12/2023 -> buiten de kalender."""
    af = valideer_stilstand(stil({"start_ts": "2024-01-01T01:00:00+01:00",
                                  "end_ts": "2024-01-01T02:00:00+01:00"}), REF)
    assert eerste_regel(af) == "D04_datum_buiten_kalender"


def test_dubbele_stop():
    af = valideer_stilstand(stil({}, {}), REF)
    assert list(af.regelnummer) == [2] and af.regel.iloc[0] == "D08_dubbele_rij"


# --- samen met de generator --------------------------------------------------
def test_generator_vuile_rijen_worden_exact_gevonden():
    """Elke bewust foute rij van de generator wordt afgekeurd, geen enkele goede rij."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "generator"))
    import generate as g
    ref = Referentie(frozenset(g.INSTALLATIES), frozenset(g.PLOEGEN),
                     frozenset({"MECH-LAGER", "MECH-HYDR", "MECH-WALS", "ELEK-MOTOR", "ELEK-PLC", "ELEK-NET",
                                "WISS-WALS", "WISS-PROD", "WACHT-MAT", "WACHT-KRAAN", "ONDH-GEPLAND"}),
                     {"S235JR": (1.5, 20), "S355J2": (2, 25), "HC340LA": (0.5, 2.5), "DC01": (0.35, 3),
                      "DC04": (0.4, 2.5), "DX51D": (0.4, 3)}, date(2024, 1, 1), date(2030, 12, 31))
    g.simuleer.cache_clear()
    for d in [date(2026, 6, n) for n in range(1, 8)]:
        p, s, _, v = g.genereer_dag(d, vuil=0.02)
        verwacht = set(zip(v.bestand, v.regelnummer))
        gevonden = {("production", n) for n in valideer_productie(p, ref).regelnummer} | \
                   {("downtime", n) for n in valideer_stilstand(s, ref).regelnummer}
        assert gevonden == verwacht, d
