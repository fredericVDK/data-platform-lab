# Power BI — SteelLine-rapport

Doel (fase 3): een rapport van 3–4 pagina's waarmee een productiemanager in 30 seconden ziet waar het misloopt.
Deze map bevat het rapport als **Power BI-project** (`.pbip`), zodat het in Git leesbaar en vergelijkbaar is.

## Week 8 — verbinding en model

### Status

- [ ] `sql/005_reporting_views.sql` opnieuw uitgevoerd op de server (stilstand-view heeft nu `shift_key`)
- [ ] Verbinding vanuit Power BI Desktop via Tailscale met `bi_reader`
- [ ] 4 dimensies + 2 views geïmporteerd en opgeschoond in Power Query
- [ ] Relaties: sterstructuur, één-op-veel, enkelvoudige filterrichting
- [ ] `Datum` gemarkeerd als datumtabel, automatische datum/tijd uit
- [ ] Basismetingen (OEE, stilstand) toegevoegd en gecontroleerd tegen SQL
- [ ] Opgeslagen als `powerbi/SteelLine.pbip` en gecommit

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
| Installatie[`naam`] | `volgorde_in_lijn` (eerst op `lijn` in de visual) |

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

## Volgende weken (overzicht)

- **Week 9** — pagina's: *Overzicht* (OEE-kaarten + trend per installatie), *Stilstand* (Pareto per oorzaak, MTTR/MTBF), *Kwaliteit* (`fact_production` + `dim_steel_grade`), *Ploegen*.
- **Week 10** — afwerking: thema, tooltips, drill-through naar dag × installatie, screenshots in deze map, publiceren of als PDF in de README.
