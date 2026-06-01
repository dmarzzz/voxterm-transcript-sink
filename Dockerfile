# voxterm-data-sink — image for deployment into a dstack CVM.
#
# Reproducibility (spec §6.3, §11.9): for measurement pinning the build must be
# reproducible — a third party rebuilding from this commit must get the SAME
# image digest. Three things make that hold (see docs/REPRODUCE.md):
#   1. base image pinned by DIGEST (ARG PYTHON_IMAGE below),
#   2. deps pinned by version+hash (requirements.lock, --require-hashes),
#   3. no embedded build timestamps: pip --no-compile (no mtime-bearing .pyc) +
#      build with SOURCE_DATE_EPOCH and buildx `rewrite-timestamp=true` to clamp
#      layer file mtimes. hatchling honours SOURCE_DATE_EPOCH for the wheel.
#
# The default below is digest-pinned for reproducible release builds (the tag is
# kept for human readability; the @sha256 is what's enforced). Refresh the digest
# when intentionally bumping the base image — see docs/REPRODUCE.md. Override per
# build with: docker build --build-arg PYTHON_IMAGE=python@sha256:<digest> .
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:93ab4b7fa528b25124c97bcc755415e60eb671a86b4dbe0328df2fe2d1c1193d
FROM ${PYTHON_IMAGE}

# --- non-root runtime user (defense-in-depth; compromise in-TD is still
#     compromise of plaintext) -------------------------------------------------
RUN useradd --system --create-home --uid 10001 sink \
    && mkdir -p /data && chown sink:sink /data

WORKDIR /app

# Make the release timestamp visible INSIDE the build so pip/hatchling stamp the
# wheel + dist-info deterministically (buildx's host SOURCE_DATE_EPOCH only clamps
# layer mtimes, it is not exported into RUN). Pass --build-arg SOURCE_DATE_EPOCH.
ARG SOURCE_DATE_EPOCH=0
ENV SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH} \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
# PIP_NO_CACHE_DIR as an ENV (not just the per-command flag) is required: the
# PEP 517 build-isolation subprocess that fetches the build backend would
# otherwise write timestamped files under /root/.cache/pip and poison the layer.

# Locked deps first for layer caching. requirements.lock pins exact versions AND
# hashes of the runtime + dstack deps (regenerate via `uv export`, see
# docs/REPRODUCE.md). --require-hashes makes the install fail closed if any
# resolved artifact does not match a pinned hash.
COPY requirements.lock ./
RUN pip install --no-cache-dir --no-compile --require-hashes -r requirements.lock

# Install the package itself without re-resolving deps (they're locked above).
# pyproject builds a wheel for BOTH the server (voxterm_transcript_sink) and the
# upload client (voxterm_sink_client), so both package dirs must be present.
COPY pyproject.toml ./
COPY voxterm_transcript_sink ./voxterm_transcript_sink
COPY voxterm_sink_client ./voxterm_sink_client
RUN pip install --no-cache-dir --no-compile --no-deps .

ENV VOXTERM_SINK_HOST=0.0.0.0 \
    VOXTERM_SINK_PORT=8723 \
    VOXTERM_SINK_ATTEST=dstack \
    VOXTERM_SINK_SNAPSHOT=/data/sink-snapshot.json

EXPOSE 8723
VOLUME ["/data"]
USER sink

CMD ["python", "-m", "voxterm_transcript_sink"]
