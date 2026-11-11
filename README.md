# data-platform-lab

Een end-to-end dataplatform voor een gesimuleerde staalfabriek ("SteelLine"), volledig zelf gebouwd en gehost op een eigen Linux-server.
Elke nacht wordt er realistische productie- en stilstanddata gegenereerd, gevalideerd en in een PostgreSQL-sterschema geladen. Daarop draaien een Power BI-dashboard, een C#-webapp voor operators en een AI-laag (anomaliedetectie en een vraag-je-data-chatbot).

> Work in progress. Dit project wordt in fases opgebouwd (zie [Roadmap](#roadmap)).

## Geplande architectuur

```mermaid
flowchart LR
    subgraph Homelab["Homelab: Ubuntu Server 24.04 + Docker"]
        GEN["Datagenerator<br/>(Python)"] -->|CSV / Parquet| LAND[(landing/)]
        LAND --> ETL["ETL-pipeline<br/>(Python, cron 02:00)"]
        ETL -->|staging → sterschema| DB[("PostgreSQL 16")]
        APP["Operator-webapp<br/>(ASP.NET Core)"] -->|stilstand melden| DB
        AI["AI<br/>anomaliedetectie + text-to-SQL"] <-->|read-only| DB
        MON["Uptime Kuma<br/>monitoring"] -.-> DB
        MON -.-> ETL
        MON -.-> APP
        BAK["Nachtelijke back-up<br/>(pg_dump + rclone)"] -.-> DB
    end
    PBI["Power BI Desktop<br/>(Windows-pc)"] -->|Tailscale, bi_reader| DB
    USER["Operator / gsm / tablet"] -->|Tailscale| APP
```

Alle toegang van buitenaf loopt via **Tailscale**; de database staat niet open naar het internet.

## Tech stack

| Laag | Technologie |
| --- | --- |
| Server | Ubuntu Server 24.04 LTS, Docker Compose, Tailscale, ufw |
| Database | PostgreSQL 16 (sterschema + staging-schema) |
| Data & ETL | Python 3.12, pandas, numpy, SQLAlchemy, psycopg, pytest |
| Rapportering | Power BI (DAX, OEE, MTBF/MTTR) |
| Webapp | C# / ASP.NET Core Razor Pages, EF Core (Npgsql), xUnit |
| AI | scikit-learn (IsolationForest), LLM-API (text-to-SQL), FastAPI |
| Operations | Uptime Kuma, cron / systemd-timers, pg_dump-back-ups |

## Mappenstructuur

```
data-platform-lab/
  infra/        docker-compose, back-upscripts, cron
  generator/    Python: fake fabrieksdata
  etl/          Python: laden in de database
  sql/          schema, views, migraties
  powerbi/      .pbix + screenshots
  webapp/       ASP.NET Core-app
  ai/           chatbot + anomaliedetectie
  docs/         architectuur, beslissingen
```

## Roadmap

- [ ] **Fase 0** – Voorbereiding: repo, werkomgeving, README
- [ ] **Fase 1** – Homelab: Linux-server, Docker, PostgreSQL, monitoring, back-ups
- [ ] **Fase 2** – Datagenerator, sterschema en nachtelijke ETL
- [ ] **Fase 3** – Power BI-dashboard (OEE, stilstand, kwaliteit)
- [ ] **Fase 4** – C#-webapp voor stilstandregistratie
- [ ] **Fase 5** – AI: anomaliedetectie en text-to-SQL-chatbot
- [ ] **Fase 6** – Afwerking, demovideo en documentatie
