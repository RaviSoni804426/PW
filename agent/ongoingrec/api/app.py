"""The local HTTP API (PRD sections 20 and 21).

Bound to loopback. In production the backend reaches this laptop through the
outbound poller, not by connecting inward, so nothing here needs to be
exposed to the network. What it is for is proving the retrieval path works --
on a developer's machine, and on a counsellor's laptop during support, where
being able to ask for a clip directly is the fastest way to tell whether a
problem is in recording or in transport.

Both this and the poller call the same :func:`~ongoingrec.extract.extract_clip`,
so there is exactly one implementation of "timestamp to audio".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from ..config import AGENT_VERSION, Config, ConfigError, Secrets
from ..extract import ExtractionError, NoRecordingError, extract_clip
from ..index import Database
from ..logging_setup import get_logger
from ..retention import free_disk_mb
from ..timeutil import format_utc, local_offset_name
from .models import ErrorResponse, FetchRequest, HealthResponse

log = get_logger(__name__)


@dataclass
class AppContext:
    """What the API needs from the running service.

    Both ``recorder`` and ``poller`` are optional so the API can be exercised
    against a recorded corpus with nothing else running, which is how the
    tests use it.
    """

    config: Config
    db: Database
    recorder: object | None = None
    poller: object | None = None


def create_app(context: AppContext) -> FastAPI:
    app = FastAPI(
        title="PW OngoingRec",
        version=AGENT_VERSION,
        description="Local recording service: health and timestamped clip retrieval.",
        docs_url="/docs",
    )
    app.state.context = context

    def require_token(authorization: str | None = Header(default=None)) -> None:
        """Optional bearer auth for the loopback API.

        Off by default -- on loopback the OS already restricts who can
        connect. When a token is configured (a shared or multi-user machine),
        it is enforced on retrieval but never on /health, which monitoring
        needs to reach unauthenticated.
        """
        expected = context.config.local_api_token
        if not expected:
            return
        if authorization != f"Bearer {expected}":
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    # PRD section 28 asks for 400 on a malformed request; FastAPI's default
    # for a schema violation is 422, so it is remapped here.
    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        messages = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
            messages.append(f"{location}: {error.get('msg')}" if location else error.get("msg", ""))
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(error="invalid_request", detail="; ".join(messages)).model_dump(),
        )

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        config = context.config
        recorder_status = context.recorder.status() if context.recorder else {}
        latest = context.db.latest_segment()

        # Enrolment is read from stored secrets rather than from the poller's
        # in-memory flag: a laptop that registered days ago is still
        # registered, whether or not the poller has completed a cycle since
        # this process started.
        try:
            registered = bool(Secrets.load(config.home).device_token)
        except ConfigError:
            registered = False
        backend_error = getattr(context.poller, "last_error", None)
        return HealthResponse(
            status="ok",
            version=AGENT_VERSION,
            install_id=config.install_id,
            email_id=config.email_id,
            employee_id=config.employee_id,
            recording=bool(recorder_status.get("recording", False)),
            device=recorder_status.get("device"),
            current_segment_start=recorder_status.get("current_segment_start"),
            last_segment_end=format_utc(latest.effective_end) if latest else None,
            segment_count=context.db.segment_count(),
            free_disk_mb=round(free_disk_mb(config.recordings_dir), 1),
            registered=registered,
            local_time_offset=local_offset_name(),
            detail={
                "captured_seconds": recorder_status.get("captured_seconds", 0),
                "padded_seconds": recorder_status.get("padded_seconds", 0),
                "last_error": recorder_status.get("last_error"),
                "backend_error": backend_error,
                "retention_hours": config.retention_hours,
                "retrieval_window_seconds": config.retrieval_window_seconds,
            },
        )

    @app.post(
        "/recordings/fetch",
        responses={
            200: {"content": {"audio/mpeg": {}}, "description": "The requested clip"},
            400: {"model": ErrorResponse},
            401: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    def fetch(request: FetchRequest, _: None = Depends(require_token)) -> FileResponse:
        config = context.config

        # PRD section 14: identifiers must resolve to *this* installation, and
        # when both are given they must agree. A mismatch is refused rather
        # than resolved in the requester's favour -- returning one counsellor's
        # audio under another's identifier is the worst failure this service
        # could have.
        if not config.identity_matches(request.email_id, request.employee_id):
            raise HTTPException(
                status_code=404,
                detail=(
                    "the supplied identifiers do not match this installation "
                    f"({config.employee_id})"
                ),
            )

        try:
            clip = extract_clip(
                config,
                context.db,
                request.requested_at,
                window_seconds=request.window_seconds,
                output_dir=config.data_dir / "clips",
            )
        except NoRecordingError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ExtractionError as exc:
            log.error("extraction failed: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        filename = f"{config.employee_id}_{clip.start.strftime('%Y%m%dT%H%M%SZ')}.mp3"
        return FileResponse(
            path=clip.path,
            media_type="audio/mpeg",
            filename=filename,
            headers={
                "X-OngoingRec-Clip-Start": format_utc(clip.start),
                "X-OngoingRec-Clip-End": format_utc(clip.end),
                "X-OngoingRec-Requested-At": format_utc(clip.requested_at),
                "X-OngoingRec-Partial": "true" if clip.is_partial else "false",
                "X-OngoingRec-Gaps": json.dumps([gap.to_dict() for gap in clip.gaps]),
                "X-OngoingRec-Segments": ",".join(str(i) for i in clip.segment_ids),
            },
            # The clip is a temporary rendering; delete it once it is sent so
            # a laptop polled often does not slowly fill with old clips.
            background=BackgroundTask(_remove, clip.path),
        )

    return app


def _remove(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not remove temporary clip %s: %s", path, exc)
