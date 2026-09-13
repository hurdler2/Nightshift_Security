FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    EDGE_DATA_DIR=/var/lib/nightshift-edge

WORKDIR /app

# ffmpeg/ffprobe are required for RTSP verification, clip building and live sessions.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY nightshift_edge ./nightshift_edge
RUN pip install --no-cache-dir -e .

RUN useradd --system --uid 10003 nightshift \
 && mkdir -p ${EDGE_DATA_DIR} \
 && chown -R nightshift /app ${EDGE_DATA_DIR}
USER nightshift

VOLUME ["/var/lib/nightshift-edge"]

# The gateway makes outbound connections only; it never listens on a public port.
ENTRYPOINT ["nightshift-edge"]
CMD ["run"]
