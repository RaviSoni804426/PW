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
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .settings import Settings
from .store import JOB_COMPLETE, JOB_FAILED, Session, Store, format_utc

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


class SessionStart(BaseModel):
    """Open a window.

    Date and time arrive separately and both are required. An operator is
    reading them off a form or a ticket in that shape, and a single combined
    field invites the two mistakes this split makes impossible: an omitted
    time silently meaning midnight, and a date typed in the wrong order
    passing validation as a different valid date.
    """

    email_id: str | None = None
    employee_id: str | None = None
    start_date: str = Field(description="IST calendar date, YYYY-MM-DD")
    start_time: str = Field(description="IST clock time, HH:MM:SS or HH:MM")


class SessionEnd(BaseModel):
    """Close the open window and collect its audio."""

    email_id: str | None = None
    employee_id: str | None = None
    end_date: str = Field(description="IST calendar date, YYYY-MM-DD")
    end_time: str = Field(description="IST clock time, HH:MM:SS or HH:MM")


class SessionCancel(BaseModel):
    email_id: str | None = None
    employee_id: str | None = None


# Session times are always IST, so they are unambiguous without the caller
# having to say so. A fixed offset rather than a tz database lookup: India has
# had no DST since 1945 and no plans for it, and a fixed offset needs no
# `tzdata` package -- which Windows does not ship, and this backend is expected
# to run there during testing.
IST = timezone(timedelta(hours=5, minutes=30), "IST")


# A session becomes one clip, and a clip has to survive the upload limit. At
# the agent's 32 kbps that is about 13.7 MB an hour, so the default 128 MB cap
# is reached somewhere past nine hours. Eight is the largest round number
# comfortably inside it; a longer window is refused when it is requested rather
# than after the laptop has spent minutes encoding something it cannot send.
MAX_SESSION_SECONDS = 8 * 3600
MIN_SESSION_SECONDS = 1

