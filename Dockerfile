FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/New_York
# ffprobe validates actual decodable video stream metadata, not merely extensions.
RUN apk add --no-cache ffmpeg tzdata
WORKDIR /app
RUN mkdir -p /state && chown 1000:1000 /state
COPY --chown=1000:1000 guard.py /app/guard.py
COPY --chown=1000:1000 deletion.py /app/deletion.py
COPY --chown=1000:1000 orphans.py /app/orphans.py
COPY --chown=1000:1000 records.py /app/records.py
COPY --chown=1000:1000 status_report.py /app/status_report.py
COPY --chown=1000:1000 status_web.py /app/status_web.py
COPY --chown=1000:1000 web /app/web
COPY --chown=1000:1000 scripts/watch_logs.py /app/watch_logs.py
COPY --chown=1000:1000 scripts/watch_logs.py /app/scripts/watch_logs.py
USER 1000:1000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "/app/guard.py", "--healthcheck"]
CMD ["python", "-u", "/app/guard.py"]
