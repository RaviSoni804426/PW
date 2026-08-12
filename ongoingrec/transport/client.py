"""HTTP client for the central backend.

Every call is outbound. Counsellor laptops sit behind NAT on networks the
backend cannot route into and have no stable address, so the PRD's inbound
``POST /recordings/fetch`` cannot reach them in production. Instead each
laptop registers itself, long-polls for work, and uploads the result -- which
also supplies the routing that PRD section 23 assumes: the backend knows
which employee ID belongs to which registered device because the device said
so at enrolment.
"""

from __future__ import annotations

import json
import platform
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..config import AGENT_VERSION, Config
from ..logging_setup import get_logger

log = get_logger(__name__)


class BackendError(Exception):
    """The backend could not be reached or refused the request."""


class AuthError(BackendError):
    """Credentials were rejected. Retrying with the same token will not help."""


@dataclass
class JobRequest:
    """A clip the backend has asked for."""

    job_id: str
    timestamp: str
    window_seconds: int | None = None

    @classmethod
    def from_payload(cls, payload: dict) -> "JobRequest":
        job_id = payload.get("job_id")
        timestamp = payload.get("timestamp")
        if not job_id or not timestamp:
            raise ValueError(f"job is missing job_id or timestamp: {payload!r}")
        window = payload.get("window_seconds")
        return cls(job_id=str(job_id), timestamp=str(timestamp), window_seconds=window)


class BackendClient:
    """Talks to the backend on behalf of one installation."""

    def __init__(
        self,
        config: Config,
        *,
        device_token: str = "",
        timeout: float = 30.0,
    ):
        if not config.backend_base_url:
            raise BackendError("no backend_base_url configured")
        self.config = config
        self.base_url = config.backend_base_url.rstrip("/")
        self.device_token = device_token
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, read=max(timeout, config.poll_timeout_seconds + 10)),
            headers={"User-Agent": f"OngoingRec/{AGENT_VERSION}"},
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BackendClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- helpers ----------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        if not self.device_token:
            raise AuthError("this device has no token; register first")
        return {"Authorization": f"Bearer {self.device_token}"}

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise BackendError(f"{method} {path} failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise AuthError(f"{method} {path} rejected our credentials ({response.status_code})")
        if response.status_code >= 400:
            raise BackendError(
                f"{method} {path} returned {response.status_code}: {response.text[:300]}"
            )
        return response

    # -- endpoints --------------------------------------------------------

    def register(self, enrollment_key: str) -> str:
        """Enrol this installation and obtain its device token.

        The enrollment key is shared across the fleet and shipped in the
        installer; the token that comes back is unique to this laptop. The
        caller discards the key afterwards, so a stolen laptop cannot be used
        to enrol further devices.
        """
        if not enrollment_key:
            raise AuthError("no enrollment key available to register with")

        payload = {
            "enrollment_key": enrollment_key,
            "install_id": self.config.install_id,
            "email_id": self.config.email_id,
            "employee_id": self.config.employee_id,
            "hostname": platform.node(),
            "os_version": f"{platform.system()} {platform.release()}",
            "agent_version": AGENT_VERSION,
        }
        response = self._request("POST", "/devices/register", json=payload)
        token = response.json().get("device_token")
        if not token:
            raise BackendError("registration succeeded but returned no device_token")
        self.device_token = token
        log.info("registered with backend as %s", self.config.employee_id)
        return token

    def heartbeat(self, status: dict) -> None:
        """Report liveness so the backend knows this laptop is still recording."""
        payload = {
            "install_id": self.config.install_id,
            "email_id": self.config.email_id,
            "employee_id": self.config.employee_id,
            "agent_version": AGENT_VERSION,
            **status,
        }
        self._request("POST", "/devices/heartbeat", json=payload, headers=self._auth_headers())

    def poll_jobs(self) -> list[JobRequest]:
        """Long-poll for clip requests.

        The backend holds the connection open until work arrives or the wait
        elapses, so a laptop responds within seconds without polling in a
        tight loop. An empty result is the normal case, not an error.
        """
        response = self._request(
            "GET",
            "/jobs/poll",
            params={
                "install_id": self.config.install_id,
                "wait": self.config.poll_timeout_seconds,
            },
            headers=self._auth_headers(),
        )
        if response.status_code == 204 or not response.content:
            return []
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise BackendError(f"poll returned malformed JSON: {exc}") from exc

        jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
        parsed: list[JobRequest] = []
        for entry in jobs:
            try:
                parsed.append(JobRequest.from_payload(entry))
            except ValueError as exc:
                # One malformed job must not discard the others in the batch.
                log.error("ignoring malformed job from backend: %s", exc)
        return parsed

    def upload_clip(self, job_id: str, clip_path: Path, metadata: dict) -> None:
        """Deliver the finished clip.

        The metadata travels with the audio because the clip alone cannot say
        what it is: which window it actually covers, and which stretches were
        never recorded. Without that the backend would be guessing.
        """
        clip_path = Path(clip_path)
        if not clip_path.exists():
            raise BackendError(f"clip {clip_path} no longer exists")

        with clip_path.open("rb") as handle:
            self._request(
                "POST",
                f"/jobs/{job_id}/clip",
                headers=self._auth_headers(),
                files={
                    "clip": (clip_path.name, handle, "audio/mpeg"),
                    "metadata": (None, json.dumps(metadata), "application/json"),
                },
            )
        log.info("uploaded clip for job %s (%d bytes)", job_id, clip_path.stat().st_size)

    def report_error(self, job_id: str, code: str, detail: str) -> None:
        """Tell the backend a job cannot be fulfilled.

        Reporting beats silence: a backend waiting forever for audio that was
        never recorded cannot tell that case apart from a laptop that is
        switched off.
        """
        self._request(
            "POST",
            f"/jobs/{job_id}/error",
            json={"code": code, "detail": detail[:1000]},
            headers=self._auth_headers(),
        )
        log.info("reported %s for job %s: %s", code, job_id, detail)
