# Nachtelijke pipeline: datagenerator + ETL in één image.
# Gebouwd vanuit de root van de repo (zie infra/docker-compose.yml, service "pipeline").
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Brussels

# zelfde UID als je gebruiker op de server (meestal 1000), zodat bestanden in
# landing/, archive/, logs/ ... van jou zijn en niet van root
ARG UID=1000
ARG GID=1000
RUN groupadd -g "${GID}" pipeline && useradd -u "${UID}" -g pipeline -m -s /usr/sbin/nologin pipeline

WORKDIR /app

# eerst enkel de requirements: deze laag blijft in de cache zolang ze niet wijzigen
COPY generator/requirements.txt generator-requirements.txt
COPY etl/requirements.txt etl-requirements.txt
RUN pip install -r generator-requirements.txt -r etl-requirements.txt

COPY generator/ generator/
COPY etl/ etl/
COPY infra/pipeline/nightly.sh nightly.sh
RUN chmod 755 nightly.sh

ENV LANDING_DIR=/data/landing \
    LABELS_DIR=/data/labels \
    ARCHIVE_DIR=/data/archive \
    REJECTED_DIR=/data/rejected \
    ETL_LOG_DIR=/data/logs

USER pipeline
ENTRYPOINT ["/app/nightly.sh"]
