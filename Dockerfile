FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
# ffprobe validates actual decodable video stream metadata, not merely extensions.
RUN apk add --no-cache ffmpeg
WORKDIR /app
RUN mkdir -p /state && chown 65534:65534 /state
COPY --chown=65534:65534 guard.py /app/guard.py
USER 65534:65534
CMD ["python", "-u", "/app/guard.py"]
