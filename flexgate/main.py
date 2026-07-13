from __future__ import annotations

import argparse
import logging
import os
import sys

import uvicorn

from flexgate.config import load_config
from flexgate.server import create_app


class _FlexgateServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, config_path: str) -> None:
        super().__init__(config)
        self._config_path = config_path

    async def startup(self, sockets=None) -> None:
        await super().startup(sockets=sockets)
        if self.started and os.environ.get("FLEXGATE_SERVICE") == "1":
            try:
                from flexgate.service import record_applied_state

                record_applied_state(self._config_path)
            except Exception:
                logging.getLogger("flexgate").exception(
                    "Failed to record applied service state"
                )


def run_server(config_path: str, port: int | None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    gateway_config = load_config(config_path)
    port = port or gateway_config.server.port
    app = create_app(gateway_config, config_path=config_path)
    server_config = uvicorn.Config(
        app,
        host=gateway_config.server.host,
        port=port,
        log_level="info",
    )
    _FlexgateServer(server_config, config_path).run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    run_server(args.config, args.port)
