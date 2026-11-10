FROM python:3.12-slim
# ffmpeg: remux motion clips .ts -> .mp4 (no re-encode) so phones can play them
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app/tools
COPY tools/ /app/tools/
COPY certs/ /app/certs/
ENV BLINK_PORT=8080
EXPOSE 8080 8090
CMD ["python3", "local_server.py"]
