# Nachtelijke pipeline

Elke ochtend om **06:30** start een systemd-timer de container `pipeline`:

```
systemd-timer 06:30 ──▶ docker compose run --rm --build pipeline
                          ├─ generator: laatste 7 volledige dagen aanvullen (al gearchiveerde dagen overslaan)
                          └─ ETL: landing/ → staging → dwh, run in staging.etl_run_log, push naar Uptime Kuma
```

**Waarom 06:30 en niet 02:00?** Productiedag D eindigt pas om D+1 06:00 (einde nachtploeg).
Om 06:30 is gisteren volledig, en staat de data er als je 's ochtends Power BI opent.

**Waarom een systemd-timer en geen cron in de container?**
- `Persistent=true`: stond de server uit om 06:30, dan draait de run meteen na het opstarten.
- De container bestaat enkel tijdens de run (`--rm`), er draait niets permanent.
- Uitvoer komt in `journalctl`, mislukte runs in `systemctl --failed`.
- Inhalen gebeurt vanzelf: na 3 dagen zonder stroom maakt de generator de 3 gemiste dagen.

## Installeren (eenmalig, op de server)

```bash
cd ~/data-platform-lab && git pull

# 1. database: views voor Power BI
docker exec -i postgres psql -U admin -d steelline -f - < sql/005_reporting_views.sql

# 2. mappen voor de data, van jouw gebruiker (anders maakt Docker ze als root)
mkdir -p landing labels archive rejected logs
id -u; id -g                       # meestal 1000 / 1000; anders PIPELINE_UID/GID in infra/.env zetten

# 3. image bouwen en één keer met de hand draaien
cd infra
docker compose --profile jobs run --rm --build pipeline
echo "exit $?"                     # 0 = ok

# 4. timer installeren
sudo cp systemd/steelline-pipeline.service systemd/steelline-pipeline.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now steelline-pipeline.timer
systemctl list-timers steelline-pipeline.timer     # NEXT = morgen 06:30
```

Je gebruiker moet in de groep `docker` zitten (`groups` toont `docker`); zo niet:
`sudo usermod -aG docker $USER` en opnieuw inloggen. Heet je gebruiker niet `andre` of staat de
repo elders, pas dan `User=` en `WorkingDirectory=` aan in de `.service`.

**Data die je al met de hand laadde** (venv, week 5–6) blijft gewoon staan: de generator slaat dagen
in `archive/` over en de ETL is idempotent.

## Uptime Kuma

1. *Add New Monitor* → type **Push**, naam `ETL nachtelijke pipeline`,
   heartbeat interval **90000** s (25 u), retries 0.
2. Kopieer de push-URL **zonder** `?status=up&msg=OK&ping=` en vervang de host door de containernaam:
   `http://uptime-kuma:3001/api/push/<token>` (de pipeline draait in hetzelfde Docker-netwerk).
3. In `infra/.env`: `ETL_KUMA_PUSH_URL=http://uptime-kuma:3001/api/push/<token>`

| Situatie | Wat Kuma doet |
| --- | --- |
| run geslaagd of deels (rijen afgekeurd) | push `up` met de tellingen in het bericht |
| een bestand mislukt | push `down` → Telegram-melding meteen |
| pipeline draaide niet (server uit, Docker kapot, generator crasht) | geen push → melding na 25 u |

## Opvolgen

```bash
systemctl list-timers steelline-pipeline.timer
systemctl status steelline-pipeline.service       # laatste run
journalctl -u steelline-pipeline.service -n 50    # uitvoer
tail -f ~/data-platform-lab/logs/etl.log
sudo systemctl start steelline-pipeline.service   # nu meteen draaien
```

```sql
SELECT * FROM dwh.vw_etl_status ORDER BY run_id DESC LIMIT 5;   -- ook als bi_reader
```

## Back-up na de pipeline

De back-up (`infra/backup.sh`) draaide om 03:15. Zet hem **na** de pipeline, zodat elke back-up de
data van gisteren bevat (`crontab -e`):

```
15 7 * * * /home/andre/data-platform-lab/infra/backup.sh >> /home/andre/backups/backup.log 2>&1
```

## Views voor Power BI (`sql/005_reporting_views.sql`)

| View | Grain | Inhoud |
| --- | --- | --- |
| `dwh.vw_oee_shift` | dag × ploeg × installatie | bemande, geplande, loop- en ideale minuten; rollen, tonnen; beschikbaarheid, prestatie, kwaliteit, OEE |
| `dwh.vw_daily_oee` | dag × installatie | idem, opgeteld per dag + datum, installatienaam, uitval % |
| `dwh.vw_downtime_by_reason` | dag × installatie × oorzaak × gepland | aantal, minuten, gemiddelde en langste stop |
| `dwh.vw_etl_status` | run | status en tellingen van de ETL |

OEE = beschikbaarheid × prestatie × kwaliteit:

- **beschikbaarheid** = looptijd / geplande productietijd (geplande tijd = ploeg − gepland onderhoud;
  looptijd = geplande tijd − storingen, wissels en wachten)
- **prestatie** = ideale tijd / looptijd (ideale tijd = ton / capaciteit van de installatie)
- **kwaliteit** = ton zonder defect / ton

Tel in Power BI altijd de **minuten en tonnen** op en deel daarna. Een gemiddelde van OEE-percentages
over dagen of installaties geeft een verkeerd resultaat.
