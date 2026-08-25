FROM python:3.12-alpine

# openssh-client is NOT required (paramiko speaks SSH natively), but tar/gzip
# are used for local archive handling.
RUN apk add --no-cache tzdata tar gzip

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/app/

# /config  -> settings.json, state.db, cached valetudo binaries
# /backups -> timestamped backup archives
VOLUME ["/config", "/backups"]

ENV VR_CONFIG_DIR=/config \
    VR_BACKUP_DIR=/backups \
    VR_PORT=8080 \
    TZ=UTC

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import httpx,os;httpx.get(f'http://127.0.0.1:{os.environ.get(\"VR_PORT\",8080)}/healthz',timeout=4).raise_for_status()"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
