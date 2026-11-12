# Datagenerator

`generate.py` simuleert de SteelLine-fabriek en schrijft per productiedag twee CSV's naar `landing/`,
in exact de kolommen van `staging.production_raw` en `staging.downtime_raw`.

```
landing/production_2026-11-11.csv    1 rij per rol per installatie
landing/downtime_2026-11-11.csv      1 rij per stilstand
labels/anomalies_2026-11-11.csv      grondwaarheid: ingebouwde afwijkingen (fase 5)
labels/vuile_rijen_2026-11-11.csv    grondwaarheid: bewust foute rijen (ETL-tests)
```

`landing/` en `labels/` staan in `.gitignore`. De ETL leest **alleen** `landing/`.

## Gebruik

```bash
cd ~/data-platform-lab
python3 -m venv .venv && . .venv/bin/activate
pip install -r generator/requirements.txt

python generator/generate.py --historiek 365     # 12 maanden historiek (± 15 s, ± 20 MB)
python generator/generate.py                     # laatste volledige productiedag (elke nacht)
python generator/generate.py --dag 2026-11-11 --overschrijven
python -m pytest -q generator/tests              # tests
```

| Optie | Standaard | Betekenis |
| --- | --- | --- |
| `--dag` / `--van --tot` / `--historiek N` | laatste volledige dag | welke dagen |
| `--landing`, `--labels` | `landing/`, `labels/` (of `LANDING_DIR`, `LABELS_DIR`) | uitvoermappen |
| `--seed` | 42 (of `GENERATOR_SEED`) | andere seed = andere fabriek |
| `--vuil` | 0.003 | aandeel bewust foute rijen, `0` = schoon |
| `--overschrijven` | uit | bestaande dagen opnieuw maken |

**Laatste volledige dag:** productiedag D loopt tot D+1 06:00 (einde nachtploeg). Om 02:00 is
"gisteren" dus nog niet af en maakt de generator eergisteren. Plan de nachtelijke run daarom
na 06:00 (week 7), dan staat 's ochtends de data van gisteren klaar.

**Deterministisch:** elke (seed, installatie, dag) heeft een eigen random-generator. Een dag
opnieuw maken geeft exact hetzelfde bestand, ook `coil_id` en `event_id`. De ETL kan dus veilig
herladen met `ON CONFLICT`.

Bestanden worden eerst als `.tmp` geschreven en dan hernoemd: de ETL ziet nooit een half bestand.

## De fabriek

```
WW1 warmwals 600 t/u ──koudroute (dag+1)──▶ BL1 beitslijn 250 t/u ──(dag+1)──▶ KW1 koudwals 200 t/u ──(dag+1)──▶ GO1 gloeioven 90 t/u
   └─ S235JR / S355J2 vertrekken als warmband
```

- Rollen van 12–32 t, in campagnes van 15–40 rollen van dezelfde kwaliteit.
- `coil_id` (`C202611110001`) wordt op de warmwals gegeven en volgt de rol door de koudlijn.
- Een installatie verwerkt de goede rollen die de vorige installatie de dag ervoor maakte.
  Wat ze niet kan verwerken, gaat "naar een andere vestiging". Defecte rollen gaan niet verder.

Typische werkdag: ± 330 rollen op WW1 (± 6 500 t), 140 op BL1, 115 op KW1, 70 op GO1.
Een jaar is ± 185 000 productierijen en ± 8 000 stops.

## Wat de data realistisch maakt

| Patroon | Hoe | Zie je terug in |
| --- | --- | --- |
| Minder productie in het weekend | zaterdag geen nachtploeg; zondag en feestdagen enkel WW1 vroege ploeg | productie per weekdag |
| Weekenddip schuift door de lijn | koudlijn heeft op maandag weinig rollen → `WACHT-MAT` | stilstand per reden en weekdag |
| Nachtploeg meer defecten | defectkans × 1,6 | uitval % per ploeg |
| Koudwals slijt | lagerstoringen (`MECH-LAGER`) steeds vaker en langer, diktespreiding groeit, doorvoer -8 %; grote revisie elke 70 dagen zet alles terug | stilstand KW1 per week (zaagtand) |
| Onderhoud op vaste dagen | WW1 woensdag 6 u, KW1 dinsdag 4 u, BL1 donderdag 4 u, GO1 eerste maandag van de maand 8 u | geplande stilstand |
| Wissels | walswissel elke 4 u (WW1) of 8 u (KW1), productwissel bij een andere kwaliteit | geplande stilstand |
| Ongeplande storingen | Poisson per bemand uur, duur lognormaal per oorzaak; 4 % zonder oorzaak (`reason_key -1`) | MTBF / MTTR |
| Temperatuur schiet soms uit | 0,3 % van de rollen, defectkans × 5 | anomaliedetectie |
| Dikte | ± 0,6 % rond het doel; > 3 % afwijking = defect "dikte buiten tolerantie" | kwaliteit |

## Ingebouwde afwijkingen (voor anomaliedetectie in fase 5)

Ze starten willekeurig maar reproduceerbaar en worden gelogd in `labels/anomalies_*.csv`.

| Soort | Installatie | Kans per dag | Duur | Effect |
| --- | --- | --- | --- | --- |
| `stroomstoring` | alle | 1/150 | 3–8 u | `ELEK-NET`-stop op hetzelfde moment overal |
| `temperatuurdrift` | GO1 | 1/90 | 3 dagen | +30, +45, +60 °C en meer defecten |
| `diktekalibratie` | WW1 | 1/100 | 2 dagen | gemeten dikte systematisch +0,06 mm |
| `defectgolf` | KW1 | 1/80 | 1 ploeg | 25 % defecten |
| `trage_lijn` | BL1 | 1/120 | 5 dagen | doorvoer -25 % |
| `temperatuurpiek` | alle | 0,3 % per rol | 1 rol | +15 tot +150 °C |

## Bewust foute rijen (voor de ETL in week 6)

Ongeveer 0,3 % van de productierijen en 0,9 % van de stops, gelogd in `labels/vuile_rijen_*.csv`
met het regelnummer:

`leeg_coil_id`, `tekst_in_getal`, `onbekende_installatie`, `dikte_buiten_kwaliteit`,
`negatief_gewicht`, `dubbele_rij`, `einde_voor_start`, `onbekende_reden`, `lege_start`.

De ETL moet precies deze rijen in `staging.rejected_row` zetten (of bewust anders afhandelen,
bv. een dubbele rij één keer laden). Zo kun je de validatie automatisch testen.
