FROM python:3.12-alpine

LABEL org.opencontainers.image.title="valetudo-restore" \
      org.opencontainers.image.description="Backup, monitoring and automatic restore for Valetudo on Dreame/Mova robots" \
      org.opencontainers.image.source="https://github.com/adman234/valetudo-restore" \
      org.opencontainers.image.licenses="MIT"

# paramiko speaks SSH natively, so no openssh-client is needed.
RUN apk add --no-cache tzdata

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/app/

# /config  -> settings.json, state.db, ssh key, cached valetudo binary
# /backups -> timestamped backup archives
VOLUME ["/config", "/backups"]

ENV VR_CONFIG_DIR=/config \
    VR_BACKUP_DIR=/backups \
    VR_PORT=8080 \
    VR_LOG_LEVEL=INFO \
    TZ=UTC

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('VR_PORT','8080')+'/healthz',timeout=4)"

# sh -c so VR_PORT is expanded at runtime rather than baked in.
CMD ["sh", "-c", "exec python -m uvicorn app.main:app --host 0.0.0.0 --port ${VR_PORT:-8080}"]
