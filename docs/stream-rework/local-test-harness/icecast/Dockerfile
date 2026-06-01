# Stock Icecast (protocol-compatible with Icecast-KH for this rehearsal).
# Swap to Icecast-KH before the real Phase-3 cutover; the mount/codec are identical.
FROM debian:bookworm-slim
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends icecast2 \
 && rm -rf /var/lib/apt/lists/*
COPY icecast.xml /etc/icecast2/icecast.xml
EXPOSE 8000
CMD ["icecast2", "-c", "/etc/icecast2/icecast.xml"]
