"""
Validatieregels voor de ETL, los van de database (dus makkelijk te testen).

Elke functie krijgt een DataFrame met de ruwe CSV-rijen (alles tekst) en de
referentiedata uit de dimensies, en geeft de afgekeurde rijen terug:

    regelnummer | regel | reden

Een rij wordt afgekeurd op de EERSTE regel die faalt. Regelnummer 1 = eerste
datarij (na de kopregel), zoals in staging.*_raw.regelnummer.

De normalisatie hier (trim, hoofd-/kleine letters) moet dezelfde zijn als in
de SQL van load.py, anders keurt Python iets goed dat SQL niet kan koppelen.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

PROD_KOLOMMEN = ["coil_id", "productie_datum", "ploeg", "installatie_code", "staalkwaliteit", "start_ts",
                 "weight_t", "thickness_mm", "target_thickness_mm", "temp_c", "duration_min",
                 "is_defect", "defect_type"]
STIL_KOLOMMEN = ["event_id", "bron", "installatie_code", "reden_code", "start_ts", "end_ts", "gepland", "opmerking"]

WAAR = {"true", "t", "1", "j", "ja", "y", "yes"}
ONWAAR = {"false", "f", "0", "n", "nee", "no"}
BRONNEN = {"sensor", "app"}

TS_RE = r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:?\d{2})$"  # tijdzone verplicht
DATUM_RE = r"^\d{4}-\d{2}-\d{2}$"

# grenzen = CHECK-constraints in sql/001_schema.sql
GEWICHT_MAX_T = 60
DUUR_MAX_MIN = 24 * 60
TEMP_MIN, TEMP_MAX = -20, 1400


@dataclass(frozen=True)
class Referentie:
    """Toegelaten codes en grenzen, ingelezen uit de dimensies."""
    installaties: frozenset[str]
    ploegen: frozenset[str]
    redenen: frozenset[str]
    kwaliteiten: dict[str, tuple[float, float]]  # code -> (min_dikte, max_dikte)
    eerste_dag: date
    laatste_dag: date


# ---------------------------------------------------------------------------
# Hulpfuncties (alle vectorieel)
# ---------------------------------------------------------------------------
def _schoon(s: pd.Series) -> pd.Series:
    return s.fillna("").astype(str).str.strip()


def _getal(s: pd.Series) -> pd.Series:
    """Tekst -> float; NaN als het geen eindig getal is (ook 'n/a', '1,5', 'inf')."""
    x = pd.to_numeric(_schoon(s), errors="coerce")
    return x.where(np.isfinite(x))


def _tijdstip(s: pd.Series) -> pd.Series:
    s = _schoon(s)
    ok = s.str.match(TS_RE)
    ts = pd.to_datetime(s.where(ok), errors="coerce", utc=True, format="ISO8601")
    return ts


def _datum(s: pd.Series) -> pd.Series:
    s = _schoon(s)
    return pd.to_datetime(s.where(s.str.match(DATUM_RE)), errors="coerce", format="%Y-%m-%d")


def _productiedag(ts: pd.Series) -> pd.Series:
    """Productiedag = lokale datum van (tijdstip - 6 u): de nachtploeg hoort bij de dag waarop hij begint."""
    return (ts.dt.tz_convert("Europe/Brussels") - pd.Timedelta(hours=6)).dt.tz_localize(None).dt.normalize()


class _Afkeur:
    """Houdt per rij de eerste gefaalde regel bij."""

    def __init__(self, n: int):
        self.regel = pd.Series([None] * n, dtype=object)
        self.reden = pd.Series([None] * n, dtype=object)

    def open(self) -> pd.Series:
        return self.regel.isna()

    def zet(self, masker: pd.Series, regel: str, reden: pd.Series | str) -> None:
        m = masker.fillna(False).astype(bool).to_numpy() & self.open().to_numpy()
        if not m.any():
            return
        self.regel[m] = regel
        self.reden[m] = reden[m] if isinstance(reden, pd.Series) else reden

    def resultaat(self) -> pd.DataFrame:
        m = self.regel.notna()
        return pd.DataFrame({"regelnummer": np.flatnonzero(m.to_numpy()) + 1,
                             "regel": self.regel[m].to_numpy(),
                             "reden": self.reden[m].to_numpy()})


def _verplicht(df: pd.DataFrame, af: _Afkeur, regel: str, kolommen: list[str]) -> None:
    for k in kolommen:
        af.zet(_schoon(df[k]) == "", regel, f"{k} is leeg")


def _type_getal(df: pd.DataFrame, af: _Afkeur, regel: str, kolom: str, verplicht: bool = True) -> pd.Series:
    ruw = _schoon(df[kolom])
    x = _getal(ruw)
    fout = x.isna() & ((ruw != "") | verplicht)
    af.zet(fout, regel, "'" + ruw + f"' in {kolom} is geen getal")
    return x


def _type_bool(df: pd.DataFrame, af: _Afkeur, regel: str, kolom: str, verplicht: bool = True) -> pd.Series:
    ruw = _schoon(df[kolom]).str.lower()
    fout = ~ruw.isin(WAAR | ONWAAR) & ((ruw != "") | verplicht)
    af.zet(fout, regel, "'" + ruw + f"' in {kolom} is geen ja/nee-waarde")
    return ruw.isin(WAAR).where(ruw != "")


# ---------------------------------------------------------------------------
# Productie
# ---------------------------------------------------------------------------
def valideer_productie(df: pd.DataFrame, ref: Referentie) -> pd.DataFrame:
    df = df.reset_index(drop=True)
    af = _Afkeur(len(df))

    # R01 verplichte velden (temp_c en defect_type mogen leeg zijn)
    _verplicht(df, af, "P01_verplicht_veld_leeg",
               [k for k in PROD_KOLOMMEN if k not in ("temp_c", "defect_type")])

    # R02 types
    gewicht = _type_getal(df, af, "P02_ongeldig_type", "weight_t")
    dikte = _type_getal(df, af, "P02_ongeldig_type", "thickness_mm")
    doel = _type_getal(df, af, "P02_ongeldig_type", "target_thickness_mm")
    duur = _type_getal(df, af, "P02_ongeldig_type", "duration_min")
    temp = _type_getal(df, af, "P02_ongeldig_type", "temp_c", verplicht=False)
    dag = _datum(df["productie_datum"])
    af.zet(dag.isna(), "P02_ongeldig_type", "productie_datum '" + _schoon(df["productie_datum"]) + "' is geen datum (YYYY-MM-DD)")
    ts = _tijdstip(df["start_ts"])
    af.zet(ts.isna(), "P02_ongeldig_type", "start_ts '" + _schoon(df["start_ts"]) + "' is geen tijdstip met tijdzone")
    defect = _type_bool(df, af, "P02_ongeldig_type", "is_defect")

    # R03 codes bestaan in de dimensies
    inst = _schoon(df["installatie_code"]).str.upper()
    af.zet(~inst.isin(ref.installaties), "P03_onbekende_code", "onbekende installatie '" + inst + "'")
    kw = _schoon(df["staalkwaliteit"]).str.upper()
    af.zet(~kw.isin(ref.kwaliteiten.keys()), "P03_onbekende_code", "onbekende staalkwaliteit '" + kw + "'")
    ploeg = _schoon(df["ploeg"]).str.lower()
    af.zet(~ploeg.isin(ref.ploegen), "P03_onbekende_code", "onbekende ploeg '" + ploeg + "'")

    # R04 datum binnen de kalender (dim_date)
    buiten = (dag < pd.Timestamp(ref.eerste_dag)) | (dag > pd.Timestamp(ref.laatste_dag))
    af.zet(buiten, "P04_datum_buiten_kalender", "productie_datum valt buiten dim_date")

    # R05 fysisch bereik
    af.zet((gewicht <= 0) | (gewicht >= GEWICHT_MAX_T), "P05_buiten_bereik",
           "gewicht " + gewicht.astype(str) + f" t ligt niet tussen 0 en {GEWICHT_MAX_T}")
    af.zet((duur <= 0) | (duur > DUUR_MAX_MIN), "P05_buiten_bereik",
           "duur " + duur.astype(str) + " min ligt niet tussen 0 en 1440")
    af.zet((dikte <= 0) | (doel <= 0), "P05_buiten_bereik", "dikte moet groter dan 0 zijn")
    af.zet((temp < TEMP_MIN) | (temp > TEMP_MAX), "P05_buiten_bereik",
           "temperatuur " + temp.astype(str) + f" °C ligt niet tussen {TEMP_MIN} en {TEMP_MAX}")

    # R06 dikte binnen de grenzen van de staalkwaliteit
    lo = kw.map(lambda k: ref.kwaliteiten.get(k, (np.nan, np.nan))[0]).astype(float)
    hi = kw.map(lambda k: ref.kwaliteiten.get(k, (np.nan, np.nan))[1]).astype(float)
    af.zet((dikte < lo) | (dikte > hi) | (doel < lo) | (doel > hi), "P06_dikte_buiten_kwaliteit",
           "dikte " + dikte.astype(str) + " mm buiten [" + lo.astype(str) + ", " + hi.astype(str) + "] voor " + kw)

    # R07 defect en defecttype horen bij elkaar
    heeft_type = _schoon(df["defect_type"]) != ""
    af.zet((defect == True) & ~heeft_type, "P07_defect_inconsistent", "is_defect = ja maar geen defect_type")  # noqa: E712
    af.zet((defect == False) & heeft_type, "P07_defect_inconsistent", "defect_type ingevuld maar is_defect = nee")  # noqa: E712

    # R08 dubbel in het bestand: eerste goede rij blijft, de rest wordt afgekeurd
    sleutel = _schoon(df["coil_id"]) + "|" + inst
    dubbel = sleutel.where(af.open()).duplicated(keep="first") & af.open()
    af.zet(dubbel, "P08_dubbele_rij", "rol " + sleutel + " komt meer dan eens voor in het bestand")

    return af.resultaat()


# ---------------------------------------------------------------------------
# Stilstand
# ---------------------------------------------------------------------------
def valideer_stilstand(df: pd.DataFrame, ref: Referentie) -> pd.DataFrame:
    df = df.reset_index(drop=True)
    af = _Afkeur(len(df))

    # R01 verplicht (end_ts leeg = stilstand loopt nog; reden leeg = onbekend)
    _verplicht(df, af, "D01_verplicht_veld_leeg", ["event_id", "bron", "installatie_code", "start_ts"])

    # R02 types
    start = _tijdstip(df["start_ts"])
    af.zet(start.isna(), "D02_ongeldig_type", "start_ts '" + _schoon(df["start_ts"]) + "' is geen tijdstip met tijdzone")
    eind_ruw = _schoon(df["end_ts"])
    eind = _tijdstip(eind_ruw)
    af.zet(eind.isna() & (eind_ruw != ""), "D02_ongeldig_type", "end_ts '" + eind_ruw + "' is geen tijdstip met tijdzone")
    _type_bool(df, af, "D02_ongeldig_type", "gepland", verplicht=False)

    # R03 codes
    bron = _schoon(df["bron"]).str.lower()
    af.zet(~bron.isin(BRONNEN), "D03_onbekende_code", "onbekende bron '" + bron + "' (sensor/app)")
    inst = _schoon(df["installatie_code"]).str.upper()
    af.zet(~inst.isin(ref.installaties), "D03_onbekende_code", "onbekende installatie '" + inst + "'")
    reden = _schoon(df["reden_code"]).str.upper()
    af.zet((reden != "") & ~reden.isin(ref.redenen), "D03_onbekende_code", "onbekende reden '" + reden + "'")

    # R04 productiedag binnen de kalender
    dag = _productiedag(start)
    af.zet((dag < pd.Timestamp(ref.eerste_dag)) | (dag > pd.Timestamp(ref.laatste_dag)),
           "D04_datum_buiten_kalender", "start_ts valt buiten dim_date")

    # R05 einde na begin, niet absurd lang
    af.zet(eind <= start, "D05_buiten_bereik", "end_ts ligt niet na start_ts")
    af.zet((eind - start) > pd.Timedelta(days=7), "D05_buiten_bereik", "stilstand langer dan 7 dagen")

    # R08 dubbel
    sleutel = bron + "|" + _schoon(df["event_id"])
    dubbel = sleutel.where(af.open()).duplicated(keep="first") & af.open()
    af.zet(dubbel, "D08_dubbele_rij", "event " + sleutel + " komt meer dan eens voor in het bestand")

    return af.resultaat()
