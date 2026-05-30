"""voxterm-data-sink — reference PoC for the VoxTerm Sink Protocol.

Wire identifier: ``voxterm-sink/1``. Spec: ``specs/v1/voxterm-sink-protocol.md``.

This is a proof-of-concept sink server: it implements the §7 HTTP API, the §9
data model, and a §5 attestation endpoint. It is NOT a production TEE
deployment — storage is in-memory (with an optional JSON snapshot) and the
default attestation backend only works when running inside a real dstack TD.
"""

WIRE = "voxterm-sink/1"
SPEC_VERSION = "1.0.0-draft.1"
SCHEMA_VERSION = "1"
RELEASE = "voxterm-data-sink v0.1.0 (PoC)"

__all__ = ["WIRE", "SPEC_VERSION", "SCHEMA_VERSION", "RELEASE"]
