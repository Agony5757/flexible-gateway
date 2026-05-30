from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from flexgate.config import load_config
from flexgate.server import create_app


def run_server(config_path: str, port: int | None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = load_config(config_path)
    port = port or config.server.port
    app = create_app(config, config_path=config_path)
    uvicorn.run(app, host=config.server.host, port=port, log_level="info")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    run_server(args.config, args.port)
