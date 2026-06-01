# VoxTerm Sink Upload CLI

| Field | Value |
|---|---|
| **Spec** | VoxTerm Sink Upload CLI |
| **Version** | `0.1.0-draft.1` |
| **Status** | Draft |
| **Wire dependency** | [`voxterm-sink/1`](./voxterm-sink-protocol.md) |
| **Target runtime** | Local CLI uploading to a Phala Cloud / Dstack TEE sink |
| **Date** | 2026-06-01 |

This document specifies a small client-side CLI for uploading existing VoxTerm
transcript exports to a verified TEE-backed `voxterm-sink/1` backend. It is a
specification only; it does not imply the CLI has been implemented.

## 1. Goals

The CLI provides a bridge from local VoxTerm transcript files to the deployed
TEE sink without requiring the full VoxTerm application to implement TEE
publishing first.

The CLI MUST:

1. Accept one or more existing VoxTerm Markdown transcript exports.
2. Verify the sink is an attested TEE before upload.
3. Convert each input file into a valid `voxterm-sink/1` `Transcript`.
4. Sign each transcript with a persistent local Ed25519 author key.
5. Upload each transcript with `POST /v1/transcript`.
6. Verify the sink-signed upload response before reporting success.

The CLI MUST NOT:

- capture audio;
- run transcription or diarization;
- implement live `/v1/transcript/stream` upload;
- implement a GUI;
- replace the full VoxTerm client integration described in
  `VoxTerm/docs/specs/hivemind-sink-integration.md`;
- weaken sink verification by default.

## 2. Packaging

The reference implementation SHOULD live in the `voxterm-transcript-sink`
repository as a second Python import package in the same wheel as the sink
server.

Recommended package and script names:

```toml
[project.scripts]
voxterm-data-sink = "voxterm_transcript_sink.__main__:main"
voxterm-sink-upload = "voxterm_sink_client.__main__:main"

[tool.hatch.build.targets.wheel]
packages = ["voxterm_transcript_sink", "voxterm_sink_client"]
```

The implementation SHOULD use the existing project dependencies where practical:
`cryptography`, `blake3`, `rfc8785`, and an HTTP client available in the project
environment. It SHOULD use `argparse` unless the project already adopts a richer
CLI framework.

## 3. Command Surface

The CLI MUST expose these commands:

```bash
voxterm-sink-upload verify --sink-url URL
voxterm-sink-upload upload PATH... --sink-url URL --hivemind-id UUID [options]
voxterm-sink-upload trust inspect
voxterm-sink-upload trust reset --sink-url URL
```

`verify` verifies the TEE sink and records/refreshes TOFU trust.

`upload` verifies the TEE sink, converts each input path into a transcript, and
uploads it. If a directory is provided, the CLI scans `*.md` files in that
directory. Recursive scanning MUST require `--recursive`.

`trust inspect` prints the local trusted sink records without secrets.

`trust reset --sink-url URL` removes the URL's local trust binding. It MUST NOT
delete transcripts from the sink.

### 3.1 Upload Options

`upload` MUST support:

| Option | Required | Meaning |
|---|---:|---|
| `PATH...` | yes | File or directory paths to upload. |
| `--sink-url URL` | yes | Base URL of the TEE sink. May include or omit `/v1`; the client normalizes it. |
| `--hivemind-id UUID` | yes | Cohort/hivemind UUID written into every transcript. |
| `--recursive` | no | Recursively scan directories for Markdown exports. |
| `--tag TAG` | no | Add one tag; repeatable. |
| `--dry-run` | no | Parse and build transcripts, but do not upload. |
| `--json` | no | Emit machine-readable JSON result output. |

The CLI MAY later support `--timezone`, `--content-type`, or alternate input
formats, but v1 SHOULD keep those out unless a concrete need appears.

## 4. Input Format

The v1 CLI accepts VoxTerm Markdown transcript exports. A valid input file MUST:

