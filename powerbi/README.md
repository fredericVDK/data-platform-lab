# Power BI — SteelLine-rapport

Doel (fase 3): een rapport van 3–4 pagina's waarmee een productiemanager in 30 seconden ziet waar het misloopt.
Deze map bevat het rapport als **Power BI-project** (`.pbip`), zodat het in Git leesbaar en vergelijkbaar is.

## Week 8 — verbinding en model

### Status

- [x] `sql/005_reporting_views.sql` opnieuw uitgevoerd op de server (stilstand-view heeft nu `shift_key`)
- [x] Verbinding vanuit Power BI Desktop via Tailscale met `bi_reader`
- [x] 4 dimensies + 2 views geïmporteerd en opgeschoond in Power Query
- [x] Relaties: sterstructuur, één-op-veel, enkelvoudige filterrichting
- [x] `Datum` gemarkeerd als datumtabel, automatische datum/tijd uit
- [x] Basismetingen (OEE, stilstand) toegevoegd en gecontroleerd tegen SQL
- [x] Opgeslagen als `powerbi/steelline.pbip` en gecommit

---

### 0. Eénmalig op de server

```bash
cd ~/data-platform-lab && git pull
docker exec -i postgres psql -U admin -d steelline -f - < sql/005_reporting_views.sql
grep BI_READER_PASSWORD infra/.env      # wachtwoord voor Power BI
```

### 1. Verbinding

Voorwaarden op de pc: Tailscale staat aan, en `Test-NetConnection mordor -Port 5432` (PowerShell) geeft `TcpTestSucceeded : True`.

1. Power BI Desktop → **Bestand → Opties → Huidig bestand → Gegevens laden**:
   - *Automatische datum/tijd* **uit** (anders maakt Power BI per datumkolom een verborgen kalender);
   - *Nieuwe relaties automatisch detecteren* **uit** (we leggen ze zelf).
2. **Gegevens ophalen → PostgreSQL-database**
   - Server: `mordor:5432`  · Database: `steelline` · Modus: **Importeren**
3. Referenties: tabblad **Database**, gebruiker `bi_reader`, wachtwoord uit `infra/.env`.
4. Melding *"kan geen versleutelde verbinding maken"* → **OK** (onversleuteld). De PostgreSQL-container heeft geen TLS, maar het verkeer loopt door de WireGuard-tunnel van Tailscale en is dus wel versleuteld. Van buiten de tailnet is poort 5432 onbereikbaar.

Lukt het niet?

| Fout | Oorzaak / oplossing |
| --- | --- |
| *Host niet gevonden* | Tailscale op de pc staat uit of MagicDNS is uit → gebruik het `100.x.y.z`-adres |
| *Time-out* | `tailscale serve status` op de server: staat 5432 er nog? |
| *password authentication failed* | Verkeerd wachtwoord; Power BI onthoudt het: **Bestand → Opties → Instellingen voor gegevensbron → Machtigingen wissen** |
| *permission denied for schema staging* | Klopt: `bi_reader` mag alleen `dwh` lezen. Kies tabellen uit `dwh`. |
| *canceling statement due to statement timeout* | Query > 60 s (`statement_timeout` van `bi_reader`). Bij deze datavolumes zou dat niet mogen; meld het. |

### 2. Wat importeren (Navigator → schema `dwh`)

| Tabel in `dwh` | Naam in Power BI | Rol |
| --- | --- | --- |
| `dim_date` | **Datum** | dimensie |
| `dim_shift` | **Ploeg** | dimensie |
| `dim_installation` | **Installatie** | dimensie |
| `dim_downtime_reason` | **Stilstandsoorzaak** | dimensie |
| `vw_oee_shift` | **OEE** | feit: dag × ploeg × installatie |
| `vw_downtime_by_reason` | **Stilstand** | feit: dag × ploeg × installatie × oorzaak |

Bewust **niet** importeren:

- `vw_daily_oee` — is gewoon de som van `vw_oee_shift` per dag. Twee feiten met dezelfde betekenis in één model geven dubbele tellingen en verwarring; Power BI telt de ploegen zelf op.
- `dim_steel_grade`, `fact_production` — pas nodig voor de kwaliteitspagina (week 9). Dan komt `fact_production` erbij met een relatie naar `dim_steel_grade`; de structuur blijft dezelfde ster.

Klik **Gegevens transformeren** (niet *Laden*).

### 3. Power Query — opschonen

Hernoem de queries zoals in de tabel hierboven. Dan per query:

**OEE**
- Verwijder `beschikbaarheid`, `prestatie`, `kwaliteit`, `oee`. Die ratio's gelden per ploeg; wie ze in een visual optelt of middelt krijgt een fout getal. De metingen (stap 6) rekenen ze juist uit de minuten en tonnen.

