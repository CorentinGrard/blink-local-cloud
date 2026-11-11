# Pinned to a Debian release: ffmpeg comes from Debian, and an unannounced base
# move (bookworm -> trixie, ffmpeg 5.1 -> 7.1) once added ~4.4 s of input
# probing to two-way talk. Bump this tag on purpose, and let CI's in-image
# tests check the new ffmpeg.
FROM python:3.12-slim-trixie
# ffmpeg: remux motion clips .ts -> .mp4 (no re-encode), and encode two-way talk
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app/tools
COPY tools/ /app/tools/
COPY certs/ /app/certs/
ENV BLINK_PORT=8080
EXPOSE 8080 8090
CMD ["python3", "local_server.py"]
