"""The outbound job loop: register, poll, extract, upload.

This is the production retrieval path. It runs on its own thread for the life
of the service and is responsible for every promise made to the backend:

* A job is never fulfilled twice, however many times it is delivered.
* A job survives a restart -- accepted before a reboot, uploaded after it.
* A job whose audio cannot exist is reported, not left silently unanswered.
* A window that has not finished happening yet is waited for, not failed.
"""

from __future__ import annotations

import random
import threading
import time
from datetime import timedelta
from pathlib import Path

from ..config import Config, Secrets
from ..extract import ExtractionError, NoRecordingError, extract_clip
from ..index import JOB_DONE, JOB_FAILED, JOB_UPLOADING, Database
from ..logging_setup import get_logger
from ..timeutil import ensure_utc, parse_timestamp, utcnow
from .client import AuthError, BackendClient, BackendError

log = get_logger(__name__)

# A clip cannot be cut until the window it covers has actually elapsed and the
# recorder has checkpointed that far. This margin sits above the recorder's
# 30-second progress checkpoint so a request for a just-passed moment waits
# rather than being reported as missing.
FLUSH_MARGIN_SECONDS = 45.0

MAX_BACKOFF_SECONDS = 1800.0


class JobPoller:
    """Long-polls the backend and fulfils clip requests."""

    def __init__(
        self,
        config: Config,
        db: Database,
        *,
        recorder=None,
        client_factory=None,
    ):
        self.config = config
        self.db = db
        self.recorder = recorder
        self._client_factory = client_factory or self._default_client
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client: BackendClient | None = None
        self._last_heartbeat = 0.0
        self.registered = False
        self.last_error: str | None = None

    def _default_client(self, device_token: str) -> BackendClient:
        return BackendClient(self.config, device_token=device_token)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("poller already started")
        self._thread = threading.Thread(target=self.run, name="poller", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        if self._client is not None:
            self._client.close()
            self._client = None
        self._thread = None

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except AuthError as exc:
                # A rejected token is not something retrying quickly fixes;
                # back off hard and keep recording regardless, so audio is
                # still there when the credential problem is sorted out.
                self.last_error = str(exc)
                log.error("backend rejected our credentials: %s", exc)
                self._stop.wait(300)
            except BackendError as exc:
                self.last_error = str(exc)
                log.warning("backend unreachable: %s", exc)
                self._stop.wait(self.config.poll_retry_seconds)
            except Exception:  # noqa: BLE001 - the loop must outlive surprises
                log.exception("unexpected failure in the poll loop")
                self._stop.wait(self.config.poll_retry_seconds)

    # -- one cycle ---------------------------------------------------------

    def tick(self) -> None:
        """One poll cycle. Separated from :meth:`run` so tests can step it."""
        client = self._ensure_client()
        self._maybe_heartbeat(client)

        for job in client.poll_jobs():
            window = job.window_seconds or self.config.retrieval_window_seconds
            try:
                requested_at = parse_timestamp(job.timestamp)
            except ValueError as exc:
                log.error("job %s has an unusable timestamp: %s", job.job_id, exc)
                self._safe_report(client, job.job_id, "INVALID_REQUEST", str(exc))
                continue
            if self.db.upsert_job(job.job_id, requested_at, window):
                log.info("accepted job %s for %s", job.job_id, job.timestamp)

        self._process_pending(client)

    def _ensure_client(self) -> BackendClient:
        secrets = Secrets.load(self.config.home)
        if self._client is None:
            self._client = self._client_factory(secrets.device_token)

        if not secrets.device_token:
            token = self._client.register(secrets.enrollment_key)
            # The enrollment key is fleet-wide; once this laptop has its own
            # token the key is no longer needed here and is not kept.
            Secrets(enrollment_key="", device_token=token).save(self.config.home)
            self._client.device_token = token

        self.registered = True
        self.last_error = None
        return self._client

    def _maybe_heartbeat(self, client: BackendClient) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat < self.config.heartbeat_interval_seconds:
            return
        self._last_heartbeat = now

        latest = self.db.latest_segment()
        status = self.recorder.status() if self.recorder else {}
        client.heartbeat(
            {
                "recording": bool(status.get("recording", False)),
                "device": status.get("device"),
                "last_segment_end": latest.effective_end.isoformat() if latest else None,
                "segment_count": self.db.segment_count(),
                "retention_hours": self.config.retention_hours,
            }
        )

    # -- job fulfilment ----------------------------------------------------

    def _process_pending(self, client: BackendClient) -> None:
        now = utcnow()
        for job in self.db.claimable_jobs(now_epoch=now.timestamp()):
            if self._stop.is_set():
                return
            self._process_one(client, job)

    def _process_one(self, client: BackendClient, job) -> None:
        window = job.window_seconds
        requested_at = ensure_utc(job.requested_at)

        # The second half of the window may not have happened yet. Waiting for
        # it is right; reporting "no recording" for a moment that simply has
        # not been reached would be a lie the backend cannot detect.
        ready_at = requested_at + timedelta(seconds=window / 2 + FLUSH_MARGIN_SECONDS)
        if utcnow() < ready_at:
            log.info(
                "job %s covers time not yet recorded; waiting until %s",
                job.job_id,
                ready_at.isoformat(),
            )
            self.db.update_job(job.job_id, next_attempt_epoch=ready_at.timestamp())
            return

        attempts = job.attempts + 1
        try:
            clip = extract_clip(
                self.config,
                self.db,
                requested_at,
                window_seconds=window,
                output_dir=self.config.data_dir / "clips",
            )
        except NoRecordingError as exc:
            # Permanent: the window has passed and no audio was captured in
            # it. Retrying cannot conjure a recording that never happened.
            log.warning("job %s has no audio: %s", job.job_id, exc)
            self._safe_report(client, job.job_id, "NO_RECORDING", str(exc))
            self.db.update_job(
                job.job_id, status=JOB_FAILED, attempts=attempts, last_error=str(exc)
            )
            return
        except (ExtractionError, ValueError) as exc:
            self._retry_or_fail(client, job, attempts, "EXTRACT_FAILED", str(exc))
            return

        self.db.update_job(
            job.job_id, status=JOB_UPLOADING, attempts=attempts, clip_path=clip.path
        )
        metadata = {
            "job_id": job.job_id,
            "install_id": self.config.install_id,
            "email_id": self.config.email_id,
            "employee_id": self.config.employee_id,
            **clip.to_dict(),
        }
        try:
            client.upload_clip(job.job_id, clip.path, metadata)
        except BackendError as exc:
            # The clip itself is fine; only delivery failed. Keep it on disk
            # so the retry does not have to re-cut it.
            self._retry_or_fail(client, job, attempts, "UPLOAD_FAILED", str(exc), report=False)
            return

        self.db.update_job(job.job_id, status=JOB_DONE, attempts=attempts)
        self._discard(clip.path)
        log.info("job %s complete", job.job_id)

    def _retry_or_fail(
        self,
        client: BackendClient,
        job,
        attempts: int,
        code: str,
        detail: str,
        *,
        report: bool = True,
    ) -> None:
        if attempts >= self.config.upload_max_attempts:
            log.error("job %s failed permanently after %d attempts: %s", job.job_id, attempts, detail)
            if report:
                self._safe_report(client, job.job_id, code, detail)
            self.db.update_job(
                job.job_id, status=JOB_FAILED, attempts=attempts, last_error=detail
            )
            if job.clip_path:
                self._discard(job.clip_path)
            return

        delay = min(self.config.poll_retry_seconds * (2 ** (attempts - 1)), MAX_BACKOFF_SECONDS)
        # Jitter keeps a fleet that lost the backend together from returning
        # in lockstep and knocking it over again.
        delay += random.uniform(0, delay * 0.25)
        log.warning(
            "job %s attempt %d failed (%s); retrying in %.0fs", job.job_id, attempts, detail, delay
        )
        self.db.update_job(
            job.job_id,
            attempts=attempts,
            last_error=detail,
            next_attempt_epoch=(utcnow().timestamp() + delay),
        )

    def _safe_report(self, client: BackendClient, job_id: str, code: str, detail: str) -> None:
        """Report a failure without letting the report's own failure escape."""
        try:
            client.report_error(job_id, code, detail)
        except BackendError as exc:
            log.warning("could not report %s for job %s: %s", code, job_id, exc)

    @staticmethod
    def _discard(path: Path | str) -> None:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not remove clip %s: %s", path, exc)