**Stilstand**
- Verwijder `datum`, `installation_code`, `reason_code`, `categorie`, `omschrijving` (die komen uit de dimensies) en `gem_minuten`, `max_minuten` (een gemiddelde van gemiddelden is fout; zie metingen).

**Datum**
- Beperk de kalender tot de periode met data, zodat slicers niet tot 2030 lopen: klik op *fx* naast de formulebalk. Power BI maakt een nieuwe stap met als formule `= <naam van de vorige stap>` (bv. `= dwh_dim_date`). Maak daarvan (startdatum = je eerste dag met data: `SELECT min(datum) FROM dwh.vw_daily_oee;` — met `--historiek 365` ongeveer een jaar terug):
  ```
  = Table.SelectRows(dwh_dim_date, each [datum] >= #date(2025,1,1) and [datum] <= Date.From(DateTime.LocalNow()))
  ```
  Bij elke vernieuwing schuift de einddatum mee.
- Controleer de types: `datum` = **Datum**, `date_key` = **Geheel getal**.

**Algemeen**
- Sleutels (`*_key`) moeten aan beide kanten hetzelfde type hebben (Geheel getal). Power BI zet `integer`/`smallint` daar automatisch op; `numeric` wordt **Decimaal getal**. Laat dat zo.

**Sluiten en toepassen.**

### 4. Relaties (Modelweergave)

Verwijder eerst alles wat Power BI eventueel toch zelf aanlegde. Leg dan aan, telkens van dimensie (1) naar feit (*), kruisfilterrichting **Enkel**:

| Van (1) | Naar (*) | Kolom |
| --- | --- | --- |
| Datum | OEE | `date_key` |
| Datum | Stilstand | `date_key` |
| Ploeg | OEE | `shift_key` |
| Ploeg | Stilstand | `shift_key` |
| Installatie | OEE | `installation_key` |
| Installatie | Stilstand | `installation_key` |
| Stilstandsoorzaak | Stilstand | `reason_key` |

Geen relatie tussen **OEE** en **Stilstand**: ze delen hun dimensies, en dat volstaat om ze samen te filteren. Geen bidirectionele filters: die maken het model dubbelzinnig en traag, en zijn hier nergens nodig.

Netjes maken (rechtsklik → *Verbergen in rapportweergave*):

- alle `*_key`-kolommen in **OEE** en **Stilstand** (filteren gebeurt via de dimensies);
- `load_id` / technische kolommen als die er zijn;
- in de dimensies de `*_key`-kolommen.

Sorteren (kolom selecteren → *Kolomhulpmiddelen → Sorteren op kolom*):

| Kolom | Sorteren op |
| --- | --- |
| Datum[`maand_naam`] | `maand` |
| Datum[`dag_naam`] | `dag_van_week` |
| Ploeg[`ploeg`] en [`shift_code`] | `shift_key` (vroeg → laat → nacht) |
| Installatie[`naam`] en [`installation_code`] | `installation_key` (WW1 → BL1 → KW1 → GO1, de volgorde van de lijn) |

### 5. Datumtabel

**Datum** selecteren → *Tabelhulpmiddelen → Markeren als datumtabel* → kolom `datum`.
Power BI controleert of de waarden uniek en aaneensluitend zijn (dat zijn ze: 1 rij per dag). De relaties lopen op `date_key` (geheel getal); dat mag, zolang de tabel gemarkeerd is werken tijdfuncties zoals `DATESYTD` en `SAMEPERIODLASTYEAR` gewoon.

### 6. Metingen

Maak een lege tabel **_Metingen** (*Start → Gegevens invoeren → Laden*) en zet daar alle metingen in. Verwijder daarna de lege kolom; de tabel springt dan bovenaan de veldenlijst.

Kopieer de metingen uit [`measures.dax`](measures.dax). De regel: **eerst minuten/tonnen optellen, dan delen** — nooit percentages middelen.

Formaat: `OEE`, `Beschikbaarheid`, `Prestatie`, `Kwaliteit`, `Uitval %` → *Percentage, 1 decimaal*; minuten en tonnen → *Geheel getal* met scheidingsteken.

### 7. Controle tegen de database

Zet een tabel-visual met `Datum[datum]`, `Installatie[installation_code]`, `[OEE]`, `[Beschikbaarheid]`, `[Stilstand ongepland (min)]` en vergelijk één dag met DBeaver:

```sql
SELECT datum, installation_code, oee, beschikbaarheid, ongepland_min
FROM dwh.vw_daily_oee
WHERE datum = current_date - 1
ORDER BY installation_code;
```

