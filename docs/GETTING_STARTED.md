# Getting started (using a sink)

You have VoxTerm transcripts and someone is running a `voxterm-sink/1` sink. This
page gets you uploading to it in a few minutes. You verify the sink is a genuine
TEE *before* anything is sent — you authenticate the enclave, not the operator.

> **What this is today.** The path that works right now is the `voxterm-sink-upload`
> CLI run over your existing VoxTerm **Markdown exports**. The live VoxTerm TUI does
> **not** yet speak this sink's protocol (it still posts legacy
> `shape-rotator-hivemind/v1` batches over LAN mDNS — the two are not wire-compatible).
> Live publishing from the app is roadmap; see
> [HOSTING_AND_GUARANTEES.md](HOSTING_AND_GUARANTEES.md). If you want to *run* a sink
> instead of use one, see [SELF_HOSTING.md](SELF_HOSTING.md).

## 1. Install the client

The client ships in the `voxterm-data-sink` wheel as the `voxterm-sink-upload`
console script. Until there's a published release, install it from a checkout of
this repo:

```bash
pipx install ./voxterm-transcript-sink        # isolated, recommended
# or, inside a venv:  pip install ./voxterm-transcript-sink

voxterm-sink-upload --help
```

> Once published to PyPI this becomes a one-liner: `pipx install voxterm-data-sink`.

Requires Python 3.12+. On first use the client creates a persistent Ed25519
**author key** at `~/.config/voxterm-sink-client/author_ed25519.key` (owner-only,
`0600`). The private key is **never** sent to the sink — only its public half rides
along as the `author` field.

## 2. Verify the sink

You need the sink's base URL from whoever runs it. On Phala/dstack it looks like
`https://<app-id>-8723.<gateway-domain>`.

```bash
# first contact / staging — trust-on-first-use (the default policy)
voxterm-sink-upload verify --sink-url https://<app-id>-8723.<gateway-domain>
```

This fetches a fresh TDX attestation quote, verifies it, replays the event log to
confirm the running code, checks the quote binds the sink's signing key, and then
records the sink in your local trust store at
`~/.config/voxterm-sink-client/verified_sinks.json`.

Two trust policies:

- **`tofu`** (default) — *trust on first use*. Accepts the sink the first time and
  remembers its measurements. A later run that presents a **different** sink key,
  `compose_hash`, or measurements **fails** — that's the protection. To accept a
  deliberate redeploy, run `trust reset` (below) and verify again.
- **`pinned`** — for a production cohort. Requires the live quote to match a
  published release manifest and **fails closed** otherwise:

  ```bash
  voxterm-sink-upload verify --sink-url https://<app-id>-8723.<gateway-domain> \
    --measurement-policy pinned --measurements ./measurements.json
  ```

  Your sink operator publishes the `measurements.json`. (Note: in this PoC release
  the manifest still ships with placeholders until a real TDX build fills it in —
  don't pin against placeholders. See [HOSTING_AND_GUARANTEES.md](HOSTING_AND_GUARANTEES.md).)

## 3. Quick upload

Point the CLI at your VoxTerm Markdown exports (a file, several files, or a
directory of `*.md`) and the cohort/hivemind UUID you're uploading into:

```bash
voxterm-sink-upload upload ~/Documents/voxterm \
  --sink-url https://<app-id>-8723.<gateway-domain> \
  --hivemind-id <UUID> \
  --recursive \
  --tag meeting
```

`upload` verifies the sink first (same checks as step 2), so a separate `verify` is
optional — though it's a nice dry-run of trust before you send anything. Useful flags:

- `--dry-run` — parse and build the transcripts, print what *would* upload, send
  nothing. Run this first if you're unsure.
- `--tag TAG` — attach a tag; repeatable.
- `--recursive` — descend into sub-directories (otherwise only the top level is scanned).
- `--json` — machine-readable output (never prints keys or secrets).
- `--measurement-policy pinned --measurements PATH` — same pinning option as `verify`.

The command exits non-zero if **any** file failed, but keeps going through the rest,
so one bad file doesn't abort the batch.

Each input must be a VoxTerm Markdown export whose filename carries a session
timestamp (`YYYY-MM-DD_HHMMSS-transcript.md`). The CLI parses the dialogue lines
into segments, preserves the original markdown, and content-addresses each
transcript by BLAKE3 — so re-uploading the same file is idempotent (you'll see
`status=already_stored` instead of `status=created`).

## 4. CLI capabilities

| Command | What it does |
|---|---|
| `voxterm-sink-upload verify --sink-url URL` | Attest the sink and record/refresh local trust. Add `--measurement-policy pinned --measurements PATH` for pinned trust. |
| `voxterm-sink-upload upload PATH... --sink-url URL --hivemind-id UUID` | Verify, then convert and upload Markdown exports. Options: `--recursive`, `--tag`, `--dry-run`, `--json`, `--measurement-policy`. |
| `voxterm-sink-upload trust inspect` | Print your local trusted-sink records (no secrets). |
| `voxterm-sink-upload trust reset --sink-url URL` | Forget a sink's local trust binding — the only way to accept a changed/redeployed sink under TOFU. Does **not** delete anything from the sink. |

## 5. What you get — and what you don't (v1)

- Transcripts are uploaded **plaintext to a verified TEE**. Confidentiality in v1 is
  the attested TEE boundary plus sealed storage — **not** end-to-end encryption. Don't
  upload anything you wouldn't trust to a verified enclave.
- Reading transcripts back (`GET /v1/transcript`) uses a **placeholder shared secret**
  (default `1234`) in v1. It is explicitly not real per-member auth.
- Author signatures are **optional** in v1; registered membership is roadmap.

The full picture of guarantees and what's coming is in
[HOSTING_AND_GUARANTEES.md](HOSTING_AND_GUARANTEES.md). The complete, normative CLI
contract is [`specs/v1/voxterm-sink-upload-cli.md`](../specs/v1/voxterm-sink-upload-cli.md).
