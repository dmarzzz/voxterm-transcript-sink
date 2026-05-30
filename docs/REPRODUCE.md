# Reproducible build & measurements

Pinned measurement policy (spec §6.3, §11.9) is only as strong as the ability to
reproduce `compose_hash` and the base-image measurements. This file is the
per-release procedure that produces `measurements.json`.

> Status: the values in `measurements.json` are **placeholders**. They can only
> be filled by an actual dstack/TDX build — they are not derivable on a non-TDX
> host. Do not pin against placeholder measurements.

## 1. Pin the base image by digest

The default `Dockerfile` base (`python:3.12-slim-bookworm`) is a moving tag. For
a release, resolve and pin a digest:

```bash
docker pull python:3.12-slim-bookworm
docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim-bookworm
# → python@sha256:<digest>
docker build --build-arg PYTHON_IMAGE=python@sha256:<digest> -t voxterm-data-sink .
```

## 2. Lock dependencies

`requirements.lock` pins exact versions (generated via
`pip freeze --exclude-editable` in a clean resolve). For stronger guarantees,
regenerate with hashes using `uv pip compile --generate-hashes` or
`pip-compile --generate-hashes` in CI and commit the result.

## 3. Build + push an immutable image

```bash
docker build --build-arg PYTHON_IMAGE=python@sha256:<digest> \
  -t ghcr.io/<org>/voxterm-data-sink:v0.1.0 .
docker push ghcr.io/<org>/voxterm-data-sink:v0.1.0
docker inspect --format='{{index .RepoDigests 0}}' ghcr.io/<org>/voxterm-data-sink:v0.1.0
# → ghcr.io/<org>/voxterm-data-sink@sha256:<image-digest>   (put this in measurements.json + compose SINK_IMAGE)
```

## 4. Compute `compose_hash`

dstack computes `compose-hash = SHA-256(app-compose.json)` and extends it into
RTMR3. Wrap `docker-compose.yaml` (with the pinned `SINK_IMAGE` digest) into the
`app-compose.json` your `dstack-vmm` deploys, then:

```bash
sha256sum app-compose.json   # → compose_hash
```

## 5. Read MRTD / RTMR0..2 from a real deployment

Deploy into a dstack TD (or the simulator for shape only), then read the quote /
TCB info:

```python
from dstack_sdk import DstackClient
info = DstackClient().info()        # tcb_info carries MRTD, RTMR0..3, event log
```

`MRTD/RTMR0..2` come from the dstack base image + VM config and are
precomputable per base-image release; `RTMR3` is verified by event-log replay
and carries the `compose-hash`.

## 6. Fill `measurements.json`

Replace every `<FILL: ...>` placeholder with the values from steps 1–5, set the
real `release` string, and publish it at the URL referenced by `/v1/info`
`build.measurements_ref`.
