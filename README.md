# voxterm-transcript-sink

An authenticated, always-on data sink for [VoxTerm](https://github.com/dmarzzz/VoxTerm) hivemind mode. It runs inside a TEE, so it can hold a group's full transcript history without ever being able to read it.

## what this is

VoxTerm hivemind mode is persistent, cross-location, async sharing of transcripts, notes, and readouts across a trusted group. Party mode (real-time, LAN-only, one room) feeds it. Hivemind is what the agents point at.

The protocol is mesh gossip with no coordinator. Every member is sovereign over their own copy, holds a complete local replica of each hivemind they joined, and syncs opportunistically when peers are reachable. That model is correct, and it has one gap: it assumes someone is online. When every laptop in the group is closed, a peer who just joined, or who reconnects after a week away, has no one to backfill from.

This is the peer that never sleeps. It is not a coordinator and not an authority. It is one more node in the mesh that happens to stay online: it joins a hivemind, accepts gossiped entries, and serves request/reply backfill when a member comes back.

## why a TEE

The hivemind scoping doc flags bootstrap relay operators as an explicit centralization tradeoff. An always-on node sees every entry that flows through the group, so normally you have to trust whoever runs the box.

A TEE removes that trust instead of asking for it. The sink runs inside an enclave with remote attestation. It stays online, accepts entries, and serves backfill, but the operator cannot read private hivemind payloads and cannot forge or tamper with entries. You do not trust the operator. You verify the enclave.

This is the answer to the relay-operator question that does not reintroduce a coordinator: a node that holds the data without holding the keys to it.

## authenticated, both directions

Membership in a private hivemind is possession of the shared `hivemind_key` plus a registered author pubkey. The sink enforces both:

- **The sink authenticates writers.** Every entry is signed with the author's ed25519 key. The sink verifies the signature and the author's membership before it accepts or relays an entry. No valid signature, no admission.
- **Writers authenticate the sink.** Before any private group-key material crosses the channel, the client checks the enclave's remote attestation. A node that cannot prove it is the expected code running in a genuine TEE never receives the key.

## what it stores

It stores the hivemind entry model as published, nothing decoded:

- signed with the author's ed25519 key
- content-addressed by BLAKE3 over canonical bytes
- payloads encrypted with the group's AES-256-GCM key for a private hivemind

For a private hivemind the sink only ever holds opaque ciphertext. It can index by `hivemind_id`, author, tags, and time range from the cleartext envelope, and serve backfill, without the group key. For a public hivemind payloads are unencrypted but still signed, and the sink verifies signatures the same way.

Immutability and tombstones follow the protocol: entries are append-only, edits are new entries with `parent_ids` set, deletions are tombstones the sink respects in its views.

## relationship to VoxTerm

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

Sources are pluggable, hivemind is the destination, and this is one durable node on that destination. It does not replace any peer's local copy. It is the copy that is always reachable.

## the spec

This repo is spec-first. Someone else ships the implementation against a frozen wire contract.

- [`specs/v1/voxterm-sink-protocol.md`](specs/v1/voxterm-sink-protocol.md) is the normative protocol, version `1.0.0-draft.1`, wire identifier `voxterm-sink/1`. It defines the attestation procedure ("verify it's a TEE"), the data model, the `/auth` and `/transcript` APIs, the cohort/coordinator auth lattice, and the roadmap.
- [`openapi.yaml`](openapi.yaml) is the machine-readable API description. The prose spec wins on any divergence.

Read it in that order. The short version of the v1 design:

1. The sink runs in Dstack on Intel TDX. It derives a long-term `sink_sig` identity from its attested app identity and serves a TDX DCAP quote at `GET /v1/attestation`.
2. A VoxTerm client adds the sink by URL, sends a fresh nonce, verifies the quote against Intel collateral, replays the event log to confirm the running code, checks the quote's `report_data` binds the sink's key (channel binding plus freshness), and only then pushes.
3. Push is a live `POST /v1/transcript/stream` (NDJSON chunk stream) or a single `POST /v1/transcript` on finalize. Whether you stream live or post once is a client choice; the sink supports both.
4. Read is `GET /v1/transcript`. v1 read auth is a labeled placeholder: a shared static secret defaulting to `1234`. It is not real auth and the spec says so. The real cohort/coordinator model is defined and deferred.

The data model is additive-only and versioned, so v1 can evolve without breaking old clients or old data.

## what is deferred

Per the spec roadmap (§12): author-signed writes, real read auth replacing `1234`, coordinator enforcement, end-to-end payload encryption under a hivemind group key, tombstones, and the full Hivemind mesh bridge. v1 ships the attested always-on sink with the easy auth; the hard halves are flagged, not hidden.

## status

Draft. The spec is open for contribution (see §13 for the process). The Hivemind protocol it targets is itself still in scoping; see `docs/hivemind-scoping.md` in the VoxTerm repo for the entry model, membership rules, and open questions this sink fits into.