Op dag × installatie moeten de getallen exact gelijk zijn. Over een hele maand mag `[OEE]` licht afwijken van een gemiddelde van de dag-OEE's — dat is net de bedoeling (gewogen naar geplande tijd).

### 8. Opslaan in Git

**Bestand → Opslaan als → Power BI-projectbestanden (`*.pbip`)** → `data-platform-lab\powerbi\SteelLine.pbip`.
Dat maakt `SteelLine.Report/` en `SteelLine.SemanticModel/` (tekstbestanden: TMDL/JSON), die je in Git kan vergelijken. Er zit **geen data en geen wachtwoord** in; die staan in `.pbi/cache.abf` en de Windows-referentieopslag, en worden genegeerd via [`.gitignore`](.gitignore).

```powershell
cd $env:USERPROFILE\Desktop\data-platform-lab
git add powerbi
git commit -m "Fase3: Power BI-model met sterstructuur, datumtabel en OEE-metingen"
git push
```

Lukt *.pbip* niet in jouw versie: **Bestand → Opties → Preview-functies → Power BI-projectopslag** aanvinken en Power BI herstarten.

---

## Week 9 — DAX-metingen

**Voorwaarde:** week 8 staat (model met relaties, `Datum` gemarkeerd, de metingen van week 8 in **_Metingen**).

### Status

- [x] Productie, doorvoer en uitval %
- [x] Stilstand, % ongepland, MTBF / MTTR (nieuwe definitie)
- [x] OEE
- [x] Tijdvergelijkingen en voortschrijdend gemiddelde
- [x] Gecontroleerd met [`controle_week9.sql`](controle_week9.sql)
- [x] Metingen staan in `steelline.SemanticModel/definition/tables/_Metingen.tmdl` (44, met mappen en opmaak)

### 1. Metingen toevoegen

Alle metingen staan in [`measures.dax`](measures.dax): bovenaan die van week 8, onderaan het blok **WEEK 9**.
Snelste manier: **DAX-queryweergave** → nieuw tabblad → plak

```dax
DEFINE
    MEASURE _Metingen[Productie (t)] = [Ton]
    MEASURE _Metingen[Draaiuren (u)] = DIVIDE ( [Looptijd (min)], 60 )
    -- ... (zet "MEASURE _Metingen[" voor elke naam uit measures.dax)
EVALUATE {1}
```

en klik op **Model bijwerken met wijzigingen**. Of één voor één: **_Metingen → Nieuwe meting**.

| Map (*Weergavemap*) | Metingen | Formaat |
| --- | --- | --- |
| Productie | Productie (t), Draaiuren (u), Doorvoer (t/u) | geheel getal / 1 decimaal |
| Kwaliteit | Uitval % (week 8) | percentage, 2 decimalen |
| OEE | OEE, Beschikbaarheid, Prestatie, Kwaliteit (week 8) | percentage, 1 decimaal |
| Betrouwbaarheid | Storingen, Storingsduur (min), MTTR (min), MTBF (min) | geheel getal |
| Stilstand | Stilstand (min), Stilstand ongepland (min), Stilstand ongepland oorzaak (min), % ongepland | geheel getal / percentage |
| Vergelijking | … vorige week, … vorige maand, … verschil (pp), … % verschil, OEE vorig jaar | zoals de basismeting; pp en % verschil met teken (`+0.0%;-0.0%;0.0%`) |
| Trend | OEE 7d, Uitval % 7d, Productie 7d gem. (t/dag), Stilstand ongepland 7d gem. (min/dag) | zoals de basismeting |
| Basis (verbergen) | Ton, Rollen, Looptijd (min), … | — |

### 2. Wat er veranderd is tegenover week 8

**MTBF en MTTR** gaan nu alleen over echte storingen: mechanisch en elektrisch. In week 8 telde elke ongeplande stop mee, ook *wachten op materiaal*. De gloeioven kreeg daardoor een MTTR van bijna 5 uur, terwijl hij gewoon op rollen wachtte. Nu (testdata, één maand):

| Installatie | MTTR oud | MTTR nieuw | MTBF nieuw |
| --- | --- | --- | --- |
| WW1 | 36 min | 40 min | 13 u |
| BL1 | 93 min | 40 min | 24 u |
| KW1 | 117 min | 49 min | 18 u |
| GO1 | 289 min | 11 min | 125 u |

Had je de metingen van week 8 al geplakt: vervang de formules van **Storingen**, **MTTR (min)** en **MTBF (min)** door die uit `measures.dax`, en voeg **Storingsduur (min)** toe. Visuals blijven werken, want de namen zijn dezelfde.

### 3. De keuzes achter de metingen

