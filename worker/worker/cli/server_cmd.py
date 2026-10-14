"""`python -m worker server run` — the worker's HTTP surface (bundle reads, panel API, webhook).

Runs on uvicorn. On a Render deployment this is the web service that the UI and the operator talk
to, and the ONLY process with a database connection.
"""

from __future__ import annotations

import argparse
import os


def run(args: argparse.Namespace) -> int:
    import uvicorn

    from ..api.app import app
    from ..log import setup_logging

    setup_logging()
    host = args.host or os.environ.get("HOST", "0.0.0.0")
    port = int(args.port or os.environ.get("PORT", os.environ.get("WORKER_PORT", "8787")))
    print(f"worker HTTP surface on http://{host}:{port}  (uvicorn, single process)")
    print("  GET  /healthz                       liveness + database + whether a panel password exists")
    print("  GET  /api/bundle/<member>           UI data built from the database (needs the panel session)")
    print("  GET  /                              the exported Mission Control UI (ui/out) + login gate")
    print("  POST /api/auth/login|logout|setup   panel session (HttpOnly cookie, scrypt password)")
    print("  GET  /api/admin/integrations        fingerprints only, never values")
    print("  POST /webhook                       signed fire-and-forget trigger for an external scheduler")
    uvicorn.run(app, host=host, port=port, log_level=os.environ.get("LOG_LEVEL", "info").lower())
    return 0


__all__ = ["run"]
