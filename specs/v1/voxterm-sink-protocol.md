# VoxTerm Sink Protocol

| Field | Value |
|---|---|
| **Spec** | VoxTerm Sink Protocol (VSP) |
| **Version** | `1.0.0-draft.1` |
| **Wire identifier** | `voxterm-sink/1` |
| **Status** | Draft (open for contribution) |
| **Target runtime** | Dstack on Intel TDX (Phala / Flashbots / Linux Foundation) |
| **Date** | 2026-05-30 |
| **Requires** | VoxTerm Hivemind Mode scoping (`VoxTerm/docs/hivemind-scoping.md`) |

> This document is a specification, not an implementation. It defines the wire protocol, data model, attestation procedure, and APIs precisely enough that an independent team can ship a conforming sink and a conforming VoxTerm client without further coordination. Where v1 deliberately punts on a hard problem, the punt is stated as a normative `MUST` against a placeholder and the real design is captured in the roadmap.

---

## 1. Conventions

The key words **MUST**, **MUST NOT**, **REQUIRED**, **SHALL**, **SHALL NOT**, **SHOULD**, **SHOULD NOT**, **RECOMMENDED**, **MAY**, and **OPTIONAL** are to be interpreted as described in RFC 2119 and RFC 8174.

- A **conforming sink** is a server that implements §7 (API) and §5 (attestation) at the version it advertises.
- A **conforming client** is software (VoxTerm, an agent, a CLI) that implements the §6 verification procedure before it streams, and the §7 calls it uses.
- Byte strings are lowercase hex unless stated otherwise. Timestamps are RFC 3339 / ISO 8601 with explicit timezone, always UTC (`Z`). Hashes are BLAKE3-256 unless stated otherwise, rendered as 64 lowercase hex chars. Identities are Ed25519 unless stated otherwise.
- Canonical bytes for hashing and signing are produced by **JCS** (RFC 8785, JSON Canonicalization Scheme) over the object with the `id` and `signature` fields removed. A conforming implementation MUST produce identical canonical bytes for identical logical content.
- All sizes are decimal bytes. `KiB`/`MiB` are powers of two.

---

## 2. Abstract

VoxTerm Hivemind Mode is mesh-gossiped, peer-to-peer transcript sharing across a trusted group. Every member holds a full local replica and backfills from whichever peers are online. The gap is liveness: when every laptop is closed, a member who joins late or reconnects after a week has no peer to sync from.

The VoxTerm Sink Protocol defines an **always-on, attested data sink**: one network endpoint that joins the mesh as a node which never sleeps. It accepts a live transcript-chunk stream from VoxTerm clients, persists transcripts, and serves them back on request. The sink runs inside a Trusted Execution Environment (TEE) deployed via **Dstack** on Intel TDX. Because the sink is always-on it sees everything that flows through it, so the protocol does not ask clients to trust the operator. Clients **verify the enclave** by remote attestation before they push, and the confidentiality boundary in v1 is the TEE itself.

This is the answer to the bootstrap-relay-operator centralization tradeoff named in the Hivemind scoping doc: a node that holds the data without the operator holding the keys to it.

```
                              "verify it's a TEE"  (orange)
                                       │
  ┌───────────── laptop #1 ─────────┐  │     ┌─────────── TEE (Intel TDX) ───────────┐
  │  VoxTerm                        │  ▼     │   Dstack                              │
  │   └─ Hivemind ──auth + transcript chunk stream──►  voxterm-data-sink            │
  └─────────────────────────────────┘        │        ▲          ▲                  │
                                              │        │ auth     │ fetch data       │
  ┌──────────── laptop #2 ──────────┐         │        │          │                  │
  │  VoxTerm                        │         └────────┼──────────┼──────────────────┘
  │   └─ Hivemind ──auth + stream───┼──────────────────┘          │
  └─────────────────────────────────┘                            │
                                                       ┌──────────┴───────────┐
   auth levels:                                        │  Intel  (PCS / DCAP) │
     cohort ⊇ coordinator                              │  TCB info, PCK chain,│
                                                       │  CRLs, QE identity   │
   API:  /auth                                         └──────────────────────┘
         /transcript  [POST, GET]
```

---

## 3. Goals and non-goals

### 3.1 Goals (v1)

1. A VoxTerm client can add a sink by URL, **verify it is a genuine TEE running the expected sink code**, and only then stream to it.
2. A client can **POST** transcript chunks live during a session and/or a finalized transcript when a session ends. Whether a client streams live or posts once on finalize is an implementation choice; the sink MUST support both.
3. A reader can **GET** transcripts back. v1 read authorization is a single shared static secret (the placeholder `"1234"`), not real auth.
4. The data model and API are **versioned and additive-only** so the protocol can evolve without breaking old clients or old data.
5. Attestation is **sound and reproducible**: the verification procedure is fully specified, uses standard Intel DCAP collateral, and binds the network channel to the attested key.

### 3.2 Non-goals (v1, deferred to roadmap §12)

- Real multi-tier authentication and authorization (the `cohort` / `coordinator` lattice of §8 is *defined* but only partially *enforced* in v1).
- End-to-end payload encryption under a hivemind group key (the sink stores what it receives; confidentiality in v1 is the TEE boundary, see §10).
- Mesh gossip *between* sinks, deletion/tombstone propagation, and full Hivemind entry reconciliation.
- On-chain KMS governance policy authoring (the sink runs under Dstack KMS but v1 does not specify the governance contract content).

---

## 4. Roles and terminology

