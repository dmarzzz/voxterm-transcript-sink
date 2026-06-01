# Phala Cloud production deployment

This deploys only `voxterm-data-sink`, the always-on TEE transcript sink. The
local VoxTerm recorder/TUI is not part of this cloud deployment.

Strategy: **promote the existing staging app in place.** We upgrade the *same*
Phala app (same app-id) to a reproducibly-built, digest-pinned production image.
Keeping the app-id preserves the `get_key`-derived `sink_sig` identity and the
encrypted `/data` volume across the upgrade. The `compose_hash` (RTMR3) changes
by design — that new value is what production clients pin (spec §6.3).

> The image is already built, pushed, and pinned
> (`sh1sh1nk/voxterm-data-sink@sha256:b9f5c72b…`, see `REPRODUCE.md`). These steps
> need your Phala credentials, so run them yourself.

## 1. Validate locally

```bash
cd /home/ubuntu/voice/voxterm-transcript-sink
uv run pytest -q                                  # 53 passing
docker compose -f docker-compose.phala.yaml config   # needs VOXTERM_SINK_READ_SECRET set
```

## 2. Generate a fresh production read secret

Do **not** reuse the staging secret or the `1234` default (spec §8.3). Keep this
value somewhere safe — read clients need it.

```bash
VOXTERM_SINK_READ_SECRET="$(openssl rand -hex 32)"
```

## 3. Upgrade the existing app in place

Find the app/CVM, then upgrade it to the pinned compose with the new secret.
(Confirm exact subcommands with `phala --help`; the CLI evolves.)

```bash
phala login
phala cvms list                       # note the app-id for voxterm-transcript-sink-staging

phala cvms upgrade <app-id> \
  -c docker-compose.phala.yaml \
  -e "VOXTERM_SINK_READ_SECRET=$VOXTERM_SINK_READ_SECRET"
```

Upgrading the existing app id (rather than `phala deploy` with a new name)
preserves identity + volume. `phala.toml` keeps the historical
`voxterm-transcript-sink-staging` name on purpose — renaming it would provision a
*new* app and reset the identity/volume.

## 4. Smoke test

```bash
BASE_URL="https://<app-id>-8723.<gateway-domain>"

curl -fsS "$BASE_URL/v1/health"
curl -fsS "$BASE_URL/v1/info"
curl -fsS "$BASE_URL/v1/attestation?nonce=$(openssl rand -hex 32)"
```

A `503` on `/v1/attestation` means the service cannot reach the dstack guest
agent or is not in the expected CVM environment.

## 5. Freeze the production measurements

The new app produces a new `compose_hash`. Read the real values from the live
quote and pin them (this is `REPRODUCE.md` steps 5–6):

```bash
voxterm-sink-upload verify --sink-url "$BASE_URL"   # TOFU records the measurements
voxterm-sink-upload trust inspect                   # prints compose_hash + mrtd/rtmr0..2
```

Fill `measurements.json` (repo root) with those values + the dstack base-image
name and commit; publish it for clients. Production clients then verify against
it with:

```bash
voxterm-sink-upload verify --sink-url "$BASE_URL" \
  --measurement-policy pinned --measurements ./measurements.json
```

(Don't bundle `measurements.json` into `voxterm_sink_client/` — it would be
circular with the image digest; see `REPRODUCE.md` §6.)

## 6. Migrate existing TOFU testers

Anyone who verified the old staging app under TOFU has its old measurements in
their local trust store; the new `compose_hash` will (correctly) make
re-verification fail. They reset and re-verify:

```bash
voxterm-sink-upload trust reset --sink-url "$BASE_URL"
voxterm-sink-upload verify --sink-url "$BASE_URL" --measurement-policy pinned
```