1. Be UTF-8 Markdown.
2. Have a filename containing a VoxTerm session timestamp:
   `YYYY-MM-DD_HHMMSS`, usually `YYYY-MM-DD_HHMMSS-transcript.md`.
3. Contain zero or more transcript lines in one of these forms:

```markdown
**[HH:MM:SS]** **Speaker:** text
**[HH:MM:SS]** text
```

The parser SHOULD tolerate the two known VoxTerm header styles:

```markdown
# VOXTERM Transcript

- **Date:** 2026-06-01
- **Time:** 12:00:00
- **Model:** qwen3-0.6b
- **Language:** en

---
```

and:

```markdown
# VoxTerm Transcript

- **Date:** Monday, June 01, 2026
- **Started:** 12:00 PM
- **Model:** qwen3-0.6b
- **Language:** English

---
```

Summary blocks inserted by VoxTerm summary export MUST be preserved in the
`markdown` field but MUST NOT be parsed as transcript segments unless they match
the transcript-line grammar.

Files with no transcript lines SHOULD be rejected by default because the sink
expects a useful transcript payload.

## 5. Conversion To `Transcript`

For each input file, the CLI builds one finalized `Transcript` object and sends
it to `POST /v1/transcript`.

### 5.1 Required Fields

The CLI MUST set:

| Transcript field | Source |
|---|---|
| `schema_version` | `"1"` |
| `sink_id` | Verified `GET /v1/info` response. |
| `hivemind_id` | `--hivemind-id`. |
| `session_id` | Filename timestamp, `YYYY-MM-DD_HHMMSS`. |
| `author` | Local Ed25519 author public key, 64 lowercase hex chars. |
| `content_type` | `"transcript"`. |
| `created_at` | Session timestamp rendered as UTC RFC3339 `Z`. |
| `title` | Input filename stem. |
| `tags` | Repeated `--tag` values. |
| `parent_ids` | Empty list. |
| `segments` | Parsed transcript lines. |
| `markdown` | Original file content. |
| `source` | Provenance object described below. |
| `id` | BLAKE3-256 over JCS of the object without `id` or `signature`. |
| `signature` | Ed25519 over JCS of the object without `signature`. |

The implementation MUST use the same canonicalization rules as
`voxterm-sink/1`: RFC 8785 JCS, lowercase hex hashes, and `ed25519:<hex>`
signature prefixes.

### 5.2 Time Handling

`session_id` is timezone-free. For v1, the CLI SHOULD interpret the filename
timestamp in the local system timezone and convert it to UTC for `created_at`.
This is acceptable for manual imports because VoxTerm exports are local files.

Transcript line timestamps are `HH:MM:SS` offsets on the session date. Segment
times MUST be stored as seconds relative to session start:

- `t_start` is the parsed line timestamp minus the session start time.
- `t_end` is the next segment's `t_start`.
- The final segment's `t_end` is `t_start + 1.0`.
- If line timestamps decrease, the parser SHOULD treat that as a midnight
  rollover and add 24 hours to subsequent timestamps.
- Negative `t_start` values SHOULD be clamped to `0.0` only if the line appears
  within a small tolerance of the session start; otherwise the file should fail
  validation.

### 5.3 Speaker Mapping

The parser MUST assign stable `speaker.local_id` values by first appearance:

```text
Speaker label "Alice" -> local_id 1
Speaker label "Bob"   -> local_id 2
```

Unlabelled transcript lines SHOULD use:

```json
{"local_id": 0, "label": null}
```

### 5.4 Source Metadata

The `source` object MUST preserve enough provenance to audit an import:

```json
{
  "tool": "voxterm-sink-upload",
  "tool_version": "0.1.0-draft.1",
  "input_format": "voxterm-markdown",
  "filename": "2026-06-01_120000-transcript.md",
  "file_blake3": "<64 hex chars>",
  "model": "qwen3-0.6b",
  "language": "en"
}
```

