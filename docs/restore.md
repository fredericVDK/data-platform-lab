# Restore — PostgreSQL `steelline`

> Een back-up die je nooit hebt teruggezet, telt niet.
> Doe minstens één keer per maand een restore-test en vul het [logboek](#logboek-restore-tests) aan.

## Wat staat er in een back-up?

`infra/backup.sh` maakt elke nacht drie bestanden in `~/backups/postgres/` (map `700`, bestanden `600`):

| Bestand | Inhoud | Gemaakt met |
| --- | --- | --- |
| `steelline_JJJJ-MM-DD_UUMM.dump` | de hele database: schema's, tabellen, data, rechten | `pg_dump -Fc` |
| `globals_JJJJ-MM-DD_UUMM.sql` | rollen (`admin`, `etl_writer`, `bi_reader`) met wachtwoord-hash en instellingen | `pg_dumpall --globals-only` |
| `SHA256SUMS_JJJJ-MM-DD_UUMM` | checksums van beide bestanden | `sha256sum` |

Rollen zitten **niet** in een `pg_dump`: zonder het globals-bestand faalt de restore op
"role etl_writer does not exist".

Kopieën: tweede schijf (`BACKUP_COPY_DIR`, 30 dagen) en/of cloud (`RCLONE_REMOTE`, 30 dagen).
Log: `~/backups/backup.log`. Bij een mislukte of ontbrekende back-up stuurt Uptime Kuma een melding.

---

## A. Restore-test (maandelijks, raakt productie niet)

### Automatisch

```bash
~/data-platform-lab/infra/restore-test.sh                 # nieuwste back-up
~/data-platform-lab/infra/restore-test.sh ~/backups/postgres/steelline_2026-11-12_0315.dump
```

Het script controleert de checksums, start een wegwerp-container `pg_restore_test`, zet rollen
en database terug, vergelijkt schema's, rollen en het aantal rijen per tabel met productie,
en ruimt de container weer op. Onderaan staat een regel voor het logboek.

### Handmatig (om te begrijpen wat het script doet)

```bash
cd ~/backups/postgres
ls -lt | head                                   # kies een back-up
sha256sum -c SHA256SUMS_2026-11-12_0315         # 2x "OK"

# lege PostgreSQL 16 zonder volume (verdwijnt met --rm)
docker run -d --rm --name pg_restore_test -e POSTGRES_USER=admin -e POSTGRES_PASSWORD=test postgres:16
docker exec pg_restore_test pg_isready -h 127.0.0.1    # herhaal tot "accepting connections"

# 1. rollen (fout 'role "admin" already exists' is normaal)
docker exec -i pg_restore_test psql -U admin -d postgres < globals_2026-11-12_0315.sql

# 2. database: --create maakt steelline zelf aan, mét de database-rechten
docker exec -i pg_restore_test pg_restore -U admin -d postgres --create --exit-on-error \
  < steelline_2026-11-12_0315.dump

# 3. controleren
docker exec -it pg_restore_test psql -U admin -d steelline -c '\dn' -c '\du' -c '\dt dwh.*'
docker exec -it pg_restore_test psql -U admin -d steelline -c 'SELECT count(*) FROM dwh.<tabel>;'

# 4. opruimen
docker stop pg_restore_test
```

Tip voor de eerste test, zolang er nog geen echte data is: maak een markeertabel vóór de back-up
en controleer na de restore of de rij er is.

```bash
docker exec -i postgres psql -U etl_writer -d steelline \
  -c "CREATE TABLE dwh.restore_marker (gemaakt timestamptz DEFAULT now(), tekst text);" \
  -c "INSERT INTO dwh.restore_marker (tekst) VALUES ('back-up test');"
~/data-platform-lab/infra/backup.sh && ~/data-platform-lab/infra/restore-test.sh
# daarna: DROP TABLE dwh.restore_marker;
```

---

## B. Echte restore op de server (database kapot of foute data geladen)

1. **Schrijvers stoppen**: pipeline-timer uitschakelen (`sudo systemctl stop steelline-pipeline.timer`), webapp stoppen (`docker compose stop webapp`).
2. **Huidige toestand bewaren**, ook al is ze kapot: `~/data-platform-lab/infra/backup.sh`
3. **Database vervangen** (rollen bestaan al in de cluster):

   ```bash
   cd ~/backups/postgres
   B=2026-11-12_0315                                   # gekozen back-up
   sha256sum -c SHA256SUMS_$B
   docker exec postgres psql -U admin -d postgres -c 'DROP DATABASE steelline WITH (FORCE);'
   docker exec -i postgres pg_restore -U admin -d postgres --create --exit-on-error < steelline_$B.dump
   ```

4. **Controleren**: `docker exec -i postgres psql -U admin -d steelline < ~/data-platform-lab/sql/check_roles.sql`
   en het aantal rijen in de belangrijkste tabellen.
5. **Schrijvers terug aanzetten** en in Uptime Kuma nagaan dat alles groen is.

Eén tabel terugzetten in plaats van de hele database:

```bash
docker exec -i postgres pg_restore -U admin -d steelline --clean --if-exists \
  -t <tabel> -n dwh < steelline_$B.dump
```

---

## C. Server of volume kwijt (nieuwe installatie)

1. Server opnieuw opzetten volgens de installatiegids (Ubuntu, SSH, Docker, Tailscale).
2. Repo clonen en **`infra/.env` terugzetten uit de wachtwoordmanager** (staat niet in Git).
3. Back-ups ophalen:

   ```bash
   mkdir -p ~/backups/postgres && chmod 700 ~/backups/postgres
   rclone copy <remote>:steelline-backups ~/backups/postgres     # of kopiëren van de tweede schijf
   ```

4. `cd ~/data-platform-lab/infra && docker compose up -d` — de lege volume draait `initdb`:
   rollen en lege schema's worden opnieuw aangemaakt.
5. Rollen gelijkzetten en database vervangen:

   ```bash
   cd ~/backups/postgres && B=<nieuwste>
   docker exec -i postgres psql -U admin -d postgres < globals_$B.sql     # "already exists" is normaal
   docker exec postgres psql -U admin -d postgres -c 'DROP DATABASE steelline WITH (FORCE);'
   docker exec -i postgres pg_restore -U admin -d postgres --create --exit-on-error < steelline_$B.dump
   ```

6. Cron (`backup.sh`) en `tailscale serve` opnieuw instellen.

---

## Logboek restore-tests

| Datum | Back-up | Duur | Resultaat | Opmerking |
| --- | --- | --- | --- | --- |
| | | | | |
