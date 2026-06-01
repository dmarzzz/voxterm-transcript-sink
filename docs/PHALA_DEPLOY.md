# Phala Cloud production deployment

This deploys only `voxterm-data-sink`, the always-on TEE transcript sink. The
local VoxTerm recorder/TUI is not part of this cloud deployment.

## Live production deployment (as built)

| | |
|---|---|
| App name | `voxterm-transcript-sink-prod` |
| App ID | `737d7cb9c5fbdff22d88408b3fdf3463a1d088b8` |
| Base URL | `https://737d7cb9c5fbdff22d88408b3fdf3463a1d088b8-8723.dstack-pha-prod5.phala.network` |
| Image | `sh1sh1nk/voxterm-data-sink@sha256:836bb5f2…` (reproducible, see `REPRODUCE.md`) |
| Base image | `dstack-0.5.9` |
| Measurements | published in `measurements.json` (pinning works — verified live) |

Pinned verification against the live sink:

```bash
voxterm-sink-upload verify \
  --sink-url https://737d7cb9c5fbdff22d88408b3fdf3463a1d088b8-8723.dstack-pha-prod5.phala.network \
  --measurement-policy pinned --measurements ./measurements.json
```

`voxterm-transcript-sink-prod` is the only deployed app (an earlier staging CVM
was retired). Stand up a separate staging app only if you need a test bed.

## Reproducing / re-deploying this (needs Phala credentials)

### 1. Validate locally

```bash
uv run pytest -q                                     # 54 passing
VOXTERM_SINK_READ_SECRET=x docker compose -f docker-compose.phala.yaml config >/dev/null
```

### 2. Fresh production read secret

Never reuse a prior secret or the `1234` default (spec §8.3). Keep it safe — read
clients need it. Pass it via an env file (avoids leaking it in `ps`):

```bash
umask 077
printf 'VOXTERM_SINK_READ_SECRET=%s\n' "$(openssl rand -hex 32)" > prod.env
```

### 3. Deploy

`phala deploy` resolves the target CVM by the `name` in `phala.toml`. With
`name = "voxterm-transcript-sink-prod"` it **updates** that app once it exists.
To create it the first time, the name must not match any existing CVM — move
`phala.toml` aside and pass `-n` explicitly:

```bash
phala login
mv phala.toml /tmp/phala.toml.bak          # only needed for the very first create
phala deploy \
  -n voxterm-transcript-sink-prod \
  -c docker-compose.phala.yaml \
  -e prod.env -t tdx.small --kms phala \
  --no-public-logs --no-public-sysinfo --no-listed --wait
mv /tmp/phala.toml.bak phala.toml
rm -f prod.env
```

(`phala cvms upgrade <app-id>` upgrades an existing app in place instead, keeping
its identity + `/data` volume — used if you ever want to promote a CVM rather
than stand up a new one.)

### 4. Smoke test

```bash
BASE_URL="https://<app-id>-8723.<gateway-domain>"   # gateway base_domain via `phala cvms get <app-id> --json`
curl -fsS "$BASE_URL/v1/health"
curl -fsS "$BASE_URL/v1/info"
curl -fsS "$BASE_URL/v1/attestation?nonce=$(openssl rand -hex 32)"   # 503 ⇒ can't reach guest agent
```

### 5. Freeze measurements (only when releasing a NEW build/app)

A new app/compose yields a new `compose_hash`. Read the real values from the live
quote and pin them (`REPRODUCE.md` §5–6):

```bash
voxterm-sink-upload verify --sink-url "$BASE_URL"   # TOFU records measurements
voxterm-sink-upload trust inspect                   # compose_hash + mrtd/rtmr0..2
```

Fill `measurements.json` (repo root) + the dstack base-image name and commit;
publish it for clients. Keep it at the repo root, **not** bundled into
`voxterm_sink_client/` (circular with the image digest — see `REPRODUCE.md` §6).
