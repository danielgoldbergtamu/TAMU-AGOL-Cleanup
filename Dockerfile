# One image for both parts: the web app (default command) and the daily job (command: python pipeline_daily.py).
# Holds code only: the database, reports, built dashboards, the private lookup tables and every secret are given
# to the container at run time (volumes and environment), never built in.
FROM python:3.12-slim

# Run dates and report dates are US Central, as on the workstation (times are recorded in US Central).
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AGOL_DUCKDB=/data/agol.duckdb AGOL_REPORTS_DIR=/data/reports AGOL_OUTPUT_DIR=/data/out \
    AGOL_ENTRA_INBOX=/data/entra-inbox AGOL_CONFIG_DIR=/config \
    TZ=America/Chicago

RUN apt-get update && apt-get install -y --no-install-recommends libkrb5-3 tzdata && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-web.txt ./
RUN pip install -r requirements.txt -r requirements-web.txt

COPY agol_db.py pipeline_daily.py TAMU_AGOL_*.py ./
COPY dashboard/ dashboard/
COPY tools/ tools/
COPY web/ web/

RUN useradd --uid 10001 --create-home agol && mkdir -p /data /config && chown agol /data
USER 10001
EXPOSE 8000
CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
