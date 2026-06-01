# Developing `voxterm-data-sink`

Reference PoC for the [VoxTerm Sink Protocol](../specs/v1/voxterm-sink-protocol.md)
(`voxterm-sink/1`). FastAPI server, in-memory store (+ optional JSON snapshot),
pluggable attestation.

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q                     # 53 hermetic tests
```

## Running

Three attestation postures, selected by `VOXTERM_SINK_ATTEST`:

| Mode | When | `sink_sig` identity | `/v1/attestation` |
|---|---|---|---|
| `dstack` (default) | inside a real dstack TD | guest-agent `get_key` (spec §5.2) | real TDX quote |
| `dstack` + simulator | local dev, real code path | guest-agent `get_key` (simulator) | simulator quote |
| `dev` | fast local hacking | **non-attested** seed key | fabricated stub (logs a warning) |

```bash
# Fast local hacking — fabricated, NON-ATTESTING quotes:
VOXTERM_SINK_ATTEST=dev python -m voxterm_transcript_sink   # :8723
```

## Local development against the dstack simulator (recommended)

Per Phala's [local-development guide](https://docs.phala.com/dstack/local-development),
run the TEE simulator and point the SDK at it. This exercises the **real**
`get_key` / `get_quote` path instead of the fabricated stub:

```bash
git clone https://github.com/Dstack-TEE/dstack.git
cd dstack/sdk/simulator && ./build.sh && ./dstack-simulator &   # creates dstack.sock
export DSTACK_SIMULATOR_ENDPOINT="$PWD/dstack.sock"             # SDK + our config read this

