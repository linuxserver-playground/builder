# syntax=docker/dockerfile:1

FROM ghcr.io/linuxserver/baseimage-alpine:3.22

# set version label
ARG BUILD_DATE
ARG VERSION
LABEL build_version="Linuxserver.io version:- ${VERSION} Build-date:- ${BUILD_DATE}"
LABEL maintainer="thelamer"

COPY . /tmp/app

RUN \
  echo "**** install runtime packages ****" && \
  apk add --no-cache \
    git \
    python3 && \
  echo "**** install builder ****" && \
  python3 -m venv /lsiopy && \
  /lsiopy/bin/pip install --no-cache-dir -U pip && \
  /lsiopy/bin/pip install --no-cache-dir /tmp/app && \
  printf "Linuxserver.io version: ${VERSION}\nBuild-date: ${BUILD_DATE}\n" > /build_version && \
  echo "**** cleanup ****" && \
  rm -rf \
    /tmp/* \
    /root/.cache

COPY entrypoint.sh /entrypoint.sh

WORKDIR /repo

ENTRYPOINT ["catatonit", "--", "/entrypoint.sh"]
