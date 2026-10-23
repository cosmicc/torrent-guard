FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
# ffprobe validates actual decodable video stream metadata, not merely extensions.
RUN apk add --no-cache ffmpeg
WORKDIR /app
RUN mkdir -p /state && chown 1000:1000 /state
COPY --chown=1000:1000 guard.py /app/guard.py
COPY --chown=1000:1000 deletion.py /app/deletion.py
COPY --chown=1000:1000 scripts/watch_logs.py /app/watch_logs.py
USER 1000:1000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "/app/guard.py", "--healthcheck"]
CMD ["python", "-u", "/app/guard.py"]
