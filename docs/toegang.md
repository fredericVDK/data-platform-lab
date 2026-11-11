# Toegang, monitoring en back-ups

Server `mordor` (Ubuntu Server 24.04, Docker). Geen enkele poort staat open in de router.

## Toegang via Tailscale

Alle containers publiceren hun poort enkel op `127.0.0.1` (Docker-poorten omzeilen ufw).
Tailscale stelt ze beschikbaar aan de eigen toestellen in de tailnet met `tailscale serve`:

```bash
sudo tailscale serve --bg --tcp 5432 tcp://127.0.0.1:5432   # PostgreSQL
sudo tailscale serve --bg --tcp 3001 tcp://127.0.0.1:3001   # Uptime Kuma
sudo tailscale serve --bg --tcp 3000 tcp://127.0.0.1:3000   # Grafana (optioneel)
tailscale serve status
```

Waarom niet de Tailscale-IP in `docker-compose.yml` binden? Bij het opstarten van de server
is Docker soms sneller dan Tailscale; dan bestaat het adres `100.x.y.z` nog niet en start
PostgreSQL niet. `tailscale serve` heeft dat probleem niet en blijft actief na een herstart.

| Dienst | Adres (vanaf pc/gsm met Tailscale) |
| --- | --- |
| SSH | `ssh andre@mordor` |
| PostgreSQL | `mordor:5432` (DBeaver, Power BI) |
| Uptime Kuma | `http://mordor:3001` |
| Grafana | `http://mordor:3000` |

## Monitoring

- **Uptime Kuma**: PostgreSQL-check (gebruiker `bi_reader`, `SELECT 1`), push-monitor voor de
  nachtelijke back-up, later HTTP-check op de webapp en push-monitor voor de ETL.
  Meldingen via Telegram.
- **Grafana + Prometheus + node-exporter** (profiel `monitoring`): CPU, RAM, schijf.
  `docker compose --profile monitoring up -d`

## Back-ups

`infra/backup.sh` draait elke nacht om 03:15 (na de ETL): `pg_dump` + rollen, 7 dagen lokaal,
kopie naar tweede schijf en/of cloud (rclone), status naar Uptime Kuma.
Terugzetten: [restore.md](restore.md).
