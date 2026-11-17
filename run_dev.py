"""Start the API for local development: `python run_dev.py`.

Runs from the backend directory whatever the caller's working directory is, so
relative paths in the configuration (`./var`, `.env`) always resolve the same way.
"""

import os
import sys
from pathlib import Path

import uvicorn

BACKEND_ROOT = Path(__file__).resolve().parent

if __name__ == "__main__":
    os.chdir(BACKEND_ROOT)
    sys.path.insert(0, str(BACKEND_ROOT))
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        reload=os.environ.get("RELOAD", "true").lower() == "true",
        reload_dirs=[str(BACKEND_ROOT / "app")],
    )
