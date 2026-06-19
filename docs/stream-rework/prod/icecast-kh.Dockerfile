# Icecast-KH (karlheyes fork) built from pinned upstream source.
#
# Why build our own image (issue #173/#178): there's no official Icecast-KH
# image, and we don't want an unmaintained third-party one in the path of the
# public stream. This compiles a pinned release in a builder stage and ships a
# slim runtime — reproducible and auditable. Run it via icecast.service
# (docker run --network host), mirroring liquidsoap.service.
#
# Build (on the box):  docker build -f icecast-kh.Dockerfile -t moafunk/icecast-kh:kh22 .
# Bump version:        --build-arg KH_VERSION=icecast-2.4.0-kh23
ARG KH_VERSION=icecast-2.4.0-kh22

FROM debian:12-slim AS build
ARG KH_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential ca-certificates curl autoconf automake libtool pkg-config \
      libxml2-dev libxslt1-dev libvorbis-dev libtheora-dev libssl-dev \
      libcurl4-openssl-dev libogg-dev libspeex-dev \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL "https://github.com/karlheyes/icecast-kh/archive/refs/tags/${KH_VERSION}.tar.gz" \
      -o /tmp/kh.tgz \
 && mkdir -p /src && tar -xzf /tmp/kh.tgz -C /src --strip-components=1
WORKDIR /src
RUN ./autogen.sh \
 && ./configure --prefix=/usr/local --with-curl --with-openssl \
 && make -j"$(nproc)" \
 && make install

FROM debian:12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      libxml2 libxslt1.1 libvorbis0a libvorbisenc2 libtheora0 libssl3 \
      libcurl4 libogg0 libspeex1 ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --no-create-home --shell /usr/sbin/nologin icecast \
 && mkdir -p /var/log/icecast && chown icecast:icecast /var/log/icecast
COPY --from=build /usr/local /usr/local
# KH ships status.xsl but NOT the JSON status XSLT that stock Icecast has. The
# #177 metrics poller needs GET /status-json.xsl, so vendor the stock XSLT pair
# (status-json.xsl imports xml2json.xslt) into the webroot.
COPY icecast-web/status-json.xsl icecast-web/xml2json.xslt /usr/local/share/icecast/web/
USER icecast
EXPOSE 8010
# Icecast answers HEAD with 400 — health-check with GET against the status JSON.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8010/status-json.xsl >/dev/null || exit 1
ENTRYPOINT ["/usr/local/bin/icecast"]
CMD ["-c", "/etc/moafunk/icecast.xml"]
