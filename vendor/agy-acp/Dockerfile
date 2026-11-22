# Antigravity ACP bridge — runs Google's own `agy acp` server.
#
# Why a separate image: the `agy` binary is glibc-linked, while the 9router
# runtime image is Alpine/musl. Running it inside the 9router container would
# need a glibc compatibility layer. This image is Debian-based instead, and the
# binary is bind-mounted from the host at runtime (see the compose overlay).
#
# The bridge only talks to accounts.google.com (OAuth) and to the local `agy`
# process over stdio. It holds no outbound telemetry.
FROM python:3.11-slim-bookworm

LABEL org.opencontainers.image.title="agy-acp-bridge" \
      org.opencontainers.image.description="OpenAI-compatible bridge over Antigravity's official ACP server"

ENV PYTHONUNBUFFERED=1 \
    NO_COLOR=1 \
    TERM=dumb \
    AGY_BIN=/usr/local/bin/agy \
    AGY_APPDATA_ROOT=/data/gemini \
    AGY_PROFILES_DIR=/data/profiles \
    AGY_CWD=/data/work \
    AGY_BRIDGE_PORT=20130 \
    AGY_BRIDGE_BIND=0.0.0.0

# ca-certificates: the OAuth login exchange is HTTPS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/agy-acp
COPY bridge.py hooks/ ./

RUN mkdir -p /data/gemini /data/profiles /data/work \
    && chmod 0755 /data

# 0.0.0.0 inside the image is safe: the port is only published on the internal
# compose network, never to the host. Never publish it to a public interface —
# an empty AGY_ADMIN_TOKEN makes every /admin route open.
EXPOSE 20130

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:20130/health', timeout=4).status==200 else 1)"

STOPSIGNAL SIGTERM

CMD ["python3", "/opt/agy-acp/bridge.py"]