# A start further ahead than this is far more likely to be a typo -- a wrong
# date, a local time sent as UTC -- than a genuine intention.
MAX_SESSION_LEAD_SECONDS = 24 * 3600


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
        return await _await_clip(job, device.employee_id, wait_seconds)

    async def _await_clip(job, employee_id: str, wait_seconds: int):
        """Hold the request open until the laptop delivers, or give up with 202.

        Shared by the one-step fetch and by ending a session, so both agree on
        what a switched-off laptop means: not a failure, just a job that will
        be fulfilled later.
        """
        deadline = asyncio.get_event_loop().time() + wait_seconds
        while True:
            current = await asyncio.to_thread(store.get_job, job.job_id)
            if current is not None and current.status == JOB_COMPLETE:
                return _clip_response(current, employee_id)
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
                "employee_id": employee_id,
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

    # -- sessions: mark a window now, collect its audio later ---------------
    #
    # The microphone is never actually started or stopped by any of this. The
    # agent records continuously, so a "session" only records the operator's
    # intent to keep a particular stretch. Two consequences worth knowing:
    # cancelling destroys nothing, and a start time may be in the future.

    def _resolve_device(employee_id: str | None, email_id: str | None):
        if not (employee_id or email_id):
            raise HTTPException(status_code=400, detail="employee_id or email_id is required")
        device = store.device_by_identifier(employee_id, email_id)
        if device is None:
            raise HTTPException(
                status_code=404, detail="no registered device for that identifier"
            )
        return device

    def _ist_to_utc(date_value: str, time_value: str, prefix: str) -> datetime:
        """Combine an IST calendar date and clock time into an aware UTC instant.

        Both halves are parsed strictly. A lenient parser here would accept
        "18-08-2026" as some other real date and return audio from a day the
        caller never asked for, which no status code would reveal.
        """
        date_text = (date_value or "").strip()
        time_text = (time_value or "").strip()

        try:
            day = datetime.strptime(date_text, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{prefix}_date must be YYYY-MM-DD (e.g. '2026-08-18'), "
                    f"got {date_value!r}"
                ),
            ) from None

        clock = None
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                clock = datetime.strptime(time_text, fmt).time()
                break
            except ValueError:
                continue
        if clock is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{prefix}_time must be HH:MM:SS or HH:MM in 24-hour IST "
                    f"(e.g. '14:30:00'), got {time_value!r}"
                ),
            )

        return datetime.combine(day, clock, tzinfo=IST).astimezone(timezone.utc)

    def _ist_parts(value: datetime) -> tuple[str, str]:
        """Render a stored UTC instant back as the IST date and time it came from."""
        local = value.astimezone(IST)
        return local.strftime("%Y-%m-%d"), local.strftime("%H:%M:%S")

    def _session_public(session: Session, now: datetime) -> dict[str, Any]:
        """The session as the caller sees it: their own IST values, plus the UTC.

        Both, because the IST pair is what was sent and what a human checks
        against a ticket, while the UTC is what every other endpoint, header
        and log line in this system speaks.
        """
        start = _parse_stored(session.start_utc)
        start_date, start_time = _ist_parts(start)
        end_date = end_time = None
        if session.end_utc:
            end_date, end_time = _ist_parts(_parse_stored(session.end_utc))
        return {
            "session_id": session.session_id,
            "employee_id": session.employee_id,
            "status": session.state(now),
            "start_date": start_date,
            "start_time": start_time,
            "start_utc": session.start_utc,
            "end_date": end_date,
            "end_time": end_time,
            "end_utc": session.end_utc,
            "job_id": session.job_id,
            "created_at": session.created_at,
        }

    def _parse_stored(value: str) -> datetime:
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def _format_instant(value: datetime) -> str:
        """UTC, keeping fractional seconds only when there are any.

        ``format_utc`` truncates to whole seconds, which is right for anything
        a human reads and wrong for a clip's centre point: an odd-length window
        puts the midpoint on a half second, and dropping it shifts the returned
        clip half a second earlier than the window that was asked for.
        """
        value = value.astimezone(timezone.utc)
        if value.microsecond:
            return value.strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0") + "Z"
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _in_progress_conflict(session: Session, now: datetime) -> HTTPException:
        state = session.state(now)
        when = " ".join(_ist_parts(_parse_stored(session.start_utc))) + " IST"
        if state == "scheduled":
            detail = (
                f"a recording is already scheduled to start at "
                f"{when} for {session.employee_id}"
            )
        else:
            detail = (
                f"a recording is already in progress for {session.employee_id}, "
                f"started at {when}"
            )
        return HTTPException(
            status_code=409,
            detail=detail,
            headers={"X-OngoingRec-Session-Id": session.session_id},
        )

    @app.post(
        "/admin/sessions/start",
        tags=["admin"],
        status_code=201,
        dependencies=[Depends(require_admin)],
        responses={409: {"description": "a session is already open for this person"}},
    )
    async def session_start(body: SessionStart) -> dict[str, Any]:
        """Mark where the audio you want begins."""
        device = await asyncio.to_thread(_resolve_device, body.employee_id, body.email_id)
        now = datetime.now(timezone.utc)
        start = _ist_to_utc(body.start_date, body.start_time, "start")

        if (start - now).total_seconds() > MAX_SESSION_LEAD_SECONDS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"start {body.start_date} {body.start_time} IST is more than "
                    f"{MAX_SESSION_LEAD_SECONDS // 3600}h in the future; "
                    f"that is usually a typo"
                ),
            )

        session = await asyncio.to_thread(
            store.open_session,
            employee_id=device.employee_id,
            install_id=device.install_id,
            start=start,
        )
        if session is None:
            existing = await asyncio.to_thread(store.active_session, device.employee_id)
            if existing is None:
                raise HTTPException(status_code=409, detail="a session was opened concurrently")
            error = _in_progress_conflict(existing, now)
            existing_start_date, existing_start_time = _ist_parts(
                _parse_stored(existing.start_utc)
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "detail": error.detail,
                    "session_id": existing.session_id,
                    "status": existing.state(now),
                    "start_date": existing_start_date,
                    "start_time": existing_start_time,
                    "start_utc": existing.start_utc,
                    "hint": (
                        "POST /admin/sessions/cancel to discard it and start a new "
                        "one, or POST /admin/sessions/end to close it and get the audio"
                    ),
                },
            )

        log.info(
            "session %s opened for %s at %s",
            session.session_id,
            device.employee_id,
            session.start_utc,
        )
        return _session_public(session, now)

    @app.post(
        "/admin/sessions/end",
        tags=["admin"],
        dependencies=[Depends(require_admin)],
        responses={
            200: {"content": {"audio/mpeg": {}}, "description": "The session's audio"},
            202: {"description": "Laptop has not delivered yet; collect it later"},
            409: {"description": "no session is open for this person"},
        },
    )
    async def session_end(
        body: SessionEnd,
        wait_seconds: int = Query(default=DEFAULT_FETCH_WAIT, ge=0, le=MAX_FETCH_WAIT),
    ):
        """Close the open window and hand back the audio inside it."""
        device = await asyncio.to_thread(_resolve_device, body.employee_id, body.email_id)
        now = datetime.now(timezone.utc)
        session = await asyncio.to_thread(store.active_session, device.employee_id)
        if session is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"no recording is in progress for {device.employee_id}; "
                    f"POST /admin/sessions/start first"
                ),
            )

        end = _ist_to_utc(body.end_date, body.end_time, "end")
        if end > now:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"end {body.end_date} {body.end_time} IST is in the future; "
                    f"that audio has not been recorded yet"
                ),
            )

        start = _parse_stored(session.start_utc)
        if start >= now:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"this recording is scheduled to start at "
                    f"{' '.join(_ist_parts(start))} IST and has not begun; "
                    f"cancel it instead of ending it"
                ),
            )

        duration = (end - start).total_seconds()
        if duration < MIN_SESSION_SECONDS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"the end must be at least {MIN_SESSION_SECONDS}s after the "
                    f"start {' '.join(_ist_parts(start))} IST"
                ),
            )
        if duration > MAX_SESSION_SECONDS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"the session covers {duration / 3600:.1f}h, over the "
                    f"{MAX_SESSION_SECONDS // 3600}h limit one clip can carry. "
                    f"Cancel it and collect the period in shorter pieces with "
                    f"/admin/recordings/fetch."
                ),
            )

        # extract_clip centres its window on the timestamp it is given, so a
        # start/end pair travels as its midpoint plus its length. An odd number
        # of seconds puts the midpoint on a half second, which the whole-second
        # format would silently drop -- shifting the returned clip half a second
        # earlier than the window that was actually asked for. So the midpoint
        # keeps sub-second precision whenever it has any.
        window = int(round(duration))
        midpoint = start + timedelta(seconds=window / 2)

        job = await asyncio.to_thread(
            store.create_job, device.install_id, _format_instant(midpoint), window
        )
        closed = await asyncio.to_thread(
            store.close_session,
            session.session_id,
            end=end,
            job_id=job.job_id,
            install_id=device.install_id,
        )
        if closed is None:
            # Cancelled or ended by another caller between the read and here.
            raise HTTPException(
                status_code=409, detail="the session was closed by someone else"
            )

        log.info(
            "session %s ended for %s: %s..%s (%ds) as job %s",
            session.session_id,
            device.employee_id,
            session.start_utc,
            format_utc(end),
            window,
            job.job_id,
        )
        response = await _await_clip(job, device.employee_id, wait_seconds)
        if isinstance(response, JSONResponse):
            payload = json.loads(bytes(response.body))
            payload["session_id"] = session.session_id
            payload["start_date"], payload["start_time"] = _ist_parts(start)
            payload["end_date"], payload["end_time"] = _ist_parts(end)
            payload["start_utc"] = session.start_utc
            payload["end_utc"] = format_utc(end)
            return JSONResponse(status_code=response.status_code, content=payload)
        response.headers["X-OngoingRec-Session-Id"] = session.session_id
        return response

    @app.post(
        "/admin/sessions/cancel",
        tags=["admin"],
        dependencies=[Depends(require_admin)],
        responses={404: {"description": "nothing was open to cancel"}},
    )
    async def session_cancel(body: SessionCancel) -> dict[str, Any]:
        """Discard the open window. No audio is lost -- the laptop never stopped."""
        device = await asyncio.to_thread(_resolve_device, body.employee_id, body.email_id)
        now = datetime.now(timezone.utc)
        session = await asyncio.to_thread(store.active_session, device.employee_id)
        if session is None:
            raise HTTPException(
                status_code=404,
                detail=f"no recording is in progress for {device.employee_id}",
            )

        was = session.state(now)
        cancelled = await asyncio.to_thread(store.cancel_session, session.session_id)
        if cancelled is None:
            raise HTTPException(
                status_code=409, detail="the session was closed by someone else"
            )
        log.info("session %s cancelled for %s", session.session_id, device.employee_id)
        body_out = _session_public(cancelled, now)
        body_out["was"] = was
        body_out["started"] = was == "recording"
        return body_out

    @app.get("/admin/sessions", tags=["admin"], dependencies=[Depends(require_admin)])
    async def sessions(
        employee_id: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        rows = await asyncio.to_thread(store.recent_sessions, employee_id, limit)
        return [_session_public(s, now) for s in rows]

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
