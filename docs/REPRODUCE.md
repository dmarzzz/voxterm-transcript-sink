# Reproducible build & measurements

Pinned measurement policy (spec §6.3, §11.9) is only as strong as the ability to
reproduce `compose_hash` and the base-image measurements. This file is the
per-release procedure that produces `measurements.json`.

> Status: done for v0.1.0. The build is digest-pinned, hash-locked, and
> deterministic (steps 1–3), and `measurements.json` is filled from the live
> `voxterm-transcript-sink-prod` quote (steps 4–6) — pinned verification against
> it is confirmed working. The steps below are the per-release procedure to
> repeat on any future image/compose change (the live `compose_hash` +
> `mrtd`/`rtmr0..2` are not derivable on a non-TDX host, so they must be re-read
> from a live deployment each release).

## 1. Base image is digest-pinned (done)

`Dockerfile` defaults `PYTHON_IMAGE` to a pinned digest, not a moving tag:

```
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:93ab4b7fa528b25124c97bcc755415e60eb671a86b4dbe0328df2fe2d1c1193d
```

To intentionally bump the base image, resolve a new digest and update that line:

```bash
docker buildx imagetools inspect python:3.12-slim-bookworm   # → new index digest
```

## 2. Dependencies are hash-locked (done)

`requirements.lock` pins exact versions **and** sha256 hashes, regenerated from
`uv.lock`. The Dockerfile installs with `--require-hashes`, so the build fails
closed if any artifact does not match. Regenerate after a dependency change:

```bash
uv export --extra dstack --no-dev --no-emit-project --format requirements-txt -o requirements.lock
```

## 3. Build + push a reproducible immutable image (done for v0.1.0)

The build is deterministic: a clean rebuild from this commit yields the same
registry digest. That requires, beyond steps 1–2, eliminating embedded
timestamps — a fixed `SOURCE_DATE_EPOCH` (exported into the build so hatchling
stamps the wheel deterministically), `pip --no-compile` + `PIP_NO_CACHE_DIR=1`
(no `.pyc` mtimes, no timestamped pip cache in the layer), and buildx
`rewrite-timestamp=true` to clamp layer file mtimes. Single-platform with
provenance/SBOM off keeps the manifest clean.

```bash
export SOURCE_DATE_EPOCH=1735689600   # fixed release epoch — DO NOT use `date`
docker buildx build --platform linux/amd64 --provenance=false --sbom=false \
  --build-arg SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH \
  --output type=image,name=docker.io/sh1sh1nk/voxterm-data-sink:v0.1.0,push=true,unpack=false,rewrite-timestamp=true \
  .
docker buildx imagetools inspect sh1sh1nk/voxterm-data-sink:v0.1.0   # → Digest: ...
# → docker.io/sh1sh1nk/voxterm-data-sink@sha256:836bb5f28ffc85b572d096a7b6511353f24a567caea2d3c9d6e6efb43589742d
```

Verify reproducibility before pinning: run the command twice (add `--no-cache`)
and confirm the pushed digest is identical. This digest is pinned in
`docker-compose.phala.yaml` and `measurements.json`.

## 4. `compose_hash`

dstack computes `compose-hash = SHA-256(app-compose.json)` (the wrapper Phala
builds around `docker-compose.phala.yaml`) and extends it into RTMR3. You do not
hand-compute it: read the authoritative value back from the live quote in step 5.

## 5. Read measurements from the live production deployment

Once the production CVM is live (see `PHALA_DEPLOY.md`), the client itself
extracts every value you need. A TOFU verify records them; `trust inspect` prints
them:

```bash
voxterm-sink-upload verify --sink-url "https://<app-id>-8723.<gateway-domain>"
voxterm-sink-upload trust inspect
# the sink record carries: compose_hash, measurements.{mrtd,rtmr0,rtmr1,rtmr2,rtmr3}
```

`MRTD/RTMR0..2` come from the dstack base image + VM config (stable per
base-image release); `RTMR3` is verified by event-log replay and carries the
`compose-hash`. Pin `compose_hash` + `mrtd`/`rtmr0..2`; RTMR3 is covered by the
`compose_hash` binding and is not pinned directly.

Find the dstack base-image name (for the `name` field) on the CVM's Phala
dashboard / `phala cvms list` output.

## 6. Fill `measurements.json`

Replace the remaining `<FILL: ...>` placeholders (`compose_hash`, and the
`dstack_base_images[0]` `name`/`mrtd`/`rtmr0..2`) with the step-5 values, set the
real `release` string, and commit. Publish this file at the URL referenced by
`/v1/info` `build.measurements_ref`; pinned clients consume it with
`--measurements <path-or-published-file>`. Leave `kms` null/empty (static
pinning; KMS-rooted trust is deferred).

> Do **not** bake `measurements.json` into `voxterm_sink_client/` (the package is
> copied into the server image): the image digest feeds `compose_hash`, which is
> inside `measurements.json`, so bundling it would be circular. Keep it a
> repo-root + published artifact. (`default_measurements_path()` supports a
> packaged copy for a future *client-only* distribution that does not ship the
> server image.)