- **Doorvoer = ton ÷ draaiuren**, en draaiuren = looptijd: de tijd dat een installatie gepland was en niet stillag. Zo meet doorvoer hoe snel er gewerkt wordt wanneer er gewerkt wordt.
- **% ongepland** komt uit de tabel *Stilstand* en reageert dus op een slicer op oorzaak of categorie.
- **Procentpunten, geen procenten.** OEE van 60 % naar 66 % is **+6 pp**, niet +10 %. Voor percentages staan er `… verschil (pp)`-metingen, voor tonnen en minuten `… % verschil`.
- **DATEADD** verschuift de geselecteerde periode: met maand november in de slicer vergelijkt hij met oktober, met 1–16 november met 1–16 oktober. Daarvoor moet `Datum` gemarkeerd zijn (week 8, stap 5).
- **OEE 7d is gewogen:** de minuten en tonnen van 7 dagen samen, dan delen. Een gemiddelde van 7 dag-OEE's zou een zondag met één ploeg even zwaar laten wegen als een volle werkdag.
- De **7d-metingen zijn leeg op dagen zonder data**, zodat een trendlijn niet tot vandaag doorzakt naar nul.
- **Vorig jaar** (`SAMEPERIODLASTYEAR`) geeft pas iets vanaf 13 maanden historiek.

### 4. Controleren

Voer [`controle_week9.sql`](controle_week9.sql) uit (DBeaver als `bi_reader`, of op de server):

```bash
docker exec -i postgres psql -U bi_reader -d steelline -f - < powerbi/controle_week9.sql
```

Elke query zegt welke visual en welke slicer je in Power BI moet zetten. De getallen moeten gelijk zijn, op afronding na:

1. vorige maand per installatie: productie, draaiuren, doorvoer, uitval %, storingen, MTBF, MTTR, OEE;
2. % ongepland per installatie;
3. deze maand tot gisteren tegenover dezelfde dagen vorige maand;
4. OEE 7d voor de laatste 10 dagen.

### 5. Opslaan

```powershell
cd $env:USERPROFILE\Desktop\data-platform-lab
git add powerbi
git commit -m "Fase3: DAX-metingen week 9 in het model"
git push
```

---

## Week 10 — rapport

Het rapport staat in PBIR-formaat (`steelline.Report/definition/pages/`): elke pagina en elke visual is een eigen JSON-bestand.
Het model kreeg er twee tabellen bij: **Productie** (`dwh.fact_production`) en **Staalsoort** (`dwh.dim_steel_grade`), met relaties naar Datum, Ploeg, Installatie en Staalsoort.

### Status

- [x] Pagina 1 Overzicht: KPI-kaarten (OEE, productie, uitval %, ongeplande stilstand) met verschil tegenover vorige maand, OEE-trend per dag met 7-daags gemiddelde, OEE per installatie
- [x] Pagina 2 Stilstand: Pareto per oorzaak, per installatie en categorie, drill-down jaar → maand → dag, MTBF/MTTR per installatie
- [x] Pagina 3 Kwaliteit: uitval per ploeg en staalsoort, defecten per soort, spreiding dikte tegenover doel, tabel per staalsoort
- [x] Pagina 4 Details: drill-through op installatie (rechtsklik → *Drill through* → *Details*), terugknop
- [x] Slicers datum, ploeg, installatie, gesynchroniseerd over de pagina's; uitleg in subtitels en het ⓘ-icoon; vaste kleur per installatie
- [ ] Nagekeken in Power BI Desktop en bijgewerkt waar nodig
- [ ] Optioneel: rijbeveiliging (RLS) per ploeg
- [ ] Screenshots in `powerbi/screenshots/` + schermopname van 1–2 min

### Eerste keer openen

1. Open `steelline.pbip` → **Refresh** (laadt Productie en Staalsoort voor het eerst).
2. Overloop de vier pagina's. Verschuiven, vergroten en opmaak aanpassen mag gewoon; bewaar daarna.

### Kleuren

| Installatie | Kleur |
| --- | --- |
| WW1 warmwals | `#118DFF` blauw |
| BL1 beitslijn | `#12239E` donkerblauw |
| KW1 koudwals | `#E66C37` oranje |
| GO1 gloeioven | `#6B007B` paars |

Stilstand en defecten staan in oranje, uitval in donkerblauw.

### De vraag uit het plan

*"Welke installatie had vorige maand de meeste ongeplande stilstand, en waarom?"* → pagina **Stilstand**, datum-slicer op vorige maand: de staafgrafiek rechtsboven toont de installatie, de Pareto de oorzaak. Rechtsklik op die installatie → **Drill through → Details** voor het verloop.
