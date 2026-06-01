# Self-hosting (running a sink)

This is the guided path to standing up a `voxterm-sink/1` sink, in three postures of
increasing fidelity: a **dev** server for fast iteration, the **dstack simulator** for
the real attestation code path off-TDX, and a **real TEE** deployment on Phala/dstack
Intel TDX. If you just want to *use* an existing sink, see
[GETTING_STARTED.md](GETTING_STARTED.md) instead.

**Prerequisites:** Python 3.12+, and Docker for the TDX deploy.

## Posture A — dev server (fastest, NOT attestable)

```bash
cd voxterm-transcript-sink
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q                                            # 53 passing

VOXTERM_SINK_ATTEST=dev python -m voxterm_transcript_sink   # serves on :8723
```

Dev mode derives a non-attested seed key and serves a **fabricated** quote at
`GET /v1/attestation` (it logs a warning). A real client cannot meaningfully attest
it — this posture is for exercising the API and wiring up the upload CLI against a
local target, nothing more. Smoke it:

```bash
curl -fsS localhost:8723/v1/info
curl -fsS localhost:8723/v1/health
```

## Posture B — dstack simulator (real code path, off-TDX)

Runs the **real** `get_key` / `get_quote` path against Phala's TEE simulator instead
of the dev stub. Per [DEVELOPMENT.md](DEVELOPMENT.md):

```bash
git clone https://github.com/Dstack-TEE/dstack.git
cd dstack/sdk/simulator && ./build.sh && ./dstack-simulator &   # creates dstack.sock
export DSTACK_SIMULATOR_ENDPOINT="$PWD/dstack.sock"

# back in this repo (default attest mode is dstack; endpoint comes from the env var):
pip install -e ".[dstack,dev]"
python -m voxterm_transcript_sink
pytest tests/test_simulator.py -v    # runs only when the env var is set
```

## Posture C — real TEE on Phala / dstack TDX

The production sink runs inside a Confidential VM on Intel TDX. The full procedure is
[PHALA_DEPLOY.md](PHALA_DEPLOY.md); the reproducible-build and measurement-pinning
procedure is [REPRODUCE.md](REPRODUCE.md). The shape:

- The image is a **reproducibly-built, digest-pinned** artifact referenced from
  `docker-compose.phala.yaml` (`sh1sh1nk/voxterm-data-sink@sha256:836bb5f2…`). A clean
  rebuild from the same commit reproduces the same digest.
- Current strategy is to **upgrade the existing app in place**
  (`phala cvms upgrade <app-id> -c docker-compose.phala.yaml -e …`) rather than deploy
  a new one. Keeping the app-id preserves the `get_key`-derived `sink_sig` identity and
  the encrypted `/data` volume across upgrades; the `compose_hash` (RTMR3) changes by
  design, and that new value is what production clients pin.
- Set a **real** `VOXTERM_SINK_READ_SECRET` (e.g. `openssl rand -hex 32`) — never the
  `1234` default. In `dstack` mode the sink **fails closed**: if `get_key()` can't
  derive the signing identity at boot, it refuses to start (it never falls back to a
  non-attested key).

Smoke-test the live endpoint:

```bash
BASE_URL="https://<app-id>-8723.<gateway-domain>"
curl -fsS "$BASE_URL/v1/health"
curl -fsS "$BASE_URL/v1/info"
curl -fsS "$BASE_URL/v1/attestation?nonce=$(openssl rand -hex 32)"   # 503 ⇒ can't reach guest agent
```

### Publishing pinned measurements

Once the CVM is live, the live quote yields the real `compose_hash` and base-image
measurements. Read them back with the client and pin them for your cohort:

```bash
voxterm-sink-upload verify --sink-url "$BASE_URL"   # TOFU records the measurements
voxterm-sink-upload trust inspect                   # prints compose_hash + mrtd/rtmr0..2
```

Fill those into `measurements.json` (it ships with **placeholders**), publish it, and
hand clients `--measurement-policy pinned --measurements <file>`. Full steps:
[REPRODUCE.md](REPRODUCE.md) §4–6.

## Configuration (environment variables)

| Var | Default | Purpose |
|---|---|---|
| `VOXTERM_SINK_ATTEST` | `dstack` | `dstack` (real TD / simulator) or `dev` (insecure stub). |
| `DSTACK_SIMULATOR_ENDPOINT` / `VOXTERM_SINK_DSTACK_ENDPOINT` | — | dstack guest-agent socket or simulator URL. |
| `VOXTERM_SINK_READ_SECRET` | `1234` | Read-tier secret (warns at the default; **change for any real use**). |
| `VOXTERM_SINK_SNAPSHOT` | — | JSON snapshot path for crude persistence (e.g. `/data/sink-snapshot.json`). |
| `VOXTERM_SINK_HOST` / `VOXTERM_SINK_PORT` | `0.0.0.0` / `8723` | Bind address. |

Full table and rationale: [DEVELOPMENT.md](DEVELOPMENT.md#configuration-env).

## Production caveats (be honest with yourself)

This is a proof-of-concept implementation of a frozen spec. Deliberate cuts:

- **Durability** — the primary store is in-memory; the optional JSON snapshot is
  `fsync`'d but rewrites the whole store each time. Use SQLite/Postgres for real
  durability.
- **Rate limiting** — `/v1/info` advertises `rate_per_min`, but the app does **not**
  enforce it. Public TLS terminates at `dstack-gateway`, so the app sees only the
  gateway IP; enforce rate limits / per-IP caps **at the gateway**. Size caps
  (`max_chunk_bytes` / `max_transcript_bytes`) *are* enforced in-app (`413`).
- **Measurements** — `measurements.json` ships with placeholders until a real TDX
  build reads back the values. Don't ask a pinned cohort to trust placeholders.

The complete list of PoC cuts is in [DEVELOPMENT.md](DEVELOPMENT.md); the hosting
model and guarantees are in [HOSTING_AND_GUARANTEES.md](HOSTING_AND_GUARANTEES.md).
