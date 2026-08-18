"""The OngoingRec central backend.

Implements ``docs/backend-api.yaml`` -- the contract counsellor laptops speak
-- plus an ``/admin`` surface for whoever is actually asking for audio.

Every agent call is outbound. Laptops sit behind NAT with no stable address,
so this server never connects to one: it parks a job on a queue and the laptop
collects it on its next long poll. That is also where the routing comes from,
since a laptop reports its employee ID when it enrols.

Two credentials, doing different jobs:

* the **enrollment key** is fleet-wide, ships in the installer, and is only
  ever accepted by ``/devices/register``. Assume it leaks eventually -- it
  grants nothing but the right to enrol.
* the **admin API key** guards ``/admin``. It is the one that can reach real
  recordings, so it is required on every admin route including the read-only
  ones, and the server refuses to start without it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets as pysecrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .settings import Settings
from .store import JOB_COMPLETE, JOB_FAILED, Store

log = logging.getLogger("ongoingrec.backend")

POLL_INTERVAL_SECONDS = 0.25
PURGE_INTERVAL_SECONDS = 6 * 3600


class ClipRequest(BaseModel):
    """What an operator submits to ask for a counsellor's audio."""

    email_id: str | None = None
    employee_id: str | None = None
    timestamp: str
    window_seconds: int | None = Field(default=None, ge=1, le=7200)


class ErrorReport(BaseModel):
    code: str
    detail: str | None = None


# How long a one-step fetch waits for the laptop before handing back a job id
# instead. Generous, because the agent will not cut a clip until the requested
# window has finished happening plus its flush margin -- ask for a moment 30
# seconds ago and roughly a minute of the wait is that, before any work starts.
DEFAULT_FETCH_WAIT = 120
MAX_FETCH_WAIT = 600
FETCH_POLL_SECONDS = 0.5