# back in this repo (default attest mode = dstack; endpoint comes from the env var):
pip install -e ".[dstack,dev]"
python -m voxterm_transcript_sink
pytest tests/test_simulator.py -v   # runs only when the env var is set
```

`DSTACK_SIMULATOR_ENDPOINT` is honored automatically (config reads it; the SDK
also reads it). Override with `VOXTERM_SINK_DSTACK_ENDPOINT` if needed.

## VoxTerm interoperability gap (important)

The current VoxTerm client (`network/hivemind.py`) does **not** speak this sink's
protocol. It posts unsigned batches to `POST /hivemind/transcripts`
(`shape-rotator-hivemind/v1`), discovered via LAN mDNS, with the shape
`{record_id, batch_index, started_at, ended_at, origin_device(uuid), location?,
segments:[{t, speaker, text}]}` and **no client-side attestation or signing**
(the legacy convent-box sink re-signs).

This sink implements `voxterm-sink/1`: `POST /v1/transcript[/stream]`,
content-addressed `id`, Ed25519 `author`, `session_id`, `t_start/t_end`, and a
client that **verifies the TEE before pushing** (§6). The two are not
wire-compatible — a real VoxTerm batch gets `404` on `/hivemind/transcripts` and
`400 schema_mismatch` on `/v1/transcript`.

These are different **trust models**, not just different routes. The alignment
plan — supporting both a legacy (non-TEE) and an attested (TEE) mode behind a
client config flag — is specified on the VoxTerm side at
`VoxTerm/docs/specs/hivemind-sink-integration.md`. An optional, off-by-default
legacy bridge endpoint on this sink is described there (§9, item 4) as a
migration aid; it is intentionally **not** implemented here, because a bridge
yields a synthesized, unverifiable `author` and bypasses §6 — it would buy
storage interop, not the TEE's security properties.

## Rate limiting

`/v1/info` advertises `limits.rate_per_min`, but **the app does not enforce it**
— it is the **gateway-enforced** limit. Per spec §11.2 public TLS terminates at
dstack-gateway, so the app sees only the gateway's IP (not real clients), and
§11.8 names gateway-level rate limiting + per-IP caps as the primary DoS layer
(availability is best-effort; the mesh is the source of truth). Configure those
limits on dstack-gateway to match the advertised `rate_per_min`. Size caps
(`max_chunk_bytes` / `max_transcript_bytes`) **are** enforced in-app (`413`).
Per-author write caps (§11.7) are a roadmap item (§12), not in this PoC.

## Deploying into a dstack CVM

`Dockerfile` + `docker-compose.yaml` build the image and bind-mount
`/var/run/dstack.sock` (required for `get_key`/`get_quote`). The image runs as a
non-root `sink` user. Deploy the compose file via `dstack-vmm`; dstack computes
the `compose-hash` extended into RTMR3.

In `dstack` mode the sink **fails closed**: if `get_key()` can't derive the
signing identity at startup it refuses to boot (spec §5.2) — it never falls back
to a non-attested key. Use `VOXTERM_SINK_ATTEST=dev` or the simulator off-TD.

### Reproducible builds & measurement pinning

Pinning (spec §6.3) requires a reproducible build, and the build now is one: the
base image is digest-pinned, deps install from a hash-locked `requirements.lock`
under `--require-hashes`, and the image is built deterministically
(`SOURCE_DATE_EPOCH` into the build env, `pip --no-compile`, `PIP_NO_CACHE_DIR=1`,
buildx `rewrite-timestamp`) so a clean rebuild from a commit reproduces the same
digest. See **REPRODUCE.md** for the exact command. The released image digest is
pinned in `docker-compose.phala.yaml`. What's still pending is the *live*
half of `measurements.json` (spec Appendix B) — the real `compose_hash` +
`MRTD/RTMR0..2` can only be read back from an actual dstack/TDX deployment, so
those fields ship as **placeholders** until a release reads them from the live
quote.

## Known limitations (PoC)

These are deliberate cuts; each is a spec feature deferred for the PoC:

- **Stream buffering** (§7.5): `POST /v1/transcript/stream` reads the full
  request body before processing rather than consuming `request.stream()`
  incrementally with per-chunk durable acks. Fine for the PoC; a production sink
  should stream and ack incrementally to bound memory and enable true live tail.
- **Durability** (§7.5 "persists durably before acknowledging"): the primary
  store is in-memory. The optional JSON snapshot is `fsync`'d (file + parent
  dir) on write, but the whole store is rewritten each time — it is a snapshot,
  not a per-chunk write-ahead log. Production should use SQLite/Postgres with
  durable transactions.
- **Pre-finalization resume** (§7.5): in this buffered PoC, the only
  authenticated high-water before transcript assembly is the signed ack body
  returned by a completed `POST .../stream` response. `X-Sink-Seq` is an
  unauthenticated hint only. `GET /v1/transcript/{id}/chunks` resolves an `{id}`
  to a stored transcript, so it can't report high-water for an in-progress,
  not-yet-assembled session. A standalone signed session-scoped query is roadmap
  (spec §12).
- **Field validation**: models enforce hex (`author`/`id`), UUID (`hivemind_id`),
  `session_id` shape (`YYYY-MM-DD_HHMMSS`, matching VoxTerm `tui/app.py`),
  RFC3339 timestamps with an explicit UTC offset (`Z`/`+00:00` only — non-UTC
  offsets and naive timestamps are rejected, spec §1), `confidence∈[0,1]`, and
  size caps — but not every spec invariant (e.g. cross-field timing
  monotonicity). Unknown fields are preserved (additive-only, §9).

### Implementation notes (intentional, not bugs)

- `models.TranscriptMeta` / `models.StoreResult` are documentation-only shapes —
  the routes build the response dicts directly, so these classes are currently
  unused. Kept as a typed reference for the §7.6/§7.4 response bodies.
- The snapshot writer uses a single `os.write()` before `fsync` (store.py). A
  partial write is theoretically possible for very large payloads; at PoC volume
  it isn't a concern. A production store (SQLite/Postgres) sidesteps this.

## Configuration (env)

| Var | Default | Purpose |
|---|---|---|
| `VOXTERM_SINK_ATTEST` | `dstack` | `dstack` or `dev` |
| `DSTACK_SIMULATOR_ENDPOINT` / `VOXTERM_SINK_DSTACK_ENDPOINT` | — | simulator socket/URL |
| `VOXTERM_SINK_READ_SECRET` | `1234` | read-tier secret (§8.3; warns at default) |
| `VOXTERM_SINK_SNAPSHOT` | — | JSON snapshot path for persistence |
| `VOXTERM_SINK_HOST` / `VOXTERM_SINK_PORT` | `0.0.0.0` / `8723` | bind address |
