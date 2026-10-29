FROM ubuntu:24.04

# triton-terminus-offline-runtime: tmux
RUN set -eux; \
    if command -v apt-get >/dev/null; then apt-get update && apt-get install -y --no-install-recommends tmux && rm -rf /var/lib/apt/lists/*; \
    elif command -v apk >/dev/null; then apk add --no-cache tmux; \
    elif command -v dnf >/dev/null; then dnf install -y tmux && dnf clean all; \
    elif command -v yum >/dev/null; then yum install -y tmux && yum clean all; \
    else echo "no supported package manager for tmux" >&2; exit 1; fi

RUN apt-get update && apt-get install -y --no-install-recommends \
    g++ make python3 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY app/ /app/
