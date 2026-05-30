"""Run the sink: ``python -m voxterm_transcript_sink``."""

from __future__ import annotations

import logging
import os

import uvicorn

from .app import create_app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("VOXTERM_SINK_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = create_app()
    uvicorn.run(
        app,
        host=os.environ.get("VOXTERM_SINK_HOST", "0.0.0.0"),
        port=int(os.environ.get("VOXTERM_SINK_PORT", "8723")),
    )


if __name__ == "__main__":
    main()
