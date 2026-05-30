"""Simulator-backed integration test for the REAL dstack code path.

Phala's local-dev guidance is to run the dstack TEE simulator and point the SDK
at it via ``DSTACK_SIMULATOR_ENDPOINT=/path/to/dstack.sock``. When that env var
is set (and ``dstack-sdk`` is installed) this test exercises the actual
``DstackBackend`` — ``get_key`` derivation of ``sink_sig`` and a real
``get_quote`` served by ``/v1/attestation`` — instead of the fabricated dev
provider. It is skipped otherwise so the default unit suite stays hermetic.

Run locally:

    git clone https://github.com/Dstack-TEE/dstack.git
    (cd dstack/sdk/simulator && ./build.sh && ./dstack-simulator &)
    export DSTACK_SIMULATOR_ENDPOINT=$PWD/dstack/sdk/simulator/dstack.sock
    pytest tests/test_simulator.py -v
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from voxterm_transcript_sink import WIRE
from voxterm_transcript_sink.app import create_app
from voxterm_transcript_sink.attestation import compute_report_data
from voxterm_transcript_sink.config import Settings

pytest.importorskip("dstack_sdk", reason="dstack-sdk not installed")

ENDPOINT = os.environ.get("DSTACK_SIMULATOR_ENDPOINT")
pytestmark = pytest.mark.skipif(
    not ENDPOINT, reason="DSTACK_SIMULATOR_ENDPOINT not set; run the dstack simulator"
)


def sim_client() -> TestClient:
    # attest_mode defaults to dstack; endpoint picked up from the env var.
    return TestClient(create_app(Settings.from_env()))


def test_info_reflects_real_dstack_identity():
    body = sim_client().get("/v1/info").json()
    assert body["wire"] == WIRE
    # sink_sig is get_key-derived, not the seed fallback → 32-byte ed25519 pubkey.
    assert len(bytes.fromhex(body["sink_sig_pubkey"])) == 32
    # app_id / compose_hash come from client.info(), not placeholders.
    assert body["app_id"]


def test_attestation_serves_real_quote_and_replays():
    nonce = bytes(range(32))
    bundle = sim_client().get(f"/v1/attestation?nonce={nonce.hex()}").json()
    assert bundle["wire"] == WIRE
    quote = bytes.fromhex(bundle["quote"])
    assert len(quote) > 64  # a real TDX quote, not the dev DEVQUOTE blob

    # The quote returned by the sink must bind the sink key + client nonce into
    # TDX REPORTDATA (spec §5.4 / §6.2 channel binding). The simulator patches
    # REPORTDATA into its quote fixture, so the expected 64 bytes appear in the
    # exact quote served by /v1/attestation.
    expected_report_data = compute_report_data(
        bytes.fromhex(bundle["sink_sig_pubkey"]), None, nonce
    )
    assert expected_report_data in quote

    # Replay the event log from the exact sink bundle, not a second direct quote.
    # This matches Phala's local-development guidance to use replay_rtmrs() when
    # testing against the dstack simulator.
    from dstack_sdk.dstack_client import GetQuoteResponse

    q = GetQuoteResponse(quote=bundle["quote"], event_log=bundle["event_log"])
    rtmrs = q.replay_rtmrs()
    assert isinstance(rtmrs, (list, dict))
