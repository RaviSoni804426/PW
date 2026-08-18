"""Run the backend: ``python -m backend``.

Configuration comes from the environment, not from flags, because that is what
the container platform supplies. Host and port are the exception -- a platform
sets those per deployment.
"""

from __future__ import annotations

import logging
import os
import sys

import uvicorn

from .app import create_app
from .settings import Settings, SettingsError


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        settings = Settings.from_env()
    except SettingsError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    app = create_app(settings)
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        # The platform's reverse proxy terminates TLS and forwards the real
        # client address; without this every request appears to come from the
        # proxy itself, which makes any later rate limiting or audit useless.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
