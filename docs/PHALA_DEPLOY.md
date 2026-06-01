# Phala Cloud staging deployment

This deploys only `voxterm-data-sink`, the always-on TEE transcript sink. The
local VoxTerm recorder/TUI is not part of this cloud deployment.

## 1. Validate locally

```bash
cd /home/ubuntu/voice/voxterm-transcript-sink
uv run pytest -q
docker compose -f docker-compose.phala.yaml config
```

`docker compose ... config` will fail until the image digest placeholder is
replaced and `VOXTERM_SINK_READ_SECRET` is set.

## 2. Build and publish the image

```bash
docker buildx build --platform linux/amd64 \
  -t sh1sh1nk/voxterm-data-sink:v0.1.0-staging.1 \
  --push .

docker buildx imagetools inspect \
  sh1sh1nk/voxterm-data-sink:v0.1.0-staging.1
```

Copy the reported `sha256` digest into `docker-compose.phala.yaml`:

```yaml
image: sh1sh1nk/voxterm-data-sink@sha256:<digest>
```

## 3. Deploy to Phala

```bash
npm install -g phala
phala login

VOXTERM_SINK_READ_SECRET="$(openssl rand -hex 32)"

phala deploy \
  -n voxterm-transcript-sink-staging \
  -c docker-compose.phala.yaml \
  -e "VOXTERM_SINK_READ_SECRET=$VOXTERM_SINK_READ_SECRET" \
  --kms phala \
  --region us-west \
  --instance-type tdx.small \
  --no-public-logs \
  --no-public-sysinfo \
  --wait
```

After deploy, link the local project if desired:

```bash
phala link voxterm-transcript-sink-staging
```

`phala.toml` stores the staging name, compose path, and private log/sysinfo
defaults so future deploys can be shortened after the first successful link.

## 4. Smoke test

Use the HTTPS endpoint returned by Phala. Port `8723` is exposed by the compose
file, so the public URL should include the app id and port.

```bash
BASE_URL="https://<app-id>-8723.<gateway-domain>"

curl -fsS "$BASE_URL/v1/health"
curl -fsS "$BASE_URL/v1/info"
curl -fsS "$BASE_URL/v1/attestation?nonce=$(openssl rand -hex 32)"
```

The attestation endpoint must not return `503`; a `503` means the service cannot
reach the dstack guest agent or is not running in the expected CVM environment.

For staging, use TOFU verification. `measurements.json` still contains
placeholders and must not be used for pinned verification until a real Phala
deployment has produced the release measurements.