| Term | Meaning |
|---|---|
| **Sink** | The `voxterm-data-sink` service instance running inside a TDX CVM under Dstack. |
| **Client** | VoxTerm (or any conforming pusher/reader). |
| **Cohort** | The trust group whose transcripts a sink holds. Equivalent to a Hivemind membership. |
| **Coordinator** | An elevated subset of the cohort with administrative capabilities (key rotation, deletion, retention config). Coordinators ⊆ cohort. |
| **Session** | One VoxTerm recording session, identified by `session_id` (`YYYY-MM-DD_HHMMSS` per VoxTerm convention), scoped to one author. |
| **Chunk** | One incremental unit of a live transcript stream (see §5.2 of Hivemind: party mode feeds hivemind; a chunk is finer-grained than a Hivemind entry). |
| **Transcript** | A finalized, assembled readout for a session, the unit that maps onto a Hivemind `Entry` of `content_type ∈ {transcript, readout, summary, note}`. |
| **Quote** | An Intel TDX DCAP attestation quote produced by the dstack guest agent. |
| **Measurement** | The set `(MRTD, RTMR0, RTMR1, RTMR2, RTMR3)`, each a SHA-384 register value, that identifies what code is running. |
| **Collateral** | Intel-rooted data needed to verify a quote: PCK cert chain, TCB info, QE identity, CRLs. |
| **RA-TLS** | TLS where the server certificate's public key is bound into the attestation `report_data`. |

---

## 5. The sink and its TEE substrate (Dstack on TDX)

This section is informative where it describes Dstack and normative where it states what a conforming sink MUST expose.

### 5.1 Deployment shape

The sink ships as a Docker image referenced from an `app-compose.json` and is deployed by `dstack-vmm` into a Confidential VM (CVM / TD). Inside the TD it talks to the **dstack guest agent** over the Unix socket `/var/run/dstack.sock` (legacy `/var/run/tappd.sock` for older images; a conforming sink SHOULD prefer the `dstack` socket and MAY fall back).

Relevant Dstack components (informative):

- **dstack-vmm**: untrusted host orchestrator; boots the CVM from a reproducible OS image and parses `app-compose.json`.
- **dstack-kms**: derives per-app keys bound to the app's attested identity, gated by on-chain authorization (an allowlist of compose hashes). The sink uses KMS-derived keys for storage sealing (§5.5) and MAY use a KMS-derived key as its long-term signing identity.
- **dstack-gateway**: edge reverse proxy; terminates public ACME TLS and speaks RA-TLS internally.
- **dstack guest agent**: in-TD agent exposing quote generation and key derivation to the sink.

### 5.2 Sink identity keys

On first boot a conforming sink MUST establish two keypairs:

1. **`sink_sig` (Ed25519)**: the long-term signing identity of this sink instance. It MUST be derived deterministically from the app identity so it survives restarts and upgrades, by calling the guest agent key derivation:
   ```
   get_key(path="voxterm-sink/v1/sig", purpose="signing", algorithm="ed25519")
   ```
2. **`sink_dh` (X25519)** (OPTIONAL in v1, REQUIRED if §10.2 payload encryption is enabled): a key-agreement key, derived at `path="voxterm-sink/v1/dh"`.

The public halves are published in the attestation bundle (§5.4) and at `GET /v1/info`.

### 5.3 What the measurement means (informative)

A TDX quote carries one MRTD and four RTMRs (each SHA-384):

| Register | Measures | Precomputable? |
|---|---|---|
| `MRTD` | Virtual firmware (OVMF), the TD trust anchor | Yes, from the dstack base image |
| `RTMR0` | Virtual hardware config (vCPU, memory, devices) | Yes, given the VM spec |
| `RTMR1` | Linux kernel | Yes, from the base image |
| `RTMR2` | Kernel cmdline (includes rootfs hash) + initrd | Yes, from the base image |
| `RTMR3` | **Application register**: `compose-hash`, `app-id`, `instance-id`, `key-provider` extended as named events | Verified by replaying the event log |

The **app-compose / docker-compose hash is extended into RTMR3**. `compose-hash = SHA-256(app-compose.json)`; `app-id = first 20 bytes of SHA-256(app-compose.json)`. RTMR3 is verified by replaying the event log (`RTMR_new = SHA384(RTMR_old || SHA384(event_payload))` iterated over events) and checking the result equals the quote's RTMR3. The `dstack-sdk` `replay_rtmrs()` does this.

> Implementer note: Phala's attestation-hardening work is removing RTMR0–RTMR2 event-log entries in favor of digest-only verification of those registers. A conforming verifier MUST therefore treat RTMR0–RTMR2 as compared-against-expected-digests and MUST drive RTMR3 trust from the event-log replay. Do not depend on RTMR0–2 event-log entries existing.

### 5.4 The attestation bundle

A conforming sink MUST serve an attestation bundle at `GET /v1/attestation` (§7.3). The bundle is produced as follows.

On request the sink computes:

```
report_data = BLAKE3-512(
    "voxterm-sink/1\x00" ||
    sink_sig_pubkey (32 bytes) ||
    sink_dh_pubkey (32 bytes, or 32 zero bytes if absent) ||
    nonce (client-supplied, 32 bytes, or 32 zero bytes if absent)
)                                   # 64 bytes, fits TDX REPORTDATA exactly
```

It then calls the guest agent:

```python
q = dstack_client.get_quote(report_data)   # report_data is exactly 64 bytes
bundle = {
    "schema_version": "1",
    "wire": "voxterm-sink/1",
    "quote": q.quote,                 # hex TDX DCAP quote
    "event_log": q.event_log,         # JSON event log for RTMR replay
    "report_data_construction": {     # so a verifier can recompute report_data
        "algo": "blake3-512",
        "domain": "voxterm-sink/1",
        "fields": ["sink_sig_pubkey", "sink_dh_pubkey", "nonce"]
    },
    "sink_sig_pubkey": "<hex ed25519 32B>",
    "sink_dh_pubkey": "<hex x25519 32B | null>",
    "nonce": "<hex 32B | null>",      # echoes client nonce if provided
    "app_id": "<hex 20B>",
    "instance_id": "<hex>",
    "compose_hash": "<hex sha256 of app-compose.json>",
    "kms": { "root_pubkey": "<hex | null>", "signature_chain": ["<hex>", ...] },
    "produced_at": "<RFC3339>"
}
```