def create_app(settings: Settings) -> FastAPI:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.clips_dir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        purge = asyncio.create_task(_purge_loop(store, settings))
        try:
            yield
        finally:
            purge.cancel()
            store.close()

    app = FastAPI(
        title="PW OngoingRec Backend",
        version="1.0.0",
        description="Clip retrieval for OngoingRec recording agents.",
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.settings = settings

    # -- authentication ---------------------------------------------------

    def require_device(authorization: str | None = Header(default=None)):
        """Authenticate a laptop by its per-device token."""
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        device = store.device_by_token(authorization.removeprefix("Bearer ").strip())
        if device is None:
            # 401 rather than 403 on purpose: it tells the agent its credential
            # is no longer valid, which is what makes it drop the stale token
            # and re-enrol instead of retrying the same rejected one forever.
            raise HTTPException(status_code=401, detail="unknown or revoked device token")
        return device

    def require_admin(
        x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
        authorization: str | None = Header(default=None),
    ) -> None:
        """Authorize an operator, if this deployment is gated at all.

        With no ``ADMIN_API_KEY`` configured the admin surface is open and
        anyone who has the URL can fetch a clip -- which is the intended
        arrangement here, and why the URL has to be treated as the secret.

        When a key is configured it is accepted in either header, so a browser
        tool and a curl script can both send it, and compared with
        ``compare_digest`` so a wrong key takes the same time to reject however
        much of it happened to be right.
        """
        if settings.admin_open:
            return
        supplied = x_admin_key or ""
        if not supplied and authorization and authorization.startswith("Bearer "):
            supplied = authorization.removeprefix("Bearer ").strip()
        if not supplied or not pysecrets.compare_digest(supplied, settings.admin_api_key):
            raise HTTPException(
                status_code=401,
                detail="missing or invalid admin key (send X-Admin-Key)",
            )

    # -- operations -------------------------------------------------------

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, Any]:
        """Liveness for the platform's health check. Deliberately unauthenticated
        and deliberately free of counsellor data."""
        return {"status": "ok", **store.counts()}

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {
            "service": "PW OngoingRec Backend",
            "docs": "/docs",
            "health": "/healthz",
        }

    # -- device lifecycle -------------------------------------------------

    @app.post("/devices/register", tags=["agent"])
    async def register(request: Request) -> dict[str, str]:
        payload = await request.json()
        # Unset enrollment key means any laptop may enrol. Whatever the agent
        # sends is then ignored rather than rejected, so the same installer
        # works whether or not this deployment gates registration.
        if not settings.registration_open and not pysecrets.compare_digest(
            str(payload.get("enrollment_key", "")), settings.enrollment_key
        ):
            raise HTTPException(status_code=401, detail="bad enrollment key")
        for required in ("install_id", "email_id", "employee_id"):
            if not payload.get(required):
                raise HTTPException(status_code=400, detail=f"{required} is required")

        device = store.register(
            install_id=str(payload["install_id"]),
            email_id=str(payload["email_id"]),
            employee_id=str(payload["employee_id"]),
            hostname=str(payload.get("hostname", "")),
            os_version=str(payload.get("os_version", "")),
            agent_version=str(payload.get("agent_version", "")),
        )
        log.info("registered %s as %s", device.install_id, device.employee_id)
        return {"device_token": device.device_token, "registered_at": device.registered_at}

    @app.post("/devices/heartbeat", tags=["agent"])
    async def heartbeat(request: Request, device=Depends(require_device)) -> dict[str, bool]:
        store.record_heartbeat(device.install_id, await request.json())
        return {"ok": True}

    # -- job delivery -----------------------------------------------------

    @app.get("/jobs/poll", tags=["agent"])
    async def poll(
        install_id: str = Query(...),
        wait: int = Query(default=30, ge=0, le=120),
        device=Depends(require_device),
    ) -> dict[str, list[dict[str, Any]]]:
        """Long poll. Held open until work arrives or *wait* elapses."""
        if device.install_id != install_id:
            raise HTTPException(status_code=403, detail="token does not match install_id")

        deadline = asyncio.get_event_loop().time() + min(wait, settings.max_poll_wait)
        while True:
            jobs = await asyncio.to_thread(store.claim_queued, install_id)
            if jobs:
                return {
                    "jobs": [
                        {
                            "job_id": job.job_id,
                            "timestamp": job.timestamp,
                            "window_seconds": job.window_seconds,
                        }
                        for job in jobs
                    ]
                }
            if asyncio.get_event_loop().time() >= deadline:
                return {"jobs": []}
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    @app.post("/jobs/{job_id}/clip", tags=["agent"])
    async def upload_clip(
        job_id: str,
        clip: UploadFile = File(...),
        metadata: str = Form(...),
        device=Depends(require_device),
    ) -> dict[str, Any]:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        if job.install_id != device.install_id:
            raise HTTPException(status_code=403, detail="this job belongs to another device")

        payload = await clip.read()
        limit = settings.max_clip_mb * 1024 * 1024
        if len(payload) > limit:
            raise HTTPException(
                status_code=413, detail=f"clip exceeds the {settings.max_clip_mb} MB limit"
            )
        try:
            parsed = json.loads(metadata)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"metadata is not JSON: {exc}") from exc

        destination = settings.clips_dir / f"{job_id}.mp3"
        destination.write_bytes(payload)
        store.complete_job(
            job_id, clip_path=destination, clip_bytes=len(payload), metadata=parsed
        )
        log.info("job %s complete: %d bytes from %s", job_id, len(payload), device.employee_id)
        return {"ok": True, "bytes": len(payload)}

    @app.post("/jobs/{job_id}/error", tags=["agent"])
    async def report_error(
        job_id: str, body: ErrorReport, device=Depends(require_device)
    ) -> dict[str, bool]:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        store.fail_job(job_id, {"code": body.code, "detail": body.detail or ""})
        log.info("job %s failed: %s %s", job_id, body.code, body.detail)
        return {"ok": True}

    # -- operator surface -------------------------------------------------

    @app.post("/admin/request-clip", tags=["admin"], dependencies=[Depends(require_admin)])
    async def request_clip(body: ClipRequest) -> dict[str, str]:
        """Queue a clip request for whichever laptop belongs to this person."""
        if not (body.employee_id or body.email_id):
            raise HTTPException(
                status_code=400, detail="employee_id or email_id is required"
            )
        device = store.device_by_identifier(body.employee_id, body.email_id)
        if device is None:
            raise HTTPException(
                status_code=404, detail="no registered device for that identifier"
            )
        job = store.create_job(device.install_id, body.timestamp, body.window_seconds)
        log.info("queued %s for %s at %s", job.job_id, device.employee_id, body.timestamp)
        return {
            "job_id": job.job_id,
            "install_id": device.install_id,
            "employee_id": device.employee_id,
            "status": job.status,
        }

    # -- one-step retrieval -----------------------------------------------

    async def _fetch(
        employee_id: str | None,
        email_id: str | None,
        timestamp: str,
        window_seconds: int | None,
        wait_seconds: int,
    ):
        """Identifier plus timestamp in, audio out, in a single call.

        The three-step queue/poll/download flow is what the transport actually
        does, and it stays available for callers that want to fire a request
        now and collect it later. But most callers just want the audio, and
        making each of them reimplement the polling loop invites everyone to
        get the edge cases subtly wrong. So the wait happens here, once.

        The wait is bounded. When it runs out the job is *not* cancelled --
        the laptop may simply be switched off, and the request is still valid
        the moment it comes back. The caller gets 202 and the job id, so the
        work already queued is never thrown away just because one HTTP request
        gave up on it.
        """
        device = await asyncio.to_thread(store.device_by_identifier, employee_id, email_id)
        if device is None:
            raise HTTPException(
                status_code=404, detail="no registered device for that identifier"
            )

        job = await asyncio.to_thread(
            store.create_job, device.install_id, timestamp, window_seconds
        )
        log.info(
            "fetch %s for %s at %s (waiting up to %ds)",
            job.job_id,
            device.employee_id,
            timestamp,
            wait_seconds,
        )

        deadline = asyncio.get_event_loop().time() + wait_seconds
        while True:
            current = await asyncio.to_thread(store.get_job, job.job_id)
            if current is not None and current.status == JOB_COMPLETE:
                return _clip_response(current, device.employee_id)
            if current is not None and current.status == JOB_FAILED:
                error = current.error or {}
                code = error.get("code", "FAILED")
                detail = error.get("detail") or code
                # The laptop was reachable and answered honestly: there is no
                # audio for that moment. That is a 404 about the recording,
                # not a server fault.
                status = 404 if code == "NO_RECORDING" else 502
                raise HTTPException(status_code=status, detail=detail)
            if asyncio.get_event_loop().time() >= deadline:
                break
            await asyncio.sleep(FETCH_POLL_SECONDS)

        pending = await asyncio.to_thread(store.get_job, job.job_id)
        return JSONResponse(
            status_code=202,
            content={
                "job_id": job.job_id,
                "employee_id": device.employee_id,
                "status": pending.status if pending else "queued",
                "detail": (
                    "the laptop has not delivered the clip yet -- it may be switched "
                    "off. The request is still queued; collect it from "
                    f"/admin/jobs/{job.job_id}/clip when it completes."
                ),
            },
        )

    def _clip_response(job, employee_id: str) -> FileResponse:
        if not job.clip_path or not Path(job.clip_path).exists():
            raise HTTPException(status_code=500, detail="clip is recorded as stored but missing")
        meta = job.metadata or {}
        start = str(meta.get("clip_start", ""))
        # The clip alone cannot say what it is. These travel in headers so a
        # caller streaming the body straight to a file still learns which
        # window it really covers and which stretches were never recorded.
        headers = {
            "X-OngoingRec-Employee-Id": employee_id,
            "X-OngoingRec-Job-Id": job.job_id,
            "X-OngoingRec-Clip-Start": start,
            "X-OngoingRec-Clip-End": str(meta.get("clip_end", "")),
            "X-OngoingRec-Requested-At": str(meta.get("requested_at", "")),
            "X-OngoingRec-Partial": "true" if meta.get("partial") else "false",
            "X-OngoingRec-Gaps": json.dumps(meta.get("gaps", [])),
        }
        stamp = start.replace("-", "").replace(":", "").replace("Z", "Z") or job.job_id
        return FileResponse(
            path=job.clip_path,
            media_type="audio/mpeg",
            filename=f"{employee_id}_{stamp}.mp3",
            headers=headers,
        )

    @app.post(
        "/admin/recordings/fetch",
        tags=["admin"],
        dependencies=[Depends(require_admin)],
        responses={
            200: {"content": {"audio/mpeg": {}}, "description": "The requested clip"},
            202: {"description": "Laptop has not delivered yet; collect it later"},
            404: {"description": "Unknown identifier, or nothing was recorded then"},
        },
    )
    async def fetch_recording(body: ClipRequest, wait_seconds: int = Query(
        default=DEFAULT_FETCH_WAIT, ge=0, le=MAX_FETCH_WAIT
    )):
        if not (body.employee_id or body.email_id):
            raise HTTPException(status_code=400, detail="employee_id or email_id is required")
        return await _fetch(
            body.employee_id, body.email_id, body.timestamp, body.window_seconds, wait_seconds
        )

    @app.get(
        "/admin/recordings/fetch",
        tags=["admin"],
        dependencies=[Depends(require_admin)],
        responses={
            200: {"content": {"audio/mpeg": {}}, "description": "The requested clip"},
            202: {"description": "Laptop has not delivered yet; collect it later"},
            404: {"description": "Unknown identifier, or nothing was recorded then"},
        },
    )
    async def fetch_recording_get(
        timestamp: str = Query(..., description="ISO-8601; naive values are laptop local time"),
        employee_id: str | None = Query(default=None),
        email_id: str | None = Query(default=None),
        window_seconds: int | None = Query(default=None, ge=1, le=7200),
        wait_seconds: int = Query(default=DEFAULT_FETCH_WAIT, ge=0, le=MAX_FETCH_WAIT),
    ):
        """Same as the POST, as a plain URL -- easier to paste into a shell,
        a browser extension, or a spreadsheet formula."""
        if not (employee_id or email_id):
            raise HTTPException(status_code=400, detail="employee_id or email_id is required")
        return await _fetch(employee_id, email_id, timestamp, window_seconds, wait_seconds)

    @app.get("/admin/jobs/{job_id}", tags=["admin"], dependencies=[Depends(require_admin)])
    async def job_status(job_id: str) -> dict[str, Any]:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return job.public()

    @app.get(
        "/admin/jobs/{job_id}/clip",
        tags=["admin"],
        dependencies=[Depends(require_admin)],
        responses={200: {"content": {"audio/mpeg": {}}, "description": "The clip"}},
    )
    async def download_clip(job_id: str) -> FileResponse:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        if not job.clip_path or not Path(job.clip_path).exists():
            raise HTTPException(
                status_code=404,
                detail=f"no clip stored for this job (status: {job.status})",
            )
        return FileResponse(
            path=job.clip_path, media_type="audio/mpeg", filename=f"{job_id}.mp3"
        )

    @app.get("/admin/devices", tags=["admin"], dependencies=[Depends(require_admin)])
    async def devices() -> list[dict[str, Any]]:
        return [d.public() for d in store.all_devices()]

    @app.get("/admin/jobs", tags=["admin"], dependencies=[Depends(require_admin)])
    async def jobs(limit: int = Query(default=50, ge=1, le=500)) -> list[dict[str, Any]]:
        return [j.public() for j in store.recent_jobs(limit)]

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    return app


async def _purge_loop(store: Store, settings: Settings) -> None:
    """Drop finished jobs and their audio once they are old enough.

    Recordings should not pile up on a server indefinitely just because no
    one wrote the cleanup; this bounds how long a clip outlives the request
    that produced it.
    """
    while True:
        try:
            await asyncio.sleep(PURGE_INTERVAL_SECONDS)
            removed = await asyncio.to_thread(store.purge_old_jobs, settings.job_retention_days)
            if removed:
                log.info("purged %d job(s) older than %d days", removed, settings.job_retention_days)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 - a failed purge must not kill the server
            log.exception("purge failed")
