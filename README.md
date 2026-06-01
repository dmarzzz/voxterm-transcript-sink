# voxterm-transcript-sink

The always-on TEE transcript sink for the **shape-rotator** hivemind — a
[VoxTerm](https://github.com/dmarzzz/VoxTerm) cohort. It runs inside a TEE (Intel
TDX, on Phala/dstack), so the **operator can't read the cohort's transcripts** — only
the cohort can, over an authenticated read channel. The box holds the data without
holding the keys to it. You don't trust the operator; you verify the enclave.

## Use it (shape-rotator quickstart)

You're in the shape-rotator cohort, you have VoxTerm Markdown transcript exports, and
you want them on the always-on sink so anyone in the group can backfill later. Verify
the enclave, then upload — your private author key never leaves your machine.

```bash
# the live shape-rotator sink, and the cohort's hivemind id
SINK=https://737d7cb9c5fbdff22d88408b3fdf3463a1d088b8-8723.dstack-pha-prod5.phala.network
HIVEMIND=e743cd05-921c-5554-b79d-e2db6847d9d5      # the shape-rotator hivemind

# 1. install the client (ships in the wheel as `voxterm-sink-upload`)
pipx install ./voxterm-transcript-sink        # or: pip install ./voxterm-transcript-sink

# 2. verify the sink is the genuine, pinned release BEFORE sending anything
voxterm-sink-upload verify --sink-url "$SINK" \
  --measurement-policy pinned --measurements ./measurements.json

# 3. upload your transcripts into the shape-rotator hivemind
voxterm-sink-upload upload ~/Documents/voxterm \
  --sink-url "$SINK" --hivemind-id "$HIVEMIND" --recursive
```

- **`HIVEMIND`** is the shape-rotator cohort's shared hivemind id — everyone in the
  group uploads under the same one so the transcripts collect together (it's the
  UUIDv5 of `"shape-rotator"`, so anyone can recompute it). One sink can hold many
  hiveminds; this is ours.
- **Uploading needs no secret** — writes are attested-but-open in v1 (your client
  verifies the enclave; the sink accepts the write). **Reading** transcripts back
  needs the shared read secret, which the operator shares with the cohort.
- `--measurement-policy pinned` requires the live TDX quote to match the published
  [`measurements.json`](measurements.json) and **fails closed** otherwise. Omit it to
  fall back to trust-on-first-use (`tofu`).
- `upload` re-verifies first, so a standalone `verify` is optional. Use `--dry-run`
  to preview, `--json` for machine output.

Full walkthrough: [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md).

## Read transcripts back

Anyone in the cohort can read the history back — this is what the always-on sink is
*for* (backfill for whoever was offline). Reading needs the cohort **read secret**
(shared out-of-band by the operator). Exchange it for a short-lived bearer token,
then query:

```bash
READ_SECRET=<from the operator>      # not the same as uploading — reads are gated

# exchange the read secret for a bearer token (POST /v1/auth)
TOKEN=$(curl -fsS -X POST "$SINK/v1/auth" -H 'Content-Type: application/json' \
  -d "{\"tier\":\"cohort\",\"secret\":\"$READ_SECRET\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

# list transcripts in the shape-rotator hivemind
curl -fsS -H "Authorization: Bearer $TOKEN" "$SINK/v1/transcript?hivemind_id=$HIVEMIND"

# fetch one full transcript, or its chunk log
curl -fsS -H "Authorization: Bearer $TOKEN" "$SINK/v1/transcript/<id>"
curl -fsS -H "Authorization: Bearer $TOKEN" "$SINK/v1/transcript/<id>/chunks"
```

`GET /v1/transcript` supports filters: `hivemind_id`, `session_id`, `author`,
`tag`, `since`/`until`, `limit`, `cursor`. The `voxterm-sink-upload` CLI doesn't have
a read subcommand yet — reads go through the HTTP API directly as above.

> v1 read auth is a single shared secret (the spec's labeled placeholder, replacing
> the `1234` default). It gates the read surface but is **not** per-member auth —
> that's the deferred coordinator/capability-token work (spec §12).

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

shape-rotator is a VoxTerm hivemind: mesh gossip with no coordinator, where every
member holds a complete local replica and syncs when peers are reachable. The gap is
liveness — it assumes someone is online. When every laptop in the cohort is closed, a
member who joins late or reconnects after a week has no peer to sync from. This sink
is the peer that never sleeps — not a coordinator, just one more node in the
shape-rotator mesh that stays online to accept gossiped entries and serve backfill to
a member who returns.

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
