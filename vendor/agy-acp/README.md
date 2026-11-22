# Antigravity ACP provider

Adds an **Antigravity ACP** provider card to 9router. Instead of replaying a
stored OAuth refresh token through 9router's own HTTP client, this provider
routes completions through Google's own `agy acp` server (Agent Client
Protocol), the same transport Zed, JetBrains, Android Studio and Xcode use.

That matters because the fingerprint of the outgoing request is what Google's
abuse detection flags. Requests from `agy` carry the official Antigravity CLI
identity; requests replayed by a third-party client do not. See upstream
discussion in [decolua/9router#5759](https://github.com/decolua/9router/issues/5759).

## Provenance

Vendored from [rassyy/agy-acp-bridge](https://github.com/rassyy/agy-acp-bridge)
(MIT). Local changes are limited to path portability — the upstream scripts
assume `/root` and this deployment runs as an unprivileged user inside a
container.

| File | Origin | Local change |
| --- | --- | --- |
| `bridge.py` | upstream | none |
| `9r-patch/apply.py` | upstream | none |
| `9r-patch/antigravity-acp.js` | upstream | none |
| `9r-patch/agy-acp-route.js` | upstream | none |
| `9r-patch/AcpAccountsCard.js` | upstream | none |
| `9r-patch/hooks/deny_builtin.py` | upstream | `MODE_DIR` reads `AGY_CWD` |
| `Dockerfile` | ours | — |
| `docker-compose.override.yml` | ours | — |

## Why a sidecar container

The `agy` binary Google ships is dynamically linked against **glibc**. The
9router runtime image is **Alpine/musl**, so `agy` cannot exec inside it
(`Exec format error` / missing loader). Rather than bolt a glibc layer onto the
9router image, the bridge runs as its own Debian-based container on the same
compose network, with the host's real `agy` binary bind-mounted read-only.

```
9router  ->  agy-acp:20130  ->  agy acp (stdio)  ->  Google
```

The bridge publishes **no host port**. If you publish `20130`, set
`AGY_ADMIN_TOKEN` first: an empty token makes every `/admin` route open.

## Build

The provider registry and the dashboard are compiled into the Next.js bundles,
so the card is applied **before** `npm run build`. The root `Dockerfile` gains
one idempotent step, gated by `WITH_AGY_ACP` (default `1`):

```dockerfile
ARG WITH_AGY_ACP=1
RUN if [ "$WITH_AGY_ACP" = "1" ]; then \
      python3 vendor/agy-acp/9r-patch/apply.py /app; \
    fi
```

The bridge image is built separately by `.github/workflows/agy-acp-image.yml`
and pushed to `ghcr.io/<owner>/agy-acp-bridge`.

## Deploy

```bash
export AGY_ADMIN_TOKEN="$(openssl rand -hex 24)"   # required
docker compose \
  -f /home/ubuntu/9router/docker-compose.yml \
  -f vendor/agy-acp/docker-compose.override.yml \
  up -d
```

Then, in the dashboard: **Providers -> Antigravity ACP -> Accounts**, start a
login, open the printed Google URL, paste the code back.

## Account safety

- Log in **one** account at a time, from the dashboard. The login goes through
  `agy acp --login`, i.e. Google's own flow.
- Do not add an account that has already been disabled for a ToS violation by
  logging it in again elsewhere — the ban is on the account, not the client.
- Never publish port 20130.

## Models exposed

`gemini-3.8-flash-high`, `gemini-3.8-flash-medium`, `gemini-3.8-flash-low`,
`claude-sonnet-4-6`, `claude-opus-4-6-thinking`, `gpt-oss-120b-medium`.
