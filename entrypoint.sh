#!/bin/bash
# Runs the builder CLI against the repository mounted at /repo:
#   docker run --rm -v "$PWD":/repo ghcr.io/linuxserver-playground/builder:latest check
#   docker run --rm -v "$PWD":/repo --user "$(id -u):$(id -g)" ghcr.io/linuxserver-playground/builder:latest render

if [[ -n "${CI_RUN}" ]]; then
    /lsiopy/bin/builder --help >/dev/null
    echo '[ls.io-init] done.'
    exit 0
fi

# The mounted checkout is owned by the host user; let git read its HEAD regardless of who we run as.
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*'
export HOME="${HOME:-/tmp}"

cd /repo || exit 1
exec /lsiopy/bin/builder "$@"
