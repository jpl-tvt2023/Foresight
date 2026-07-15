"""Vercel serverless entrypoint — exposes the FastAPI app.

Vercel's Python runtime detects the ASGI `app` object. The dashboard runs
read-only against Turso here (set FORESIGHT_TURSO_URL + FORESIGHT_TURSO_TOKEN
+ FORESIGHT_READONLY=1 in the Vercel project); ingestion/forecast stays a
local CLI step that pushes to the same Turso database.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from foresight.api import app  # noqa: E402, F401
