"""Launch the Patent TM Suitability & Savings Estimator web server."""
from __future__ import annotations

import os
import uvicorn

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8765"))
    print(f"Starting Patent TM Estimator at http://{host}:{port}")
    uvicorn.run("app.main:app", host=host, port=port, reload=False)