The bundle MUST be fresh: if the client supplied a `nonce`, the quote MUST embed it via `report_data`, and the sink MUST NOT serve a cached quote for a nonced request.

### 5.5 Storage sealing (informative)

Dstack encrypts the CVM data volume with LUKS2 using a key provisioned by KMS and bound to the app's attested identity (app-id + measurements). Data therefore survives reboots and migration. On upgrade, key continuity is preserved only if the new `compose-hash` is added to the KMS on-chain allowlist (`allowedComposeHashes`) before/at upgrade; otherwise KMS refuses key release and sealed data becomes inaccessible. A conforming deployment SHOULD document its compose-hash allowlist policy in its `GET /v1/info` `governance` field.

---

## 6. Client verification procedure ("verify it's a TEE")

This is the load-bearing security procedure. A conforming client MUST perform it before the first push to a sink, and SHOULD re-perform it on a schedule (§6.4).

### 6.1 Inputs

- `sink_url`: the base URL the user added.
- `nonce`: 32 fresh random bytes generated by the client.
- `policy`: a measurement policy (§6.3), either pinned expected values or trust-on-first-use (TOFU).
- Collateral access: either a local DCAP verifier with a PCCS/PCS endpoint, or an Intel Trust Authority (ITA) endpoint. The "Intel" node on the whiteboard is this collateral / verification root: the client (or its verifier service) fetches PCK chain, TCB info, QE identity, and CRLs from Intel PCS (or a PCCS cache, e.g. `https://pccs.phala.network`) to validate the quote.

### 6.2 Steps (normative)

1. `GET {sink_url}/v1/attestation?nonce={hex(nonce)}` over ordinary TLS. Receive the bundle (§5.4).
2. **Decode and structurally validate** the quote as a TDX DCAP quote.
3. **Verify the quote** against Intel-rooted collateral (DCAP). The client MUST:
   - validate the PCK certificate chain to the Intel SGX Root CA and check CRLs;
   - verify the quote signature with the PCK key;
   - verify the QE report against QE Identity;
   - map the platform TCB against TCB Info and obtain a TCB status.
   The client MUST reject status `Revoked`. The client policy (§6.3) decides whether `OutOfDate` / `ConfigurationNeeded` are acceptable; the default policy MUST reject anything other than `UpToDate` unless the user has explicitly opted into a laxer policy. Equivalent: submit the quote to ITA and require a valid signed token.
