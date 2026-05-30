# voxterm-data-sink — image for deployment into a dstack CVM.
#
# Reproducibility (spec §6.3, §11.9): for measurement pinning the build must be
# reproducible. Pin the base image by DIGEST at release time via build-arg, and
# install hash-/version-locked deps from requirements.lock. See docs/REPRODUCE.md.
#
# Default tag is a moving target; override at release:
#   docker build --build-arg PYTHON_IMAGE=python@sha256:<digest> -t voxterm-data-sink .
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE}

# --- non-root runtime user (defense-in-depth; compromise in-TD is still
#     compromise of plaintext) -------------------------------------------------
RUN useradd --system --create-home --uid 10001 sink \
    && mkdir -p /data && chown sink:sink /data

WORKDIR /app

# Locked deps first for layer caching. requirements.lock pins exact versions of
# the runtime + dstack deps (regenerate in CI: pip freeze --exclude-editable).
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

# Install the package itself without re-resolving deps (they're locked above).
COPY pyproject.toml ./
COPY voxterm_transcript_sink ./voxterm_transcript_sink
RUN pip install --no-cache-dir --no-deps .

ENV VOXTERM_SINK_HOST=0.0.0.0 \
    VOXTERM_SINK_PORT=8723 \
    VOXTERM_SINK_ATTEST=dstack \
    VOXTERM_SINK_SNAPSHOT=/data/sink-snapshot.json

EXPOSE 8723
VOLUME ["/data"]
USER sink

CMD ["python", "-m", "voxterm_transcript_sink"]
