#!/usr/bin/env python3
"""
SteelLine datagenerator
=======================

Simuleert een staalfabriek met twee lijnen en schrijft per productiedag twee CSV's
naar de landingszone, in exact het formaat van de staging-tabellen:

    landing/production_YYYY-MM-DD.csv   -> staging.production_raw
    landing/downtime_YYYY-MM-DD.csv     -> staging.downtime_raw

Daarnaast komt er "grondwaarheid" in labels/ (NIET door de ETL geladen):

    labels/anomalies_YYYY-MM-DD.csv     ingebouwde afwijkingen (voor fase 5)
    labels/vuile_rijen_YYYY-MM-DD.csv   bewust foute rijen (voor de ETL-tests)

Productieroute
--------------
    WW1 warmwals --(koudroute, dag+1)--> BL1 beitslijn --(dag+1)--> KW1 koudwals --(dag+1)--> GO1 gloeioven
    Constructiestaal (S235JR, S355J2) verlaat de fabriek als warmband na WW1.

Een productiedag D loopt van D 06:00 tot D+1 06:00 (Europe/Brussels); de nachtploeg
hoort bij de dag waarop hij begint. Alle tijden worden intern in UTC berekend, zodat
zomer-/wintertijd geen fouten geeft.

Deterministisch: elke (seed, installatie, dag) heeft een eigen random-generator. Dezelfde
dag opnieuw genereren geeft exact dezelfde bestanden -> de ETL kan veilig herladen.

Gebruik
-------
    python generate.py                          # laatste volledige productiedag
    python generate.py --dag 2026-11-11
    python generate.py --historiek 365          # 12 maanden tot en met de laatste volledige dag
    python generate.py --van 2025-11-01 --tot 2026-11-11 --overschrijven
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

TZ = ZoneInfo("Europe/Brussels")
UTC = timezone.utc
REPO = Path(__file__).resolve().parent.parent
ANKER = date(2024, 1, 1)  # referentiedag voor cycli (slijtage koudwals)

log = logging.getLogger("generator")

# ---------------------------------------------------------------------------
# Instellingen
# ---------------------------------------------------------------------------
SEED = 42  # wordt overschreven door --seed

PLOEGEN = {  # naam -> (start, eind) lokale tijd; nacht eindigt de volgende dag
    "vroeg": (time(6), time(14)),
    "laat": (time(14), time(22)),
    "nacht": (time(22), time(6)),
}

# Staalkwaliteiten: aandeel in de productie, route, diktes (mm)
KWALITEITEN = {
    #  code       aandeel route    warmband-doelen                  koudgewalste doelen
    "S235JR":  (0.25, "warm", [2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 15.0], None),
    "S355J2":  (0.20, "warm", [3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 15.0, 18.0], None),
    "DC01":    (0.15, "koud", [2.0, 2.3, 2.5], [0.50, 0.70, 0.80, 1.00, 1.20, 1.50]),
    "DC04":    (0.15, "koud", [2.0, 2.2], [0.60, 0.70, 0.80, 1.00, 1.20]),
    "DX51D":   (0.15, "koud", [2.0, 2.3, 2.5], [0.50, 0.60, 0.75, 1.00, 1.50, 2.00]),
    "HC340LA": (0.10, "koud", [2.0, 2.2], [0.80, 1.00, 1.20, 1.50]),
}


@dataclass(frozen=True)
class Installatie:
    code: str
    capaciteit: float                     # t/u bij 100 % prestatie
    temp: tuple[float, float]             # (gemiddelde, standaardafwijking) °C
    temp_piek: tuple[float, float]        # extra °C bij een temperatuurpiek
    storingen_per_uur: float              # ongeplande stops per bemand uur
    redenen: dict[str, float]             # reden_code -> gewicht
    defecten: tuple[str, ...]             # mogelijke defecttypes
    basis_defect: float                   # kans op defect per rol
    walswissel_u: float | None            # elke X uur draaien een walswissel
    walswissel_min: tuple[float, float]
    productwissel_min: tuple[float, float]
    bron: str | None                      # installatie die de rollen aanlevert


INSTALLATIES: dict[str, Installatie] = {
    "WW1": Installatie(
        "WW1", 600, (880, 12), (60, 150), 0.060,
        {"MECH-HYDR": 3, "MECH-WALS": 2, "ELEK-MOTOR": 2, "ELEK-PLC": 3, "ELEK-NET": 0.5, "WACHT-KRAAN": 2},
        ("oppervlaktefout", "randscheur"), 0.015, 4.0, (20, 30), (15, 25), None),
    "BL1": Installatie(
        "BL1", 250, (85, 1.5), (15, 30), 0.040,
        {"MECH-HYDR": 2, "ELEK-MOTOR": 2, "ELEK-PLC": 3, "ELEK-NET": 0.5, "WACHT-KRAAN": 3},
        ("beitsfout", "oppervlaktefout"), 0.010, None, (0, 0), (5, 10), "WW1"),
    "KW1": Installatie(
        "KW1", 200, (110, 8), (25, 45), 0.040,
        {"MECH-HYDR": 2, "MECH-WALS": 2, "ELEK-MOTOR": 2, "ELEK-PLC": 3, "ELEK-NET": 0.5, "WACHT-KRAAN": 1},
        ("vlakheidsfout", "randscheur"), 0.015, 8.0, (25, 40), (8, 15), "BL1"),
    "GO1": Installatie(
        "GO1", 90, (720, 6), (50, 90), 0.020,
        {"ELEK-MOTOR": 1, "ELEK-PLC": 3, "ELEK-NET": 0.5, "WACHT-KRAAN": 2},
        ("hardheid buiten spec", "oppervlaktefout"), 0.008, None, (0, 0), (10, 20), "KW1"),
}

STOP_MEDIAAN_MIN = {  # mediane duur van ongeplande stops (lognormaal, sigma 0.6)
    "MECH-LAGER": 30, "MECH-HYDR": 35, "MECH-WALS": 60, "ELEK-MOTOR": 45,
    "ELEK-PLC": 15, "ELEK-NET": 20, "WACHT-KRAAN": 12,
}

DIKTE_TOLERANTIE = 0.03     # > 3 % afwijking van het doel = defect
KANS_ONBEKENDE_REDEN = 0.04  # operator vult geen oorzaak in
KANS_TEMP_PIEK = 0.003
NACHT_DEFECT_FACTOR = 1.6
SLIJTAGE_CYCLUS = 70        # dagen tussen twee revisies van KW1

# Scenario's = bewust ingebouwde afwijkingen (grondwaarheid in labels/)
#  naam               installaties            kans/dag  duur (dagen)
SCENARIOS = {
    "stroomstoring":    (("WW1", "BL1", "KW1", "GO1"), 1 / 150, 1),
    "temperatuurdrift": (("GO1",),                     1 / 90,  3),
    "diktekalibratie":  (("WW1",),                     1 / 100, 2),
    "defectgolf":       (("KW1",),                     1 / 80,  1),
    "trage_lijn":       (("BL1",),                     1 / 120, 5),
}


# ---------------------------------------------------------------------------
# Hulpfuncties
# ---------------------------------------------------------------------------
def rng(*delen) -> np.random.Generator:
    """Eigen, reproduceerbare random-generator per (seed, ...)."""
    sleutel = "|".join(map(str, (SEED, *delen))).encode()
    return np.random.default_rng(int.from_bytes(hashlib.sha256(sleutel).digest()[:8], "little"))


def lokaal_naar_utc(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=TZ).astimezone(UTC)


def iso(ts: datetime) -> str:
    return ts.astimezone(TZ).isoformat(timespec="seconds")


@lru_cache(maxsize=None)
def feestdagen(jaar: int) -> frozenset[date]:
    """Belgische wettelijke feestdagen (zelfde lijst als dim_date)."""
    a, b, c = jaar % 19, jaar // 100, jaar % 100
    d, e = b // 4, b % 4
    g = (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    pasen = date(jaar, (h + l - 7 * m + 114) // 31, (h + l - 7 * m + 114) % 31 + 1)
    vast = [(1, 1), (5, 1), (7, 21), (8, 15), (11, 1), (11, 11), (12, 25)]
    return frozenset([date(jaar, mm, dd) for mm, dd in vast]
                     + [pasen + timedelta(days=n) for n in (1, 39, 50)])


def is_rustdag(d: date) -> bool:
    return d.isoweekday() == 7 or d in feestdagen(d.year)


def bemande_ploegen(code: str, d: date) -> list[str]:
    """Minder productie in het weekend: zaterdag geen nacht, zondag/feestdag enkel WW1 vroeg."""
    if is_rustdag(d):
        return ["vroeg"] if code == "WW1" else []
    if d.isoweekday() == 6:
        return ["vroeg", "laat"]
    return ["vroeg", "laat", "nacht"]


def ploeg_venster(d: date, ploeg: str) -> tuple[datetime, datetime]:
    start, eind = PLOEGEN[ploeg]
    return lokaal_naar_utc(d, start), lokaal_naar_utc(d + timedelta(days=1) if eind <= start else d, eind)


def ploeg_van(d: date, ts: datetime) -> str:
    for p in PLOEGEN:
        s, e = ploeg_venster(d, p)
        if s <= ts < e:
            return p
    return "nacht"


def slijtage_kw1(d: date) -> float:
    """0 net na de revisie, loopt op tot ~1 vlak voor de volgende revisie."""
    return ((d - ANKER).days % SLIJTAGE_CYCLUS) / SLIJTAGE_CYCLUS


def actieve_scenarios(d: date) -> list[tuple[str, date, int]]:
    """[(naam, startdag, dagnummer binnen het scenario)] voor dag d."""
    uit = []
    for naam, (_, kans, duur) in SCENARIOS.items():
        for k in range(duur):
            start = d - timedelta(days=k)
            if rng("scenario", naam, start).random() < kans:
                uit.append((naam, start, k))
                break
    return uit


def gepland_onderhoud(code: str, d: date) -> list[tuple[datetime, float, str]]:
    """Vaste onderhoudsmomenten: (start UTC, duur in uren, opmerking)."""
    wd = d.isoweekday()
    if code == "WW1" and wd == 3:
        return [(lokaal_naar_utc(d, time(6)), 6, "Wekelijks preventief onderhoud warmwals")]
    if code == "BL1" and wd == 4:
        return [(lokaal_naar_utc(d, time(6)), 4, "Wekelijks onderhoud beitsbaden")]
    if code == "KW1":
        if (d - ANKER).days % SLIJTAGE_CYCLUS == 0:
            return [(lokaal_naar_utc(d, time(6)), 16, "Grote revisie koudwals: lagers en walsen vervangen")]
        if wd == 2:
            return [(lokaal_naar_utc(d, time(6)), 4, "Wekelijks preventief onderhoud koudwals")]
    if code == "GO1" and wd == 1 and d.day <= 7:
        return [(lokaal_naar_utc(d, time(6)), 8, "Maandelijks onderhoud gloeioven")]
    return []


# ---------------------------------------------------------------------------
# Simulatie
# ---------------------------------------------------------------------------
@dataclass
class Rol:
    coil_id: str
    kwaliteit: str
    route: str
    gewicht: float
    warm_doel: float
    koud_doel: float | None


@dataclass
class Stop:
    start: datetime
    eind: datetime
    reden: str
    gepland: bool
    opmerking: str = ""


@dataclass
class Dag:
    productie: list[dict] = field(default_factory=list)
    stops: list[Stop] = field(default_factory=list)
    labels: list[dict] = field(default_factory=list)
    uit: list[Rol] = field(default_factory=list)  # goede rollen voor de volgende installatie


def nieuwe_rollen(d: date):
    """Oneindige stroom nieuwe rollen voor de warmwals, in campagnes per kwaliteit."""
    r = rng("rollen", d)
    codes = list(KWALITEITEN)
    kansen = np.array([KWALITEITEN[c][0] for c in codes])
    while True:
        kw = codes[r.choice(len(codes), p=kansen / kansen.sum())]
        _, route, warm, koud = KWALITEITEN[kw]
        warm_doel = float(r.choice(warm))
        koud_doel = float(r.choice(koud)) if koud else None
        for _ in range(int(r.integers(15, 41))):
            yield Rol("", kw, route, float(np.clip(r.normal(23, 3), 12, 32)), warm_doel, koud_doel)


@lru_cache(maxsize=None)
def simuleer(code: str, d: date) -> Dag:
    inst = INSTALLATIES[code]
    r = rng("sim", code, d)
    dag = Dag()
    scen = {naam: (start, k) for naam, start, k in actieve_scenarios(d) if code in SCENARIOS[naam][0]}
    w = slijtage_kw1(d) if code == "KW1" else 0.0

    vensters = [(p, *ploeg_venster(d, p)) for p in bemande_ploegen(code, d)]

    def label(soort: str, omschrijving: str, s: datetime | None = None, e: datetime | None = None, coil: str = ""):
        if vensters:  # geen label als de installatie die dag niet draait
            dag.labels.append(dict(installatie_code=code, soort=soort, start_ts=iso(s or vensters[0][1]),
                                   end_ts=iso(e or vensters[-1][2]), coil_id=coil, omschrijving=omschrijving))

    # --- aanvoer --------------------------------------------------------------
    if inst.bron is None:
        bron = nieuwe_rollen(d)
        volgnr = 0
    else:
        bron = iter(simuleer(inst.bron, d - timedelta(days=1)).uit)

    # --- kandidaat-stops: onderhoud, scenario's, storingen --------------------
    kandidaten: list[Stop] = []
    for start, uren, opm in gepland_onderhoud(code, d):
        kandidaten.append(Stop(start, start + timedelta(hours=uren), "ONDH-GEPLAND", True, opm))

    if "stroomstoring" in scen:
        rs = rng("stroomstoring", scen["stroomstoring"][0])  # zelfde tijdstip voor alle installaties
        s = lokaal_naar_utc(d, time(6)) + timedelta(minutes=float(rs.uniform(0, 20 * 60)))
        e = s + timedelta(minutes=float(rs.uniform(180, 480)))
        kandidaten.append(Stop(s, e, "ELEK-NET", False, "Netstoring op de hele site"))
        label("stroomstoring", "Lange stroomstoring op alle installaties tegelijk", s, e)

    redenen = list(inst.redenen)
    gewichten = np.array(list(inst.redenen.values()), dtype=float)
    for _, vs, ve in vensters:
        uren = (ve - vs).total_seconds() / 3600
        for _ in range(r.poisson(inst.storingen_per_uur * uren)):
            reden = redenen[r.choice(len(redenen), p=gewichten / gewichten.sum())]
            s = vs + timedelta(hours=float(r.uniform(0, uren)))
            duur = float(r.lognormal(np.log(STOP_MEDIAAN_MIN[reden]), 0.6))
            kandidaten.append(Stop(s, s + timedelta(minutes=duur), reden, False))
        if code == "KW1":  # lagerslijtage: steeds vaker en langer
            for _ in range(r.poisson(0.01 * (1 + 8 * w * w) * uren)):
                s = vs + timedelta(hours=float(r.uniform(0, uren)))
                duur = float(r.lognormal(np.log(STOP_MEDIAAN_MIN["MECH-LAGER"] * (1 + 2 * w)), 0.6))
                kandidaten.append(Stop(s, s + timedelta(minutes=duur), "MECH-LAGER", False))

    # alleen binnen bemande vensters, geen overlap (eerste stop wint)
    stops: list[Stop] = []
    for st in sorted(kandidaten, key=lambda x: x.start):
        for _, vs, ve in vensters:
            s, e = max(st.start, vs), min(st.eind, ve)
            if e - s >= timedelta(minutes=1) and (not stops or s >= stops[-1].eind):
                stops.append(Stop(s, e, st.reden, st.gepland, st.opmerking))

    # --- productie: vensters vullen met rollen --------------------------------
    perf_factor = 1.0 - 0.08 * w
    if "trage_lijn" in scen:
        perf_factor *= 0.75
        label("trage_lijn", "Doorvoer ~25 % lager (verstopte sproeiers)")
    temp_extra = 0.0
    if "temperatuurdrift" in scen:
        k = scen["temperatuurdrift"][1]
        temp_extra = 15 + 15 * k
        label("temperatuurdrift", f"Oventemperatuur +{temp_extra:.0f} °C (defecte regelaar, dag {k + 1})")
    dikte_bias = 0.0
    if "diktekalibratie" in scen:
        dikte_bias = 0.06
        label("diktekalibratie", "Diktemeter meet systematisch +0,06 mm")
    defectgolf_ploeg = None
    if "defectgolf" in scen and vensters:
        defectgolf_ploeg = vensters[int(r.integers(len(vensters)))][0]
        _, s, e = next(v for v in vensters if v[0] == defectgolf_ploeg)
        label("defectgolf", f"Defectgolf tijdens ploeg {defectgolf_ploeg} (vervuilde walsen)", s, e)

    wachtrij: list[Rol] = []
    vorige_kwaliteit = None
    sinds_walswissel = 0.0
    uitgehongerd = False
    stop_i = 0

    def volgende_rol() -> Rol | None:
        if wachtrij:
            return wachtrij.pop()
        return next(bron, None)

    for ploeg, vs, ve in vensters:
        if uitgehongerd:
            break
        t = vs
        while t < ve:
            # binnen of voorbij een stop?
            while stop_i < len(stops) and stops[stop_i].eind <= t:
                stop_i += 1
            if stop_i < len(stops) and stops[stop_i].start <= t:
                t = stops[stop_i].eind
                continue
            rol = volgende_rol()
            if rol is None:
                # geen materiaal meer: rest van de ploeg wachten, latere ploegen worden niet bemand
                del stops[stop_i:]
                if ve - t >= timedelta(minutes=5):
                    stops.append(Stop(t, ve, "WACHT-MAT", False, "Geen rollen van de vorige installatie"))
                uitgehongerd = True
                break
            # wissels gaan vóór de rol; ze worden pas vastgelegd als wissel + rol samen passen
            wissels = []
            if vorige_kwaliteit is not None and rol.kwaliteit != vorige_kwaliteit:
                wissels.append(("WISS-PROD", float(r.uniform(*inst.productwissel_min))))
            if inst.walswissel_u and sinds_walswissel >= inst.walswissel_u * 60:
                wissels.append(("WISS-WALS", float(r.uniform(*inst.walswissel_min))))
            start = t + timedelta(minutes=sum(m for _, m in wissels))

            perf = float(np.clip(r.normal(0.90, 0.04), 0.6, 1.0)) * perf_factor
            gewicht = rol.gewicht * (1 - float(r.uniform(0.003, 0.008))) if code in ("BL1", "KW1") else rol.gewicht
            duur = gewicht / (inst.capaciteit * perf) * 60
            einde = start + timedelta(minutes=duur)
            volgende_stop = stops[stop_i].start if stop_i < len(stops) else None
            if einde > ve or (volgende_stop is not None and einde > volgende_stop):
                wachtrij.append(rol)
                if volgende_stop is not None and volgende_stop < ve:
                    t = stops[stop_i].eind
                    continue
                break
            for reden, minuten in wissels:
                stops.insert(stop_i, Stop(t, t + timedelta(minutes=minuten), reden, True))
                stop_i += 1
                t += timedelta(minutes=minuten)
                if reden == "WISS-WALS":
                    sinds_walswissel = 0.0

            # --- meetwaarden ---
            doel = rol.koud_doel if code in ("KW1", "GO1") else rol.warm_doel
            sd = 0.006 * (1 + 1.5 * w)
            afwijking = r.normal(0, sd)
            if r.random() < 0.004:
                afwijking += r.normal(0, 0.04)  # af en toe een uitschieter
            dikte = doel * (1 + afwijking) + dikte_bias

            temp = float(r.normal(inst.temp[0], inst.temp[1])) + temp_extra
            piek = r.random() < KANS_TEMP_PIEK
            if piek:
                temp += float(r.uniform(*inst.temp_piek))

            kans = inst.basis_defect * (1 + w)
            if ploeg == "nacht":
                kans *= NACHT_DEFECT_FACTOR
            if piek:
                kans *= 5
            if temp_extra:
                kans *= 1 + temp_extra / 15
            if ploeg == defectgolf_ploeg:
                kans = 0.25
            defect_type = None
            if abs(dikte - doel) / doel > DIKTE_TOLERANTIE:
                defect_type = "dikte buiten tolerantie"
            elif r.random() < kans:
                defect_type = inst.defecten[int(r.integers(len(inst.defecten)))]

            if code == "WW1":
                volgnr += 1
                rol.coil_id = f"C{d:%Y%m%d}{volgnr:04d}"
            rol.gewicht = gewicht

            dag.productie.append(dict(
                coil_id=rol.coil_id, productie_datum=d.isoformat(), ploeg=ploeg_van(d, t),
                installatie_code=code, staalkwaliteit=rol.kwaliteit, start_ts=iso(t),
                weight_t=f"{gewicht:.3f}", thickness_mm=f"{dikte:.3f}", target_thickness_mm=f"{doel:.3f}",
                temp_c=f"{temp:.1f}", duration_min=f"{duur:.2f}",
                is_defect="true" if defect_type else "false", defect_type=defect_type or ""))
            if piek:
                label("temperatuurpiek", f"Temperatuurpiek {temp:.0f} °C", t, einde, rol.coil_id)
            if defect_type is None and (code != "WW1" or rol.route == "koud") and code != "GO1":
                dag.uit.append(Rol(rol.coil_id, rol.kwaliteit, rol.route, gewicht, rol.warm_doel, rol.koud_doel))

            vorige_kwaliteit = rol.kwaliteit
            sinds_walswissel += duur
            t = einde + timedelta(minutes=float(r.uniform(0.2, 1.0)))  # transport tussen rollen

    dag.stops = stops
    return dag


# ---------------------------------------------------------------------------
# Uitvoer
# ---------------------------------------------------------------------------
PROD_KOLOMMEN = ["coil_id", "productie_datum", "ploeg", "installatie_code", "staalkwaliteit", "start_ts",
                 "weight_t", "thickness_mm", "target_thickness_mm", "temp_c", "duration_min",
                 "is_defect", "defect_type"]
STIL_KOLOMMEN = ["event_id", "bron", "installatie_code", "reden_code", "start_ts", "end_ts", "gepland", "opmerking"]


def genereer_dag(d: date, vuil: float) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    prod, stil, labels = [], [], []
    for code in INSTALLATIES:
        dag = simuleer(code, d)
        prod.extend(dag.productie)
        r = rng("redenen", code, d)
        for n, st in enumerate(sorted(dag.stops, key=lambda x: x.start), start=1):
            reden = st.reden
            if not st.gepland and r.random() < KANS_ONBEKENDE_REDEN:
                reden = ""  # operator heeft geen oorzaak ingevuld -> reason_key -1
            stil.append(dict(event_id=f"{code}-{d:%Y%m%d}-{n:03d}", bron="sensor", installatie_code=code,
                             reden_code=reden, start_ts=iso(st.start), end_ts=iso(st.eind),
                             gepland="j" if st.gepland else "n", opmerking=st.opmerking))
        labels.extend(dict(datum=d.isoformat(), **lab) for lab in dag.labels)

    df_p = pd.DataFrame(prod, columns=PROD_KOLOMMEN).astype(str)
    df_s = pd.DataFrame(stil, columns=STIL_KOLOMMEN).astype(str)
    df_l = pd.DataFrame(labels, columns=["datum", "installatie_code", "soort", "start_ts", "end_ts",
                                         "coil_id", "omschrijving"])
    df_p, df_s, df_v = maak_vuil(d, df_p, df_s, vuil)
    return df_p, df_s, df_l, df_v


def maak_vuil(d: date, df_p: pd.DataFrame, df_s: pd.DataFrame, kans: float):
    """Voeg bewust foute rijen toe zodat de validatie in de ETL iets te doen heeft."""
    r = rng("vuil", d)
    fouten = []
    soorten_p = ["leeg_coil_id", "tekst_in_getal", "onbekende_installatie", "dikte_buiten_kwaliteit",
                 "negatief_gewicht", "dubbele_rij"]
    soorten_s = ["einde_voor_start", "onbekende_reden", "lege_start"]

    for idx in np.flatnonzero(r.random(len(df_p)) < kans):
        soort = soorten_p[int(r.integers(len(soorten_p)))]
        sleutel = df_p.at[idx, "coil_id"]
        if soort == "leeg_coil_id":
            df_p.at[idx, "coil_id"] = ""
        elif soort == "tekst_in_getal":
            df_p.at[idx, "weight_t"] = "n/a"
        elif soort == "onbekende_installatie":
            df_p.at[idx, "installatie_code"] = "XX9"
        elif soort == "dikte_buiten_kwaliteit":
            df_p.at[idx, "thickness_mm"] = "99.900"
        elif soort == "negatief_gewicht":
            df_p.at[idx, "weight_t"] = "-" + df_p.at[idx, "weight_t"]
        elif soort == "dubbele_rij":
            df_p = pd.concat([df_p, df_p.iloc[[idx]]], ignore_index=True)
            fouten.append(dict(datum=d.isoformat(), bestand="production", regelnummer=len(df_p), soort=soort,
                               sleutel=df_p.iloc[-1]["coil_id"]))
            continue
        fouten.append(dict(datum=d.isoformat(), bestand="production", regelnummer=idx + 1, soort=soort,
                           sleutel=sleutel))

    for idx in np.flatnonzero(r.random(len(df_s)) < kans * 3):  # minder stops -> iets hogere kans
        soort = soorten_s[int(r.integers(len(soorten_s)))]
        if soort == "einde_voor_start":
            df_s.at[idx, "start_ts"], df_s.at[idx, "end_ts"] = df_s.at[idx, "end_ts"], df_s.at[idx, "start_ts"]
        elif soort == "onbekende_reden":
            df_s.at[idx, "reden_code"] = "XYZ-999"
        elif soort == "lege_start":
            df_s.at[idx, "start_ts"] = ""
        fouten.append(dict(datum=d.isoformat(), bestand="downtime", regelnummer=idx + 1, soort=soort,
                           sleutel=df_s.at[idx, "event_id"]))

    df_v = pd.DataFrame(fouten, columns=["datum", "bestand", "regelnummer", "soort", "sleutel"])
    return df_p, df_s, df_v


def schrijf_atomair(df: pd.DataFrame, pad: Path) -> None:
    """Eerst naar .tmp, dan hernoemen: de ETL ziet nooit een half bestand."""
    tmp = pad.with_suffix(pad.suffix + ".tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, pad)


def schrijf_dag(d: date, landing: Path, labels: Path, vuil: float, overschrijven: bool) -> bool:
    p_pad, s_pad = landing / f"production_{d}.csv", landing / f"downtime_{d}.csv"
    if not overschrijven and p_pad.exists() and s_pad.exists():
        log.info("%s bestaat al, overgeslagen", d)
        return False
    df_p, df_s, df_l, df_v = genereer_dag(d, vuil)
    # labels eerst: zodra de landingsbestanden er staan, kan de ETL beginnen
    if not df_l.empty:
        schrijf_atomair(df_l, labels / f"anomalies_{d}.csv")
    if not df_v.empty:
        schrijf_atomair(df_v, labels / f"vuile_rijen_{d}.csv")
    schrijf_atomair(df_s, s_pad)
    schrijf_atomair(df_p, p_pad)
    log.info("%s: %d rollen, %d stops, %d labels, %d vuile rijen",
             d, len(df_p), len(df_s), len(df_l), len(df_v))
    return True


def laatste_volledige_dag(nu: datetime | None = None) -> date:
    """Productiedag D is pas af om D+1 06:00 (einde nachtploeg)."""
    nu = (nu or datetime.now(TZ)).astimezone(TZ)
    return nu.date() - timedelta(days=1 if nu.time() >= time(6) else 2)


def main(argv: list[str] | None = None) -> int:
    global SEED
    ap = argparse.ArgumentParser(description="SteelLine datagenerator")
    groep = ap.add_mutually_exclusive_group()
    groep.add_argument("--dag", type=date.fromisoformat, help="één productiedag (YYYY-MM-DD)")
    groep.add_argument("--van", type=date.fromisoformat, help="eerste dag van een reeks")
    groep.add_argument("--historiek", type=int, metavar="N", help="de laatste N volledige dagen")
    ap.add_argument("--tot", type=date.fromisoformat, help="laatste dag van een reeks (standaard: laatste volledige dag)")
    ap.add_argument("--landing", type=Path, default=Path(os.environ.get("LANDING_DIR", REPO / "landing")))
    ap.add_argument("--labels", type=Path, default=Path(os.environ.get("LABELS_DIR", REPO / "labels")))
    ap.add_argument("--seed", type=int, default=int(os.environ.get("GENERATOR_SEED", 42)))
    ap.add_argument("--vuil", type=float, default=0.003, help="aandeel bewust foute rijen (0 = geen)")
    ap.add_argument("--overschrijven", action="store_true", help="bestaande bestanden opnieuw maken")
    ap.add_argument("-q", "--stil", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING if args.stil else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    SEED = args.seed
    simuleer.cache_clear()

    laatste = laatste_volledige_dag()
    if args.dag:
        dagen = [args.dag]
    elif args.historiek:
        dagen = [laatste - timedelta(days=n) for n in range(args.historiek - 1, -1, -1)]
    elif args.van:
        tot = args.tot or laatste
        dagen = [args.van + timedelta(days=n) for n in range((tot - args.van).days + 1)]
    else:
        dagen = [laatste]

    if any(d > laatste for d in dagen):
        ap.error(f"dag in de toekomst of nog niet afgelopen (laatste volledige dag: {laatste})")

    args.landing.mkdir(parents=True, exist_ok=True)
    args.labels.mkdir(parents=True, exist_ok=True)
    gemaakt = sum(schrijf_dag(d, args.landing, args.labels, args.vuil, args.overschrijven) for d in dagen)
    log.info("klaar: %d van %d dagen geschreven naar %s", gemaakt, len(dagen), args.landing)
    return 0


if __name__ == "__main__":
    sys.exit(main())