`model` and `language` MAY be omitted if the header does not contain them.

## 6. Author Identity

The CLI MUST create one persistent Ed25519 author key on first use and reuse it
for future uploads.

Recommended path:

```text
~/.config/voxterm-sink-client/author_ed25519.key
```

The key file MUST contain only the raw 32-byte private key material, encoded in
hex or another explicit implementation-defined format. It MUST be written with
owner-only permissions where supported (`0600`). The private key MUST never be
sent to the sink.

The wire `author` field is the raw Ed25519 public key as 64 lowercase hex chars.

## 7. TEE Verification

The CLI MUST verify the sink before upload. The default policy is TOFU
(trust-on-first-use). There is no default insecure mode.

The CLI MUST:

1. Normalize `--sink-url` and fetch:
   ```http
   GET {sink_url}/v1/attestation?nonce=<32-byte-random-hex>
   ```
2. Verify the returned TDX quote using Phala's documented attestation
   verification API:
   ```http
   POST https://cloud-api.phala.com/api/v1/attestations/verify
   ```
3. Require the verifier's quote result to be verified.
4. Recompute `report_data` exactly as specified by `voxterm-sink/1`:
   ```text
   BLAKE3-512(
       "voxterm-sink/1\x00" ||
       sink_sig_pubkey ||
       sink_dh_pubkey_or_32_zero_bytes ||
       nonce
   )
   ```
5. Compare the recomputed value to the verified quote's `reportdata`.
6. Fetch:
   ```http
   GET {sink_url}/v1/info
   ```
7. Verify `X-Sink-Signature` on the `/v1/info` JSON body using the attested
   `sink_sig_pubkey`.
8. Require `app_id` and `compose_hash` from `/v1/info` to match the attestation
   bundle and/or verifier output.
9. Apply the local TOFU trust policy.

The implementation SHOULD also support a local verifier backend later, such as
`dcap-qvl` or a Phala/dstack verifier binary, but the first CLI can use the
Phala Cloud attestation API to keep setup small.

## 8. TOFU Trust Store

The CLI MUST persist verified sink trust.

Recommended path:

```text
~/.config/voxterm-sink-client/verified_sinks.json
```

Shape:

```json
{
  "schema_version": 1,
  "url_index": {
    "https://sink.example": "<sink_sig_pubkey>"
  },
  "sinks": {
    "<sink_sig_pubkey>": {
      "sink_sig_pubkey": "<hex ed25519 pubkey>",
      "app_id": "<hex>",
      "compose_hash": "<hex sha256>",
      "first_seen": "2026-06-01T00:00:00Z",
      "last_verified": "2026-06-01T00:00:00Z",
      "urls": ["https://sink.example"],
      "verifier": {
        "provider": "phala-cloud-api",
        "summary": {}
      }
    }
  }
}
```

On first successful verification, the CLI MUST create the trust record.

On later verification:

- A known URL presenting a different `sink_sig_pubkey` MUST fail.
- A known sink presenting a different `compose_hash` MUST fail.
- A URL change with the same verified sink key and compose hash MAY be added to
  the same sink record.
- `trust reset --sink-url URL` is the only v1 mechanism for accepting a changed
  sink or redeploy.

## 9. Upload Protocol

For each transcript, the CLI MUST send:

```http
POST {sink_url}/v1/transcript
X-Sink-Protocol: voxterm-sink/1
Content-Type: application/json
```

The CLI MUST treat:

- `201` as newly stored;
- `200` as already stored/idempotent success;
- `409 id_mismatch` as a client conversion bug;
- `400 schema_mismatch` as an invalid input/conversion failure;
- `413 payload_too_large` as a hard failure for that file.

For `200` or `201`, the CLI MUST verify `X-Sink-Signature` on the JSON response
before reporting success. The signature basis is
`ed25519(BLAKE3(JCS(response_body)))`, matching the sink implementation.

