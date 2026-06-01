# voxterm-transcript-sink

An authenticated, always-on data sink for [VoxTerm](https://github.com/dmarzzz/VoxTerm)
hivemind mode. It runs inside a TEE (Intel TDX, on Phala/dstack), so it can hold a
group's full transcript history **without ever being able to read it**. You don't
trust the operator — you verify the enclave.

## Use it (client quickstart)

You have VoxTerm Markdown transcript exports. Verify the enclave, then upload — your
private author key never leaves your machine.

**Live instance:**

```
https://737d7cb9c5fbdff22d88408b3fdf3463a1d088b8-8723.dstack-pha-prod5.phala.network
```

```bash
SINK=https://737d7cb9c5fbdff22d88408b3fdf3463a1d088b8-8723.dstack-pha-prod5.phala.network

# 1. install the client (ships in the wheel as `voxterm-sink-upload`)
pipx install ./voxterm-transcript-sink        # or: pip install ./voxterm-transcript-sink

# 2. verify the sink is the genuine, pinned release BEFORE sending anything
voxterm-sink-upload verify --sink-url "$SINK" \
  --measurement-policy pinned --measurements ./measurements.json

# 3. upload transcripts into a hivemind
voxterm-sink-upload upload ~/Documents/voxterm \
  --sink-url "$SINK" --hivemind-id <uuid> --recursive
```

- **Uploading needs no secret** — writes are attested-but-open in v1 (your client
  verifies the enclave; the sink accepts the write). **Reading** transcripts back
  needs the shared read secret, which the operator gives you out-of-band.
- `--measurement-policy pinned` requires the live TDX quote to match the published
  [`measurements.json`](measurements.json) and **fails closed** otherwise. Omit it to
  fall back to trust-on-first-use (`tofu`).
- `upload` re-verifies first, so a standalone `verify` is optional. Use `--dry-run`
  to preview, `--json` for machine output.

Full walkthrough: [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md).

## API

Base URL `https://<app-id>-8723.<gateway-domain>/v1`. Every response is signed by the
sink's attested `sink_sig` key (`X-Sink-Signature`). Machine-readable contract:
[`openapi.yaml`](openapi.yaml); normative prose: [`specs/v1/voxterm-sink-protocol.md`](specs/v1/voxterm-sink-protocol.md).

| Method & path | Auth | Purpose |
|---|---|---|
| `GET /v1/health` | none | Liveness probe |
| `GET /v1/info` | none | Capabilities, limits, `sink_sig_pubkey`, `compose_hash` |
| `GET /v1/attestation?nonce=` | none | Fresh TDX quote bundle — how clients verify the enclave |
| `POST /v1/auth` | read secret | Read-tier auth handshake (spec §8) |
| `POST /v1/transcript` | attested write | Submit a finalized transcript |
| `POST /v1/transcript/stream` | attested write | Submit a live NDJSON chunk stream |
| `GET /v1/transcript` | read secret | List / read transcripts |
| `GET /v1/transcript/{id}` | read secret | Read one transcript |
| `GET /v1/transcript/{id}/chunks` | read secret | Read chunks / high-water |

## How verification works (why a TEE)

An always-on relay sees every entry that flows through a group, so normally you'd
have to trust whoever runs the box. A TEE removes that trust instead of asking for
it: the sink runs in an enclave with remote attestation, so the operator **cannot
read private payloads and cannot forge or tamper with entries**. A node that holds
the data without holding the keys to it.

Before a client sends anything it:

1. fetches a fresh TDX DCAP quote from `GET /v1/attestation` (with a nonce),
2. verifies it against Intel collateral and replays the event log to confirm the
   running code (RTMR3 → `compose_hash`),
3. checks the quote's `report_data` binds the sink's `sink_sig` key (channel binding
   + freshness), and
4. under `pinned`, requires the measurements to match the published release.

The build is reproducible (a clean rebuild reproduces the same image digest), so the
pinned measurements trace back to public source — see
[`docs/REPRODUCE.md`](docs/REPRODUCE.md).

## Where it fits

VoxTerm hivemind is mesh gossip with no coordinator: every member holds a complete
local replica and syncs when peers are reachable. The gap: it assumes someone is
online. This is the peer that never sleeps — not a coordinator, just one more node
that stays online to accept gossiped entries and serve backfill to a member who
returns.

```
 VoxTerm party-mode session ends
         │  signed readout
         ▼
 author's append-only log  ──gossip──►  peers
         │                                 │
         └──────────────► transcript-sink ◄┘   (this repo: TEE, always-on, backfill)
                                 │
                                 ▼
                          attested backfill to any returning member
```

It stores the v1 `Transcript` / `TranscriptChunk` envelopes as received: transcripts
content-addressed by BLAKE3 over JCS canonical bytes, chunks keyed by
`(session_id, author, seq)`, optional Ed25519 author signatures verified when present.
In v1 confidentiality is the verified TEE boundary plus KMS-sealed storage, **not**
end-to-end encryption — don't upload anything you wouldn't trust to a verified enclave.

## Run your own / develop

- [`docs/SELF_HOSTING.md`](docs/SELF_HOSTING.md) — stand up a sink (dev → dstack
  simulator → real TDX on Phala).
- [`docs/HOSTING_AND_GUARANTEES.md`](docs/HOSTING_AND_GUARANTEES.md) — hosting model,
  the v1 guarantees and non-guarantees, and the roadmap.
- [`docs/PHALA_DEPLOY.md`](docs/PHALA_DEPLOY.md) — the live production deployment +
  deploy runbook. [`docs/REPRODUCE.md`](docs/REPRODUCE.md) — reproducible build &
  measurement pinning. [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) — running the PoC
  and the deliberate cuts.

## Status

Proof-of-concept against a frozen wire contract (`voxterm-sink/1`, spec
`1.0.0-draft.1`). The data model is additive-only and versioned. **Deferred** (spec
§12, flagged not hidden): author-signed writes and registered membership, real read
auth replacing the shared secret, coordinator enforcement, end-to-end payload
encryption, tombstones, and the full Hivemind mesh bridge.
