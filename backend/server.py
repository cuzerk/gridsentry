"""
StormLines — request orchestration server.

Exposes a small API the frontend calls when a user picks a US bbox + date
range: POST /api/requests kicks off the ground-truth data pipeline
(transmission lines -> EagleI outage timeline -> wind/precip), GET
/api/requests/{id} polls status, and static file mounts serve the resulting
per-request JSON/GeoJSON to the frontend.

Run:
    cd backend && ../backend/venv/bin/uvicorn server:app --reload --port 8000
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "analysis"))

import orchestrator  # noqa: E402

REQUESTS_ROOT = BASE / "frontend" / "static" / "data" / "requests"
INFRA_ROOT = BASE / "analysis" / "data" / "infrastructure"
REQUESTS_ROOT.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="StormLines orchestrator")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local demo only
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory job registry (request_id -> status dict). File-based cache
# (meta.json's existence) is the source of truth across process restarts;
# this dict just tracks in-flight background jobs within one server run.
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


class RequestBody(BaseModel):
    bbox: tuple[float, float, float, float]  # west, south, east, north
    start: str  # YYYY-MM-DD
    end: str  # YYYY-MM-DD


def request_id_for(bbox: tuple, start: str, end: str) -> str:
    rounded = tuple(round(c, 2) for c in bbox)
    key = f"{rounded}|{start}|{end}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _set_status(request_id: str, **fields) -> None:
    with _jobs_lock:
        _jobs.setdefault(request_id, {}).update(fields)


def _run_pipeline(request_id: str, bbox: tuple, start_dt: datetime, end_dt: datetime) -> None:
    out_dir = REQUESTS_ROOT / request_id
    try:
        _set_status(request_id, status="orchestrating")
        result = orchestrator.run_orchestrator(bbox, start_dt, end_dt, out_dir)
        if result["status"] == "ready":
            meta = {"request_id": request_id, **result["meta"]}
            _set_status(request_id, status="ready", meta=meta)
        else:
            _set_status(request_id, status="failed", error=result["error"], coverage=result.get("coverage"))
    except Exception as exc:
        traceback.print_exc()
        _set_status(request_id, status="failed", error=str(exc))


@app.post("/api/requests")
def create_request(body: RequestBody):
    bbox = tuple(body.bbox)
    try:
        start_dt = datetime.strptime(body.start, "%Y-%m-%d")
        end_dt = datetime.strptime(body.end, "%Y-%m-%d").replace(hour=23, minute=59)
    except ValueError:
        raise HTTPException(400, "start/end must be YYYY-MM-DD")
    if end_dt <= start_dt:
        raise HTTPException(400, "end must be after start")

    request_id = request_id_for(bbox, body.start, body.end)
    out_dir = REQUESTS_ROOT / request_id
    meta_path = out_dir / "meta.json"

    with _jobs_lock:
        existing = _jobs.get(request_id)
    if existing and existing["status"] != "failed":
        return {"request_id": request_id, "status": existing["status"]}

    if meta_path.exists():
        with open(meta_path) as f:
            meta = json.load(f)
        _set_status(request_id, status="ready", meta=meta)
        return {"request_id": request_id, "status": "ready"}

    _set_status(request_id, status="pending")
    thread = threading.Thread(
        target=_run_pipeline, args=(request_id, bbox, start_dt, end_dt), daemon=True
    )
    thread.start()
    return {"request_id": request_id, "status": "pending"}


@app.get("/api/requests/{request_id}")
def get_request(request_id: str):
    with _jobs_lock:
        job = _jobs.get(request_id)
    if job is not None:
        return {"request_id": request_id, **job}

    meta_path = REQUESTS_ROOT / request_id / "meta.json"
    if meta_path.exists():
        with open(meta_path) as f:
            meta = json.load(f)
        return {"request_id": request_id, "status": "ready", "meta": meta}

    raise HTTPException(404, "unknown request_id")


app.mount("/data/requests", StaticFiles(directory=str(REQUESTS_ROOT)), name="requests")
app.mount("/data/infrastructure", StaticFiles(directory=str(INFRA_ROOT)), name="infrastructure")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