The CLI SHOULD continue uploading remaining files after a per-file validation or
upload failure, then exit non-zero if any file failed.

## 10. Output

Human output SHOULD be concise:

```text
verified sink ee94d2e99c0822f395fa7a0fd1a4865b7fb4c8a6
uploaded 2026-06-01_120000-transcript.md id=<transcript-id>
```

With `--json`, output MUST be machine-readable and MUST NOT include private key
material or bearer tokens:

```json
{
  "sink_url": "https://...",
  "verified": true,
  "uploaded": [
    {
      "path": "2026-06-01_120000-transcript.md",
      "id": "<transcript id>",
      "status": "stored"
    }
  ],
  "failed": []
}
```

## 11. Tests

The implementation MUST include tests for:

- Markdown parsing of standard exported files.
- Markdown parsing of live autosave files.
- Summary blocks being preserved in `markdown` but not parsed as dialogue.
- Unlabelled transcript lines.
- Stable speaker label to `local_id` mapping.
- Midnight rollover.
- Deterministic transcript IDs.
- Valid Ed25519 author signatures.
- TOFU first-use accept.
- TOFU repeated-sink accept.
- TOFU changed sink key rejection.
- TOFU changed compose hash rejection.
- `trust reset` removal by URL.
- Upload success for `201`.
- Upload idempotent success for `200`.
- Upload failure for `400`, `409`, and `413`.
- Verification of sink `X-Sink-Signature` responses.

TEE validation tests MUST include both offline and live paths.

Offline tests MUST use fixture attestation/verifier responses to cover:

- accepted verified quote with matching nonce/reportdata;
- rejected verifier response where quote verification is false;
- rejected mismatched nonce/reportdata;
- rejected missing or malformed attestation fields;
- rejected `/v1/info` signature;
- rejected `/v1/info` app or compose mismatch.

Live TEE tests MUST be opt-in through environment variables so normal test runs
remain hermetic:

```bash
VOXTERM_TEE_E2E=1 \
VOXTERM_SINK_URL=https://... \
PHALA_CVM_ID=d4ea3bb1-f637-4161-ba6a-d6aa04c5d862 \
uv run pytest tests/test_client_tee_e2e.py -q
```

The live test SHOULD:

1. Call `phala cvms get --json --cvm-id "$PHALA_CVM_ID"` and require
   `status == "running"`.
2. Confirm the CVM endpoint matches `VOXTERM_SINK_URL`.
3. Fetch and verify `/v1/attestation` through the Phala attestation API.
4. Fetch `/v1/info` and verify `X-Sink-Signature`.
5. Upload a tiny fixture transcript.
6. Treat `200` or `201` as success after verifying response signature.

## 12. Security Notes

This CLI uploads plaintext transcripts to the TEE sink. The v1 confidentiality
boundary is the verified TEE, matching `voxterm-sink/1`; it is not end-to-end
encrypted.

The CLI MUST fail closed when verification fails. A future implementation MAY
add an explicit development-only bypass flag, but that flag is intentionally out
of scope for this draft.

The local author key and trust store are security-sensitive local state. The CLI
MUST avoid printing private keys, read secrets, bearer tokens, or raw
credentials in logs or JSON output.

## 13. References

- `specs/v1/voxterm-sink-protocol.md`
- `VoxTerm/docs/specs/hivemind-sink-integration.md`
- Phala Cloud CLI overview:
  `https://docs.phala.com/phala-cloud/phala-cloud-cli/overview`
- Phala Cloud deploy command:
  `https://docs.phala.com/phala-cloud/phala-cloud-cli/deploy`
- Phala Cloud attestation API:
  `https://docs.phala.com/phala-cloud/phala-cloud-api/attestations`
- Phala platform verification:
  `https://docs.phala.com/phala-cloud/attestation/verify-the-platform`
