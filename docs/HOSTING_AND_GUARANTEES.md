# Hosting, guarantees, and what's next

This page is the operations-and-trust overview: **how the sink is hosted**, **what v1
actually guarantees** (and, just as importantly, what it doesn't), and **where it's
going**. For hands-on instructions see [GETTING_STARTED.md](GETTING_STARTED.md) (use a
sink) and [SELF_HOSTING.md](SELF_HOSTING.md) (run one). The normative source is
[`specs/v1/voxterm-sink-protocol.md`](../specs/v1/voxterm-sink-protocol.md).

## How we host

The sink runs inside a Confidential VM (a TD) on **Intel TDX**, deployed on
**Phala Cloud / dstack**. The request path:

```
   client (voxterm-sink-upload)
        │  HTTPS
        ▼
   dstack-gateway            terminates public ACME TLS, reverse-proxies, speaks RA-TLS inward
        │
        ▼
   voxterm-data-sink         runs as non-root inside the TD (CVM)
        │  /var/run/dstack.sock
        ▼
   dstack guest agent        get_quote() → TDX DCAP quote;  get_key() → sink_sig identity
        │
        ▼
   dstack-kms                derives per-app keys bound to the attested identity → seals /data
```

- **`dstack-gateway`** terminates public TLS and reverse-proxies to the sink. Because
  TLS terminates here, this is also where rate limiting and per-IP caps belong.
- **The sink** runs as a non-root user inside the TD, derives its long-term Ed25519
  `sink_sig` identity from the attested app identity via the guest agent, and serves a
  TDX quote at `GET /v1/attestation`.
- **`dstack-kms`** derives per-app keys bound to the app's measured identity; the
  `/data` volume is sealed with those keys, so storage is only readable by the same
  attested code.
- Deployments **upgrade the app in place** to preserve the `sink_sig` identity and the
  sealed `/data` volume across releases; the `compose_hash` changes by design and is
  what clients pin (see [SELF_HOSTING.md](SELF_HOSTING.md)).

## What v1 guarantees

**Authenticity of the sink — you verify the enclave, not the operator.**
Before a client uploads, it fetches a fresh TDX DCAP quote, verifies it against Intel
collateral, replays the event log to confirm the running code (RTMR3 → `compose_hash`),
and checks the quote's `report_data` binds the sink's `sink_sig` key. Every response is
signed by that attested key (`X-Sink-Signature`). Two trust tiers:

- **TOFU** (default) — pin the sink's measurements on first contact; a later change in
  sink key, `compose_hash`, or measurements fails verification.
- **Pinned** (`--measurement-policy pinned`) — require the live quote to match a
  published release `measurements.json`, fail closed otherwise.

> The shipped `measurements.json` is a **template with placeholders** until a real TDX
> build reads the values back from a live quote ([REPRODUCE.md](REPRODUCE.md)). Don't
> pin a cohort against placeholders; use TOFU until a release manifest is published.

**The operator can't read or forge.** Confidentiality comes from the verified TEE
boundary plus KMS-sealed storage — the host operator sees ciphertext at rest and cannot
read private payloads or tamper with stored entries undetected. This is the whole point:
an always-on node that holds the data without holding the keys to it.

**Integrity and idempotency.** Transcripts are content-addressed by BLAKE3 over RFC 8785
(JCS) canonical bytes, so the `id` *is* the content; re-uploading is idempotent. The data
model is additive-only and versioned, so v1 can evolve without breaking old data or clients.

## What v1 does NOT guarantee (yet)

Stated plainly, because the spec is — these are flagged, not hidden:

- **No end-to-end payload encryption.** Transcripts are stored plaintext inside the TEE;
  confidentiality is the attested boundary, not an e2e group key.
- **Read auth is a placeholder.** `GET /v1/transcript` is gated by a single shared
  secret (default `1234`), not real per-member capability tokens.
- **Author signatures are optional.** If present they're verified; if absent the write
  is still accepted. Registered author membership is roadmap.
- **Availability is best-effort.** The sink is the always-reachable copy, but the
  Hivemind **mesh is the source of truth** — this node is a convenience, not an authority.
- **Rate limiting is gateway-side only**, not enforced in the app (size caps are).

## What's next

The protocol roadmap (spec §12), ordered by dependency — each is an additive `/v1`
follow-up unless a break is unavoidable:

1. **Author-signed writes** — require valid Ed25519 signatures; coordinators register
   author pubkeys.
2. **Real read auth** — replace the `1234` secret with per-member capability tokens
   (same `POST /v1/auth` shape).
3. **Coordinator enforcement** — admin capabilities (rotate secret, evict author,
   retention, tombstone) under `/v1/admin`.
4. **E2E payload encryption** — activate `Transcript.encryption` under a hivemind group
   key (AES-256-GCM), `sink_dh` for any sink-assisted rewrap.
5. **Tombstones and deletion** — honor Hivemind tombstone semantics in sink views.
6. **Hivemind bridge** — the sink joins the iroh-docs mesh as a real Hivemind node,
   gossiping `Transcript` → `Entry` and serving backfill to peers. This is the original
   "always-on peer" promise, end to end.
7. **Multi-sink federation** — sinks gossip to each other for redundancy, deduplicating
   by content address.
8. **On-chain governance docs** — specify the KMS allowlist policy a cohort should adopt.
9. **Metadata privacy** — reduce envelope leakage (padded sizes, batched timing).

Separately, on the client side: the live **VoxTerm TUI** still speaks the legacy
`shape-rotator-hivemind/v1` protocol and is not wire-compatible with this sink. The
planned alignment — legacy and attested (TEE) modes behind a client config flag — is
specified VoxTerm-side (`VoxTerm/docs/specs/hivemind-sink-integration.md`) and summarized
in [DEVELOPMENT.md](DEVELOPMENT.md#voxterm-interoperability-gap-important). Until then,
the [`voxterm-sink-upload`](GETTING_STARTED.md) CLI is the bridge from local exports to a
verified sink.
