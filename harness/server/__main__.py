from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from harness.server.app import create_app


def main():
    parser = argparse.ArgumentParser(description="Local Harness API and Web process")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    from harness.server.composition import Services
    from harness.platform.config import instance_config

    settings = instance_config(args.data_dir, args.instance_id, host=args.host, port=args.port)
    uvicorn.run(create_app(Services(settings)), host=settings.host, port=settings.port,
                log_level=settings.log_level.lower(), access_log=False)


if __name__ == "__main__":
    main()
