"""`caliper-server` console script — `uvicorn` wrapper with sensible defaults."""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="caliper-server", description="Caliper dry-run backend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="restart on code changes")
    return parser


def main() -> None:
    import uvicorn

    args = build_parser().parse_args()

    uvicorn.run(
        "caliper_server.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