4. **Replay the event log** and confirm the recomputed RTMR3 equals the quote's RTMR3. Extract `compose-hash`, `app-id`, `instance-id` from the replayed events.
5. **Channel binding**: recompute `report_data` from `sink_sig_pubkey`, `sink_dh_pubkey`, and the `nonce` the client sent (per §5.4) and confirm it equals the `REPORTDATA` field in the verified quote. This proves the quote was produced by a TD that holds `sink_sig` and proves freshness (the client's nonce is inside the signed quote).
6. **Measurement policy** (§6.3): compare `(MRTD, RTMR0, RTMR1, RTMR2)` and the RTMR3 `compose-hash` against the policy.
7. If all pass, the client records the **verified sink identity** `(sink_url, sink_sig_pubkey, MRTD, RTMR0..2, compose_hash, app_id)` and MAY proceed to push. If any step fails, the client MUST refuse to push and MUST surface which check failed.

### 6.3 Measurement policy

A conforming client MUST support both modes and default to TOFU for v1 ergonomics:

- **Pinned**: the client is configured with expected `(MRTD, RTMR0, RTMR1, RTMR2)` for a known dstack base-image version and an expected `compose_hash` for a published `voxterm-data-sink` release (reproducible build). All MUST match. This is RECOMMENDED for production cohorts and REQUIRED for any sink that stores sensitive transcripts.
- **TOFU (trust on first use)**: on the first successful verification the client records the measurement set and `sink_sig_pubkey`. On every subsequent verification it MUST compare against the recorded set and MUST warn loudly and refuse to push if the measurement or signing key changed without an operator-announced upgrade.

Maintainers of `voxterm-data-sink` SHOULD publish, per release: the `compose_hash`, the expected `MRTD/RTMR0..2` for each supported dstack base image, and the reproducible build instructions, in a machine-readable `measurements.json` (§ Appendix B).

### 6.4 Channel use and re-verification

After verification the client streams over the public TLS endpoint terminated by dstack-gateway. Because the sink's application identity `sink_sig_pubkey` is attestation-bound, the client SHOULD pin it: every response from the sink that carries data MUST be servable under a transport whose key chains to, or is co-authenticated by, the attested `sink_sig` (see §7.1 response signing). A client SHOULD re-run §6.2 at least every 24h and on any `sink_sig_pubkey` change, treating a silent change as a trust failure.

---

## 7. API specification (`voxterm-sink/1`)

### 7.1 Common conventions

- **Base path**: all v1 endpoints are under `/v1`. The protocol is versioned in the path; a future incompatible revision is `/v2`. Additive changes (new optional fields, new endpoints) stay within `/v1`.
- **Negotiation**: requests SHOULD send `X-Sink-Protocol: voxterm-sink/1`. Responses MUST send it. A sink that receives an unknown major version MUST reply `400` with error code `unsupported_protocol`.
- **Content types**: JSON bodies are `application/json`. The live chunk stream uses newline-delimited JSON, `application/x-ndjson` (§7.5). All JSON MUST be UTF-8.
- **IDs**: `transcript.id` and `chunk` identity are content-addressed (§9). The sink MUST reject a body whose recomputed `id` does not match a supplied `id`.
- **Idempotency**: writes are idempotent by content address (transcripts) or by `(session_id, author, seq)` (chunks). Re-POSTing the same content MUST return `200` with the existing resource rather than duplicating.
- **Response signing**: every data-bearing response (`/attestation`, `/transcript` reads, `/info`) MUST include header `X-Sink-Signature: ed25519:<hex>` over `BLAKE3(canonical_response_body)`, produced with `sink_sig`. This lets a client bind responses to the attested identity even though public TLS is terminated at the gateway. Clients SHOULD verify it.
- **Errors**: non-2xx responses use this body:
  ```json
  { "error": { "code": "string_enum", "message": "human readable", "detail": {} } }
  ```
  Error codes: `unsupported_protocol`, `unauthorized`, `forbidden`, `not_found`, `bad_request`, `schema_mismatch`, `id_mismatch`, `payload_too_large`, `rate_limited`, `attestation_unavailable`, `internal`.
- **Limits**: the sink MUST advertise `max_chunk_bytes`, `max_transcript_bytes`, and rate limits in `GET /v1/info`. Default RECOMMENDED caps: chunk 64 KiB, transcript 16 MiB. The sink MUST reply `413 payload_too_large` when exceeded.
- **Time**: the sink MUST include `Date` and SHOULD reflect a monotonic `X-Sink-Seq` high-water mark per session on chunk writes.
- **CORS**: the sink SHOULD allow cross-origin reads for browser-based agents on `GET` endpoints; write endpoints SHOULD NOT be CORS-open.

### 7.2 Endpoint summary

| Method | Path | Auth (v1) | Purpose |
|---|---|---|---|
| `GET` | `/v1/info` | public | Capabilities, limits, identity, governance |
| `GET` | `/v1/health` | public | Liveness |
| `GET` | `/v1/attestation` | public | TEE attestation bundle (§5.4) |
| `POST` | `/v1/auth` | n/a | Exchange a secret for a bearer token (§8) |
| `POST` | `/v1/transcript` | write | Push a finalized transcript (one object) |
| `POST` | `/v1/transcript/stream` | write | Push a live NDJSON chunk stream (§7.5) |
| `GET` | `/v1/transcript` | read (`1234`) | List/query transcripts (metadata) |
| `GET` | `/v1/transcript/{id}` | read (`1234`) | Fetch one full transcript |
| `GET` | `/v1/transcript/{id}/chunks` | read (`1234`) | Fetch the chunk log for a transcript/session |

### 7.3 `GET /v1/attestation`

Query params: `nonce` (OPTIONAL, 32-byte hex; RECOMMENDED for freshness).

`200` → the attestation bundle of §5.4. Public, no auth. If the guest agent is unreachable the sink MUST reply `503 attestation_unavailable` rather than a fabricated bundle.

### 7.4 `POST /v1/transcript`

Push one finalized transcript (the "push it there once it's done" path). Body is a `Transcript` (§9.2).

- Auth (v1): write tier (§8). In v1 the write tier is open over the attested channel; the sink MAY additionally require a bearer token.
- The sink MUST recompute `id = BLAKE3(JCS(transcript \ {id, signature}))` and reject mismatch with `409 id_mismatch`.
- If `signature` is present the sink MUST verify it against `author`; if absent, v1 accepts it (signing is OPTIONAL in v1, REQUIRED in roadmap §12).
- `201` → `{ "id": "<hex>", "url": "/v1/transcript/<id>", "stored_at": "<RFC3339>" }`. Re-POST of identical content → `200` with the same body.

### 7.5 `POST /v1/transcript/stream`

The live "transcript chunk stream" from the whiteboard. The request body is an open, chunked `application/x-ndjson` stream: one `TranscriptChunk` (§9.1) per line, flushed as produced. The sink consumes lines as they arrive and persists each chunk durably before acknowledging.

- The stream MUST begin with a single `StreamHeader` line (§9.3) declaring `session_id`, `author`, `hivemind_id`, and `sink_id`; subsequent lines are chunks with monotonically increasing `seq` starting at 0.
- A chunk with `"is_final": true` closes the session's stream; the sink then MAY assemble a `Transcript` from the accumulated chunks (server-side assembly, §9.4) or wait for an explicit `POST /v1/transcript`.
- Acknowledgement: the sink streams back `application/x-ndjson` ack lines `{ "ack_seq": n, "stored": true }` so a client can resume. On reconnect the client queries the high-water mark (`X-Sink-Seq` on a `HEAD`/`GET /v1/transcript/{session→id}/chunks?since_seq=`) and resumes at the next `seq`. Chunks are idempotent by `(session_id, author, seq)`.
- Backpressure: the sink MAY apply flow control; clients MUST tolerate slow acks and MUST NOT drop unacked chunks.
- WebSocket transport at `GET /v1/transcript/ws` (upgrade) is an OPTIONAL alternative carrying the same `StreamHeader` + chunk frames; NDJSON-over-POST is the REQUIRED baseline because it traverses the gateway with no special support.

### 7.6 `GET /v1/transcript`

List/query transcript metadata. Read tier (v1: the `1234` secret, §8).

Query params (all OPTIONAL, AND-combined): `hivemind_id`, `session_id`, `author`, `content_type`, `tag` (repeatable), `since` (RFC3339), `until`, `limit` (default 50, max 500), `cursor` (opaque pagination token).

`200` →
```json
{
  "items": [ { "id": "...", "session_id": "...", "author": "...",
               "content_type": "readout", "title": "...", "tags": ["..."],
               "created_at": "...", "stored_at": "...", "bytes": 1234,
               "chunk_count": 42, "url": "/v1/transcript/..." } ],
  "next_cursor": "opaque | null"
}
```

### 7.7 `GET /v1/transcript/{id}` and `/chunks`

- `GET /v1/transcript/{id}` → the full `Transcript` (§9.2). `404 not_found` if absent.
- `GET /v1/transcript/{id}/chunks?since_seq=N` → `application/x-ndjson` of the underlying `TranscriptChunk`s with `seq > N` (the raw stream, for incremental readers / agents tailing a session). Read tier.

### 7.8 `GET /v1/info` and `/v1/health`

`GET /v1/health` → `200 {"status":"ok"}`, public, cheap.

`GET /v1/info` → public:
```json
{
  "wire": "voxterm-sink/1",
  "spec_version": "1.0.0-draft.1",
  "sink_id": "<hex>",
  "sink_sig_pubkey": "<hex>",
  "sink_dh_pubkey": "<hex | null>",
  "app_id": "<hex 20B>",
  "compose_hash": "<hex>",
  "hiveminds": ["<hivemind_id>", "..."],
  "limits": { "max_chunk_bytes": 65536, "max_transcript_bytes": 16777216,
              "rate_per_min": 600 },
  "retention": { "policy": "keep | ttl", "ttl_days": null },
  "auth": { "read": "shared-secret-v1", "write": "open-attested-v1",
            "levels": ["public","cohort","coordinator"] },
  "governance": { "kms_root_pubkey": "<hex|null>",
                  "allowed_compose_hashes_ref": "<url|null>" },
  "build": { "release": "vX.Y.Z", "measurements_ref": "<url to measurements.json>" }
}
```

---

## 8. Authentication and authorization

> v1 deliberately ships the easy half. The capability *lattice* below is normative and stable; v1 *enforcement* is intentionally trivial. This lets cohorts evolve auth without a wire break.

### 8.1 Capability lattice (the whiteboard "auth levels")

Access tiers are monotonic: `coordinator ⊇ cohort ⊇ public`.

| Tier | Capabilities |
|---|---|
| `public` | `GET /v1/health`, `GET /v1/info`, `GET /v1/attestation` |
| `cohort` | everything in `public`, plus read transcripts/chunks and write (push) |
| `coordinator` | everything in `cohort`, plus admin: rotate the read secret, register/evict author keys, set retention, delete/tombstone transcripts, configure allowed hiveminds |

A coordinator is a member of the cohort with elevated rights, exactly as drawn (coordinators nested inside the cohort).

### 8.2 `POST /v1/auth`

Body: `{ "tier": "cohort" | "coordinator", "secret": "string" }`.
`200` → `{ "token": "<opaque bearer>", "tier": "cohort", "expires_at": "<RFC3339>" }`.
The token is sent as `Authorization: Bearer <token>` on subsequent calls.

### 8.3 v1 enforcement (the placeholder)

- **Read** (`GET /v1/transcript*`): gated by a single shared static secret, the placeholder `"1234"`, exchanged at `POST /v1/auth {"tier":"cohort","secret":"1234"}` for a short-lived `cohort` token. This is **not** real authentication and the spec says so out loud; it exists so the read surface has a seam to upgrade. The secret MUST be operator-configurable (env `VOXTERM_SINK_READ_SECRET`, default `1234`) and the sink MUST log a startup warning if it is left at the default.
- **Write** (`POST /v1/transcript*`): open over the attested channel in v1. The client has already verified the TEE; the sink accepts pushes. The sink MAY require a `cohort` token but is not REQUIRED to in v1.
- **Coordinator**: defined, not enforced in v1. A v1 sink MAY hardcode a single coordinator secret via env for manual admin; full coordinator auth is roadmap §12.

### 8.4 Forward path (informative)

The lattice is designed to be filled by, in roughly increasing strength: per-author Ed25519 keys registered by a coordinator (write = valid signature by a registered author); capability tokens issued by coordinators; hivemind group-key possession; and eventually the Hivemind membership model (possession of `hivemind_key` + registered author pubkey). None of these require an API break because `POST /v1/auth` already returns a tier and tokens are opaque.

---

## 9. Data model

All objects carry `schema_version: "1"`. **Evolution is additive-only**: new versions MAY add OPTIONAL fields; they MUST NOT remove fields, change a field's type, or change the meaning of an existing field. A reader MUST ignore unknown fields. A writer MUST NOT depend on the reader understanding fields beyond the `schema_version` it declares. This guarantees graceful behavior in both directions (old client ↔ new sink, new client ↔ old sink, old data ↔ new reader).

### 9.1 `TranscriptChunk`

One incremental unit in a live stream.

```jsonc
{
  "schema_version": "1",
  "sink_id":     "<hex>",            // the sink this stream targets
  "hivemind_id": "<uuid>",           // group this belongs to
  "session_id":  "2026-05-30_141503",// VoxTerm session id (per-author scope)
  "author":      "<hex ed25519 pubkey>",
  "seq":         0,                   // monotonic per (session_id, author), starts at 0
  "created_at":  "2026-05-30T14:15:03.250Z",
  "is_final":    false,               // true closes the session stream
  "text":        "so the way I see it",
  "t_start":     12.40,               // seconds from session start
  "t_end":       14.10,
  "speaker":     { "local_id": 2, "label": "Marcus" },  // label OPTIONAL
  "lang":        "en",                // OPTIONAL
  "confidence":  0.91,                // OPTIONAL, 0..1
  "revises_seq": null,                // OPTIONAL: this chunk supersedes an earlier seq (ASR correction)
  "tags":        [],                  // OPTIONAL
  "signature":   "ed25519:<hex>"      // OPTIONAL in v1, over JCS(chunk \ {signature})
}
```

Chunk identity for idempotency is `(session_id, author, seq)`. `revises_seq` lets streaming ASR replace a previously sent chunk without breaking the append-only log: the latest chunk for a given `seq` wins in assembled views, all are retained in `/chunks`.

### 9.2 `Transcript`

A finalized, assembled readout. This is the unit that maps onto a Hivemind `Entry`.

```jsonc
{
  "schema_version": "1",
  "id":          "<hex blake3>",      // = BLAKE3(JCS(self \ {id, signature}))
  "sink_id":     "<hex>",
  "hivemind_id": "<uuid>",
  "session_id":  "2026-05-30_141503",
  "author":      "<hex ed25519 pubkey>",
  "content_type":"readout",           // transcript | readout | summary | note
  "created_at":  "2026-05-30T15:02:11Z",
  "title":       "weekly sync",       // OPTIONAL
  "tags":        ["shape-rotator"],
  "parent_ids":  [],                  // Hivemind threading: edits/replies
  "segments": [
    { "speaker": { "local_id": 2, "label": "Marcus" },
      "text": "so the way I see it ...", "t_start": 12.40, "t_end": 31.2,
      "lang": "en", "confidence": 0.90 }
  ],
  "markdown":    "## weekly sync\n\n**Marcus:** so the way I see it ...",  // OPTIONAL rendered view
  "source": {                         // provenance
    "tool": "voxterm", "tool_version": "0.x",
    "stream_chunk_count": 142, "finalized": true
  },
  "encryption":  null,                // OPTIONAL, see §10.2; null = stored as-is
  "signature":   "ed25519:<hex>"      // OPTIONAL in v1
}
```

### 9.3 `StreamHeader`

First line of a `POST /v1/transcript/stream` body.

```jsonc
{
  "schema_version": "1",
  "type": "stream_header",
  "sink_id": "<hex>", "hivemind_id": "<uuid>",
  "session_id": "2026-05-30_141503", "author": "<hex>",
  "started_at": "2026-05-30T14:15:00Z",
  "expected_final": false,            // hint: whether the client intends to send is_final
  "client": { "tool": "voxterm", "tool_version": "0.x" }
}
```

### 9.4 Server-side assembly

When a stream ends with `is_final: true` (or a `POST /v1/transcript` references a streamed `session_id`), the sink MAY assemble a `Transcript` by ordering retained chunks by `seq`, applying `revises_seq` (latest wins), grouping contiguous same-speaker chunks into `segments`, and rendering `markdown`. Assembly is deterministic so the assembled `id` is reproducible. A client MAY instead assemble locally and `POST /v1/transcript` the result; the sink MUST accept either.

### 9.5 Content addressing and Hivemind alignment

`Transcript.id` uses BLAKE3 over JCS canonical bytes, matching the Hivemind entry model's content addressing. `author` is Ed25519, matching Hivemind identities. A conforming sink-to-Hivemind bridge (out of scope for v1, see §12) maps a `Transcript` directly onto a Hivemind `Entry`: `id → Entry.id`, `author → Entry.author`, `content_type → Entry.content_type`, `tags`, `parent_ids`, `created_at`, and the canonical bytes become `Entry.payload`.

---

## 10. Privacy and confidentiality

### 10.1 v1 boundary: the TEE

In v1 the confidentiality boundary is the enclave. Transcripts are stored as the sink receives them (plaintext at rest inside the sealed, KMS-encrypted volume of §5.5). The operator cannot read them because the operator cannot enter the TD and cannot extract the sealing key (it is released by KMS only to the attested measurement). Clients get confidentiality by (a) verifying the TEE before pushing (§6) and (b) the sealed-storage property. Read access is the `1234` placeholder, so v1 confidentiality against *cohort outsiders* is weak by design and the spec says so: do not put a v1 sink on a hostile public network expecting read secrecy from the password alone. The real protection in v1 is "the operator cannot read it," not "outsiders cannot fetch it."

### 10.2 Optional payload encryption (forward-compatible hook)

The `Transcript.encryption` field reserves the seam for end-to-end encryption under a hivemind group key, so the sink stores ciphertext it cannot read even inside the TD:

```jsonc
"encryption": { "scheme": "aes-256-gcm", "kid": "<hivemind_key id>",
                "nonce": "<hex>", "aad": "<hex>" }
```

When present, `segments`/`markdown`/`text` carry ciphertext and the sink treats them as opaque. This aligns with Hivemind's AES-256-GCM group-key model. v1 sinks MUST round-trip the field untouched even if they do not originate it; full E2E key management is roadmap §12.

### 10.3 What the sink may learn

Even with §10.2, the sink observes envelope metadata: `hivemind_id`, `author`, `session_id`, timing, sizes. The threat model (§11) treats this as acceptable for v1; metadata privacy is not a v1 goal.

---

## 11. Security considerations

1. **Attestation freshness.** A quote without a client nonce can be replayed. Clients SHOULD always send a `nonce` to `/v1/attestation` and MUST verify it appears in `report_data` (§6.2 step 5). A sink MUST NOT serve a cached quote for a nonced request.
2. **Channel binding.** Public TLS is terminated at dstack-gateway, not in the TD. Binding the application key `sink_sig` into `report_data` and signing responses with it (`X-Sink-Signature`) is what ties responses to the attested code despite gateway termination. Clients SHOULD verify response signatures.
3. **Measurement pinning vs TOFU.** TOFU accepts whatever runs the first time; a malicious first contact defeats it. Production cohorts MUST pin (§6.3). A measurement change without an announced upgrade MUST be treated as compromise.
4. **TCB status.** Accepting `OutOfDate` TCB undermines the hardware guarantee. Default policy rejects non-`UpToDate`; laxer policies require explicit user opt-in.
5. **KMS upgrade governance.** Sealed data continuity across upgrades depends on the new `compose_hash` being allowlisted in the KMS governance contract. A compromised governance key can introduce a malicious measurement that still decrypts old data. Cohorts SHOULD monitor the allowlist and pin the governance root in policy.
6. **The `1234` secret is not security.** It is a labeled placeholder. The spec mandates a startup warning and an env override. Do not rely on it for confidentiality. See §12 for the real auth path.
7. **Write abuse.** Open write over the attested channel means any client who verified the TEE can push. v1 sinks SHOULD rate-limit and SHOULD cap per-session and per-author volume. Author-signed writes (§8.4) are the mitigation in the roadmap.
8. **Denial of service.** The sink is a single always-on node; it is a DoS target. Operators SHOULD front it with gateway-level rate limiting and per-IP caps and SHOULD treat availability as best-effort (the mesh remains the source of truth; the sink is a convenience peer, not an authority).
9. **Reproducible builds.** Pinned measurement policy is only as good as the ability to reproduce `compose_hash` and base-image measurements. Maintainers MUST publish reproducible build instructions and `measurements.json`.

---

## 12. Roadmap (deferred, contributions welcome)

Ordered by dependency, not priority. Each item is a candidate for a follow-up minor version within `/v1` (additive) or a `/v2` where a break is unavoidable.

1. **Author-signed writes.** Require valid Ed25519 `signature` on chunks/transcripts; coordinators register author pubkeys. Additive (`signature` already reserved).
2. **Real read auth.** Replace the `1234` shared secret with per-member capability tokens; keep `POST /v1/auth` shape. Additive.
3. **Coordinator enforcement.** Implement the admin capabilities (rotate secret, evict author, retention, tombstone). Mostly additive (new endpoints under `/v1/admin`).
4. **E2E payload encryption.** Activate `Transcript.encryption`; integrate hivemind group keys (AES-256-GCM), `sink_dh` for any sink-assisted rewrap. Additive.
5. **Tombstones and deletion.** Honor Hivemind tombstone semantics in sink views.
6. **Hivemind bridge.** The sink joins the iroh-docs mesh as a real Hivemind node, gossips `Transcript`→`Entry`, serves backfill to peers (§9.5 mapping). This is the original "always-on peer" promise end-to-end.
7. **Multi-sink federation.** Sinks gossip to each other for redundancy; deduplicate by content address.
8. **On-chain governance docs.** Specify the KMS `DstackApp` allowlist policy a cohort SHOULD adopt.
9. **Metadata privacy.** Reduce envelope leakage (padded sizes, batched timing) for cohorts that need it.

---

## 13. Versioning and contribution process

- **Spec version** (`1.0.0-draft.N`) follows semver. Patch/minor bumps are additive and stay on wire `voxterm-sink/1`. A major bump that breaks the wire becomes `voxterm-sink/2` and `/v2`.
- **Schema version** (`"1"`) bumps only on an additive schema generation; the additive-only rule of §9 holds across all `"1".x` schemas. A type change or field removal requires `"2"` and is a wire break.
- **Changes** are proposed as PRs against this file in `voxterm-transcript-sink`. A change MUST state: which section it amends, whether it is additive or breaking, and its effect on each compatibility direction (old↔new client, old↔new sink, old↔new data). Breaking changes MUST justify why an additive path was not possible.
- **Status flow**: `Draft` → `Review` → `Stable`. A version reaches `Stable` only with at least one conforming sink implementation and one conforming client implementation interoperating, plus published `measurements.json` for at least one release.

---

## Appendix A. Reference Python interfaces

Informative. These are reference signatures, not mandated code. Models shown with Pydantic; server with FastAPI; verification leans on `dstack-sdk` and `dcap-qvl`.

### A.1 Data models

```python
from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field

ContentType = Literal["transcript", "readout", "summary", "note"]

class Speaker(BaseModel):
    local_id: int
    label: Optional[str] = None

class TranscriptChunk(BaseModel):
    schema_version: Literal["1"] = "1"
    sink_id: str
    hivemind_id: str
    session_id: str
    author: str                       # hex ed25519 pubkey
    seq: int = Field(ge=0)
    created_at: str                   # RFC3339 UTC
    is_final: bool = False
    text: str
    t_start: float
    t_end: float
    speaker: Speaker
    lang: Optional[str] = None
    confidence: Optional[float] = None
    revises_seq: Optional[int] = None
    tags: list[str] = []
    signature: Optional[str] = None   # "ed25519:<hex>" over JCS(self \ {signature})

class Segment(BaseModel):
    speaker: Speaker
    text: str
    t_start: float
    t_end: float
    lang: Optional[str] = None
    confidence: Optional[float] = None

class Encryption(BaseModel):
    scheme: Literal["aes-256-gcm"]
    kid: str
    nonce: str
    aad: Optional[str] = None

class Transcript(BaseModel):
    schema_version: Literal["1"] = "1"
    id: str                           # blake3(JCS(self \ {id, signature}))
    sink_id: str
    hivemind_id: str
    session_id: str
    author: str
    content_type: ContentType = "readout"
    created_at: str
    title: Optional[str] = None
    tags: list[str] = []
    parent_ids: list[str] = []
    segments: list[Segment]
    markdown: Optional[str] = None
    source: dict = {}
    encryption: Optional[Encryption] = None
    signature: Optional[str] = None
```

### A.2 Server-side sink surface (FastAPI sketch)

```python
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

app = FastAPI()

class Sink:
    """A conforming sink. Storage and KMS wiring elided."""

    def info(self) -> dict: ...
    def health(self) -> dict: ...

    # --- attestation -------------------------------------------------
    def attestation(self, nonce: bytes | None) -> dict:
        report_data = self._report_data(nonce)          # §5.4, 64 bytes
        q = self.dstack.get_quote(report_data)           # dstack-sdk
        return self._bundle(q, nonce)

    # --- writes ------------------------------------------------------
    def put_transcript(self, t: Transcript) -> dict:
        recomputed = blake3(jcs(t, drop={"id", "signature"})).hexdigest()
        if recomputed != t.id:
            raise HTTPException(409, "id_mismatch")
        if t.signature:
            verify_ed25519(t.author, t.signature, jcs(t, drop={"signature"}))
        return self.store.upsert_transcript(t)           # idempotent by id

    async def ingest_stream(self, lines):                # NDJSON
        header = StreamHeader.model_validate_json(await lines.__anext__())
        async for line in lines:
            chunk = TranscriptChunk.model_validate_json(line)
            self.store.upsert_chunk(chunk)               # idempotent by (sid,author,seq)
            yield {"ack_seq": chunk.seq, "stored": True}
            if chunk.is_final:
                self.maybe_assemble(header.session_id, header.author)

    # --- reads (gated by §8 read tier) -------------------------------
    def list_transcripts(self, **q) -> dict: ...
    def get_transcript(self, id: str) -> Transcript: ...
    def get_chunks(self, id: str, since_seq: int): ...    # NDJSON
```

### A.3 Client verification (`verify it's a TEE`)

```python
import os, secrets, httpx
from dstack_sdk import DstackClient          # server side uses this; client verifies the output
from dcap_qvl import verify_quote            # Phala dcap-qvl bindings or CLI shim

class SinkClient:
    def __init__(self, base_url: str, policy: "MeasurementPolicy"):
        self.base = base_url.rstrip("/")
        self.policy = policy
        self.identity = None                  # set after verify()

    def verify(self) -> "VerifiedSink":
        nonce = secrets.token_bytes(32)
        b = httpx.get(f"{self.base}/v1/attestation",
                      params={"nonce": nonce.hex()},
                      headers={"X-Sink-Protocol": "voxterm-sink/1"}).json()

        report = verify_quote(                # DCAP: PCK chain + TCB + QE + CRLs
            bytes.fromhex(b["quote"]),
            collateral="https://pccs.phala.network",   # or Intel PCS / ITA
        )
        require(report.tcb_status == "UpToDate" or self.policy.allow_stale)

        rtmrs = replay_event_log(b["event_log"])        # recompute, esp. RTMR3
        require(rtmrs[3] == report.rtmr3)
        compose_hash = rtmrs.event("compose-hash")

        expected = report_data(                          # §5.4 reconstruction
            b["sink_sig_pubkey"], b.get("sink_dh_pubkey"), nonce)
        require(expected == report.report_data)          # channel binding + freshness

        self.policy.check(report.mrtd, report.rtmr0, report.rtmr1,
                          report.rtmr2, compose_hash)     # pin or TOFU

        self.identity = VerifiedSink(
            url=self.base, sig_pubkey=b["sink_sig_pubkey"],
            mrtd=report.mrtd, rtmr012=(report.rtmr0, report.rtmr1, report.rtmr2),
            compose_hash=compose_hash, app_id=b["app_id"])
        return self.identity

    # push only after verify() succeeded
    def push_transcript(self, t: Transcript) -> dict:
        assert self.identity, "verify() the TEE before pushing"
        r = httpx.post(f"{self.base}/v1/transcript", json=t.model_dump(),
                       headers={"X-Sink-Protocol": "voxterm-sink/1"})
        return r.json()

    def open_stream(self, header: "StreamHeader"):
        assert self.identity, "verify() the TEE before pushing"
        # yields chunks as NDJSON over a long-lived POST; see A.2 ingest_stream
        ...

    def fetch(self, **query) -> dict:
        tok = httpx.post(f"{self.base}/v1/auth",
                         json={"tier": "cohort",
                               "secret": os.environ.get("SINK_READ_SECRET", "1234")}
                        ).json()["token"]
        return httpx.get(f"{self.base}/v1/transcript", params=query,
                         headers={"Authorization": f"Bearer {tok}"}).json()
```

### A.4 VoxTerm integration hook (informative)

1. User adds a sink URL in Hivemind settings (keybinding `H` per Hivemind scoping §7).
2. VoxTerm runs `SinkClient.verify()`. On failure it refuses and shows which check failed. On TOFU first-contact it records the measurement and warns it is unpinned.
3. Push policy is a client choice: live (`open_stream` during recording) or on-finalize (`push_transcript` at session end / save `S`). Both are conformant; default RECOMMENDED is live stream with on-finalize assembly.
4. The local agent-legible mirror (`~/Documents/voxterm/hivemind/<id>/*.md`) is unaffected; the sink is an additional destination, not a replacement.

---

## Appendix B. `measurements.json` (published per release)

```jsonc
{
  "release": "voxterm-data-sink v1.0.0",
  "wire": "voxterm-sink/1",
  "compose_hash": "<sha256 of app-compose.json>",
  "dstack_base_images": [
    { "name": "dstack-0.x.y",
      "mrtd":  "<hex sha384>",
      "rtmr0": "<hex>", "rtmr1": "<hex>", "rtmr2": "<hex>" }
  ],
  "reproducible_build": "https://github.com/<org>/voxterm-transcript-sink/.../REPRODUCE.md",
  "kms": { "root_pubkey": "<hex|null>", "allowed_compose_hashes": ["<hex>"] }
}
```

A pinned client checks `MRTD/RTMR0..2` against an entry in `dstack_base_images` and the RTMR3 `compose-hash` against `compose_hash`.

---

## Appendix C. OpenAPI

A machine-readable OpenAPI 3.1 description of this API lives alongside this spec at [`openapi.yaml`](../../openapi.yaml). It is normative for request/response shapes where it and this prose agree; where they diverge, this prose document wins and the divergence is a bug to be fixed in `openapi.yaml`.

---

## Appendix D. Changelog

| Version | Date | Notes |
|---|---|---|
| `1.0.0-draft.1` | 2026-05-30 | Initial draft. Attestation procedure, NDJSON chunk stream, `/auth` + `/transcript`, capability lattice (cohort/coordinator), `1234` read placeholder, additive-only data model, Python reference interfaces, roadmap. |
