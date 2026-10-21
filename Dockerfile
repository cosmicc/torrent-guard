FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
# ffprobe validates actual decodable video stream metadata, not merely extensions.
RUN apk add --no-cache ffmpeg
WORKDIR /app
RUN mkdir -p /state && chown 65534:65534 /state
COPY --chown=65534:65534 guard.py /app/guard.py
USER 65534:65534
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "/app/guard.py", "--healthcheck"]
CMD ["python", "-u", "/app/guard.py"]
