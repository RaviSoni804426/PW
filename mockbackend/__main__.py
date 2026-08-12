"""Run the mock backend: ``python -m mockbackend``."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from .app import DEFAULT_ENROLLMENT_KEY, create_mock_backend


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock OngoingRec central backend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument(
        "--uploads",
        type=Path,
        default=Path("./mock-uploads"),
        help="where received clips are written",
    )
    parser.add_argument("--enrollment-key", default=DEFAULT_ENROLLMENT_KEY)
    args = parser.parse_args()

    app, _ = create_mock_backend(uploads_dir=args.uploads, enrollment_key=args.enrollment_key)
    print(f"mock backend on http://{args.host}:{args.port}  (docs at /docs)")
    print(f"enrollment key: {args.enrollment_key}")
    print(f"clips will be written to {args.uploads.resolve()}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
