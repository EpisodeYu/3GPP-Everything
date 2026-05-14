"""3GPP-Everything API entrypoint.

M0 stage only exposes /health; later routes will be registered under app/api/
following 04-backend-api.md.
"""

from fastapi import FastAPI

app = FastAPI(title="3GPP-Everything API", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}
