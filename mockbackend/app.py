"""Mock implementation of the OngoingRec backend contract."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

DEFAULT_ENROLLMENT_KEY = "dev-enrollment-key"


@dataclass
class Device:
    install_id: str
    email_id: str
    employee_id: str
    hostname: str
    device_token: str
    registered_at: str
    last_heartbeat: dict[str, Any] | None = None


@dataclass
class Job:
    job_id: str
    install_id: str
    timestamp: str
    window_seconds: int | None
    status: str = "queued"
    error: dict[str, str] | None = None
    clip_path: Path | None = None
    clip_metadata: dict[str, Any] | None = None
    clip_bytes: int = 0


@dataclass
class State:
    enrollment_key: str = DEFAULT_ENROLLMENT_KEY
    devices: dict[str, Device] = field(default_factory=dict)  # by device_token
    by_install: dict[str, Device] = field(default_factory=dict)
    by_employee: dict[str, Device] = field(default_factory=dict)
    by_email: dict[str, Device] = field(default_factory=dict)
    queues: dict[str, list[Job]] = field(default_factory=dict)  # install_id -> pending
    jobs: dict[str, Job] = field(default_factory=dict)
    uploads_dir: Path | None = None


class ClipRequest(BaseModel):
    """What an operator or the real backend's UI would submit."""

    email_id: str | None = None
    employee_id: str | None = None
    timestamp: str
    window_seconds: int | None = None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_mock_backend(uploads_dir: Path | None = None, enrollment_key: str | None = None):
    """Build the mock app. Returns ``(app, state)`` so tests can inspect state."""
    state = State(
        enrollment_key=enrollment_key or DEFAULT_ENROLLMENT_KEY,
        uploads_dir=Path(uploads_dir) if uploads_dir else None,
    )
    if state.uploads_dir:
        state.uploads_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="Mock OngoingRec Backend", version="1.0.0")
    app.state.mock = state

    def authenticate(authorization: str | None) -> Device:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        device = state.devices.get(authorization.removeprefix("Bearer "))
        if device is None:
            raise HTTPException(status_code=403, detail="unknown device token")
        return device

    # -- device lifecycle -------------------------------------------------

    @app.post("/devices/register")
    async def register(request: Request):
        payload = await request.json()
        if payload.get("enrollment_key") != state.enrollment_key:
            raise HTTPException(status_code=401, detail="bad enrollment key")
        for required in ("install_id", "email_id", "employee_id"):
            if not payload.get(required):
                raise HTTPException(status_code=400, detail=f"{required} is required")

        device = Device(
            install_id=payload["install_id"],
            email_id=payload["email_id"],
            employee_id=payload["employee_id"],
            hostname=payload.get("hostname", ""),
            device_token=f"tok-{uuid.uuid4().hex}",
            registered_at=_now(),
        )
        state.devices[device.device_token] = device
        state.by_install[device.install_id] = device
        # This mapping is the routing PRD section 23 needs: it is how an
        # employee ID becomes a specific laptop's job queue.
        state.by_employee[device.employee_id.strip().casefold()] = device
        state.by_email[device.email_id.strip().lower()] = device
        state.queues.setdefault(device.install_id, [])
        return {"device_token": device.device_token, "registered_at": device.registered_at}

    @app.post("/devices/heartbeat")
    async def heartbeat(request: Request, authorization: str | None = Header(default=None)):
        device = authenticate(authorization)
        device.last_heartbeat = {"received_at": _now(), **(await request.json())}
        return {"ok": True}

    # -- job delivery -----------------------------------------------------

    @app.get("/jobs/poll")
    async def poll(
        install_id: str = Query(...),
        wait: int = Query(default=30, ge=0, le=120),
        authorization: str | None = Header(default=None),
    ):
        device = authenticate(authorization)
        if device.install_id != install_id:
            raise HTTPException(status_code=403, detail="token does not match install_id")

        # Hold the connection open until work appears, which is what lets a
        # laptop respond within seconds without polling in a tight loop.
        deadline = asyncio.get_event_loop().time() + wait
        while True:
            queue = state.queues.setdefault(install_id, [])
            if queue:
                jobs = [
                    {
                        "job_id": job.job_id,
                        "timestamp": job.timestamp,
                        "window_seconds": job.window_seconds,
                    }
                    for job in queue
                ]
                for job in queue:
                    job.status = "delivered"
                queue.clear()
                return {"jobs": jobs}
            if asyncio.get_event_loop().time() >= deadline:
                return JSONResponse(status_code=200, content={"jobs": []})
            await asyncio.sleep(0.05)

    @app.post("/jobs/{job_id}/clip")
    async def upload_clip(
        job_id: str,
        clip: UploadFile = File(...),
        metadata: str = Form(...),
        authorization: str | None = Header(default=None),
    ):
        authenticate(authorization)
        job = state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")

        payload = await clip.read()
        job.clip_bytes = len(payload)
        job.clip_metadata = json.loads(metadata)
        job.status = "complete"
        if state.uploads_dir:
            destination = state.uploads_dir / f"{job_id}.mp3"
            destination.write_bytes(payload)
            job.clip_path = destination
        return {"ok": True, "bytes": len(payload)}

    @app.post("/jobs/{job_id}/error")
    async def report_error(
        job_id: str, request: Request, authorization: str | None = Header(default=None)
    ):
        authenticate(authorization)
        job = state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        job.error = await request.json()
        job.status = "failed"
        return {"ok": True}

    # -- operator surface -------------------------------------------------

    @app.post("/admin/request-clip")
    async def request_clip(body: ClipRequest):
        """Queue a clip request, resolving the identifier to a laptop.

        This is the part of the real backend that PRD section 23 describes:
        an employee ID arrives, and the backend already knows which
        installation it belongs to because that device registered itself.
        """
        device = None
        if body.employee_id:
            device = state.by_employee.get(body.employee_id.strip().casefold())
        if device is None and body.email_id:
            device = state.by_email.get(body.email_id.strip().lower())
        if device is None:
            raise HTTPException(status_code=404, detail="no registered device for that identifier")

        job = Job(
            job_id=f"job-{uuid.uuid4().hex[:12]}",
            install_id=device.install_id,
            timestamp=body.timestamp,
            window_seconds=body.window_seconds,
        )
        state.jobs[job.job_id] = job
        state.queues.setdefault(device.install_id, []).append(job)
        return {"job_id": job.job_id, "install_id": device.install_id}

    @app.get("/admin/jobs/{job_id}")
    async def job_status(job_id: str):
        job = state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return {
            "job_id": job.job_id,
            "status": job.status,
            "error": job.error,
            "clip_bytes": job.clip_bytes,
            "clip_path": str(job.clip_path) if job.clip_path else None,
            "metadata": job.clip_metadata,
        }

    @app.get("/admin/jobs/{job_id}/clip")
    async def download_clip(job_id: str):
        """Hand back the stored MP3.

        ``job_status`` reports where the clip landed, but that path only means
        anything on the machine running this mock. Whoever is integrating
        against it is on a different machine, so without this they can see that
        a clip arrived and still have no way to hear it.
        """
        job = state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        if job.clip_path is None or not Path(job.clip_path).exists():
            raise HTTPException(
                status_code=404,
                detail=f"no clip stored for this job (status: {job.status})",
            )
        return FileResponse(
            path=job.clip_path,
            media_type="audio/mpeg",
            filename=f"{job_id}.mp3",
        )

    @app.get("/admin/devices")
    async def devices():
        return [
            {
                "install_id": d.install_id,
                "email_id": d.email_id,
                "employee_id": d.employee_id,
                "hostname": d.hostname,
                "registered_at": d.registered_at,
                "last_heartbeat": d.last_heartbeat,
            }
            for d in state.by_install.values()
        ]

    return app, state
