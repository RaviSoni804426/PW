"""The production retrieval path, end to end against the mock backend.

Every test here drives the real poller through a real HTTP client into a real
backend implementation. Only the microphone and the network hop are
substituted, so what passes here is the loop that will run on a counsellor's
laptop.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from mockbackend.app import DEFAULT_ENROLLMENT_KEY, create_mock_backend
from ongoingrec.audio.watermark import CYCLE_SECONDS, decode_timeline
from ongoingrec.config import Secrets
from ongoingrec.index import JOB_DONE, JOB_FAILED
from ongoingrec.timeutil import format_utc, parse_timestamp, utcnow
from ongoingrec.transport.client import AuthError, BackendClient, BackendError
from ongoingrec.transport.poller import JobPoller

from .helpers import decode_bytes, record_synthetic


@pytest.fixture
def backend(tmp_path):
    app, state = create_mock_backend(uploads_dir=tmp_path / "uploads")
    with TestClient(app) as client:
        yield client, state


def make_client_factory(config, http_client):
    """Route the real BackendClient at the mock app, in process.

    Everything above the socket -- auth headers, multipart upload, error
    mapping -- is the production code path.
    """

    def factory(device_token: str) -> BackendClient:
        client = BackendClient(config, device_token=device_token)
        client._client = httpx.Client(
            transport=http_client._transport, base_url="http://testserver"
        )
        return client

    return factory


@pytest.fixture
def poller(config, recorded, backend):
    """A poller wired to the mock backend through an in-process transport."""
    http_client, _ = backend
    # No long-poll wait: the mock would otherwise hold each tick open for the
    # full production timeout while the queue is empty.
    config.poll_timeout_seconds = 0
    Secrets(enrollment_key=DEFAULT_ENROLLMENT_KEY).save(config.home)
    return JobPoller(config, recorded, client_factory=make_client_factory(config, http_client))


def request_clip(backend, **body) -> str:
    http_client, _ = backend
    response = http_client.post("/admin/request-clip", json=body)
    assert response.status_code == 200, response.text
    return response.json()["job_id"]


def job_status(backend, job_id: str) -> dict:
    http_client, _ = backend
    return http_client.get(f"/admin/jobs/{job_id}").json()


class TestRegistration:
    def test_first_tick_enrols_the_device(self, poller, config, backend):
        _, state = backend
        poller.tick()

        assert poller.registered
        devices = list(state.by_install.values())
        assert len(devices) == 1
        assert devices[0].employee_id == config.employee_id
        assert devices[0].email_id == config.email_id

    def test_token_is_stored_and_the_enrollment_key_is_discarded(self, poller, config):
        """A stolen laptop must not carry the key that enrols new devices."""
        poller.tick()
        secrets = Secrets.load(config.home)
        assert secrets.device_token.startswith("tok-")
        assert secrets.enrollment_key == ""

    def test_registration_happens_once(self, poller, backend):
        _, state = backend
        poller.tick()
        poller.tick()
        poller.tick()
        assert len(state.by_install) == 1

    def test_a_wrong_enrollment_key_is_an_auth_error(self, config, recorded, backend):
        http_client, _ = backend
        config.poll_timeout_seconds = 0
        Secrets(enrollment_key="wrong-key").save(config.home)

        poller = JobPoller(
            config, recorded, client_factory=make_client_factory(config, http_client)
        )
        with pytest.raises(AuthError):
            poller.tick()


class TestRecoveryFromBackendDataLoss:
    """What happens when the backend forgets this laptop.

    A backend restored onto an empty volume, or an in-memory one that
    restarted, no longer recognises any device token. Retrying the rejected
    token can never succeed, so the laptop has to notice and enrol again --
    otherwise a single backend redeploy silently ends clip delivery for the
    whole fleet while every laptop goes on recording perfectly.
    """

    def _forget_every_device(self, backend) -> None:
        _, state = backend
        state.devices.clear()
        state.by_install.clear()
        state.by_employee.clear()
        state.by_email.clear()

    def test_a_forgotten_device_is_rejected(self, poller, backend):
        poller.tick()
        self._forget_every_device(backend)
        with pytest.raises(AuthError):
            poller.tick()

    def test_a_rejected_token_is_always_thrown_away(self, poller, config, backend):
        """Even with no enrollment key to re-enrol with. The token has already
        been refused, so keeping it protects nothing -- and against a backend
        that leaves registration open, dropping it is what gets the laptop
        back in."""
        poller.tick()
        self._forget_every_device(backend)

        assert poller._discard_rejected_token() is True
        assert Secrets.load(config.home).device_token == ""

    def test_a_gated_backend_still_refuses_a_laptop_with_no_key(self, poller, config, backend):
        """Dropping the token does not conjure permission: this mock requires
        an enrollment key, and the laptop discarded its own after enrolling."""
        poller.tick()
        self._forget_every_device(backend)
        poller._discard_rejected_token()

        assert Secrets.load(config.home).enrollment_key == ""
        with pytest.raises(AuthError):
            poller.tick()

    def test_with_a_retained_key_the_laptop_heals_itself(self, config, recorded, backend):
        http_client, state = backend
        config.poll_timeout_seconds = 0
        config.retain_enrollment_key = True
        Secrets(enrollment_key=DEFAULT_ENROLLMENT_KEY).save(config.home)
        poller = JobPoller(
            config, recorded, client_factory=make_client_factory(config, http_client)
        )

        poller.tick()
        first_token = Secrets.load(config.home).device_token
        assert Secrets.load(config.home).enrollment_key == DEFAULT_ENROLLMENT_KEY

        self._forget_every_device(backend)
        with pytest.raises(AuthError):
            poller.tick()

        assert poller._discard_rejected_token() is True
        assert Secrets.load(config.home).device_token == ""

        # The next cycle enrols again, and the laptop is back in service.
        poller.tick()
        assert poller.registered
        assert len(state.by_install) == 1
        assert Secrets.load(config.home).device_token not in ("", first_token)

    def test_clip_delivery_resumes_after_healing(self, config, recorded, backend):
        """The point of all of the above: audio flows again afterwards."""
        http_client, _ = backend
        config.poll_timeout_seconds = 0
        config.retain_enrollment_key = True
        Secrets(enrollment_key=DEFAULT_ENROLLMENT_KEY).save(config.home)
        poller = JobPoller(
            config, recorded, client_factory=make_client_factory(config, http_client)
        )

        poller.tick()
        self._forget_every_device(backend)
        with pytest.raises(AuthError):
            poller.tick()
        poller._discard_rejected_token()
        poller.tick()  # re-enrols

        job_id = request_clip(
            backend, employee_id=config.employee_id, timestamp="2026-08-12T11:22:15Z"
        )
        poller.tick()
        poller.tick()
        assert job_status(backend, job_id)["status"] == "complete"


class TestClipDelivery:
    def test_backend_receives_the_audio_it_asked_for(self, poller, backend, config):
        """The whole product, in one test: identifier plus timestamp in,
        correct audio out."""
        poller.tick()  # register
        job_id = request_clip(
            backend, employee_id=config.employee_id, timestamp="2026-08-12T11:22:15Z"
        )
        poller.tick()  # collect and fulfil

        status = job_status(backend, job_id)
        assert status["status"] == "complete"
        assert status["clip_bytes"] > 1000

        clip = Path(status["clip_path"]).read_bytes()
        decoded = decode_timeline(decode_bytes(clip, config.sample_rate), config.sample_rate)
        clip_start = parse_timestamp(status["metadata"]["clip_start"])
        assert clip_start == parse_timestamp("2026-08-12T11:17:15Z")
        assert decoded == [
            int(clip_start.timestamp() + i) % CYCLE_SECONDS for i in range(len(decoded))
        ]

    def test_email_id_resolves_to_the_same_installation(self, poller, backend, config):
        """PRD section 23, via the routing the backend gains at registration."""
        poller.tick()
        job_id = request_clip(backend, email_id=config.email_id, timestamp="2026-08-12T11:22:15Z")
        poller.tick()
        assert job_status(backend, job_id)["status"] == "complete"

    def test_uploaded_metadata_describes_the_clip(self, poller, backend, config):
        poller.tick()
        job_id = request_clip(
            backend, employee_id=config.employee_id, timestamp="2026-08-12T11:29:00Z"
        )
        poller.tick()

        metadata = job_status(backend, job_id)["metadata"]
        assert metadata["employee_id"] == config.employee_id
        assert metadata["email_id"] == config.email_id
        assert len(metadata["segment_ids"]) == 2  # crossed a segment boundary
        assert metadata["partial"] is False
        assert metadata["gaps"] == []

    def test_unknown_identifier_is_refused_by_the_backend(self, poller, backend):
        poller.tick()
        http_client, _ = backend
        response = http_client.post(
            "/admin/request-clip",
            json={"employee_id": "EMP999", "timestamp": "2026-08-12T11:22:15Z"},
        )
        assert response.status_code == 404


class TestIdempotency:
    def test_a_job_delivered_twice_is_uploaded_once(self, poller, backend, config, recorded):
        """At-least-once delivery must not become at-least-once upload."""
        poller.tick()
        job_id = request_clip(
            backend, employee_id=config.employee_id, timestamp="2026-08-12T11:22:15Z"
        )
        poller.tick()
        assert recorded.get_job(job_id).status == JOB_DONE

        # The backend re-delivers the same job_id.
        _, state = backend
        state.queues[state.by_employee[config.employee_id.casefold()].install_id].append(
            state.jobs[job_id]
        )
        poller.tick()

        job = recorded.get_job(job_id)
        assert job.status == JOB_DONE
        assert job.attempts == 1  # not re-extracted, not re-uploaded

    def test_a_job_survives_a_restart(self, poller, backend, config, recorded):
        """Accepted before a reboot, delivered after it."""
        poller.tick()
        job_id = request_clip(
            backend, employee_id=config.employee_id, timestamp="2026-08-12T11:22:15Z"
        )
        # Take the job but stop before fulfilling it, as a power cut would.
        client = poller._ensure_client()
        for job in client.poll_jobs():
            recorded.upsert_job(job.job_id, parse_timestamp(job.timestamp), 600)
        assert recorded.get_job(job_id) is not None

        poller.tick()  # a fresh cycle, as after a restart
        assert job_status(backend, job_id)["status"] == "complete"


class TestFailureReporting:
    def test_a_window_with_no_audio_is_reported_not_ignored(self, poller, backend, config, recorded):
        """A backend waiting silently cannot tell "never recorded" from
        "laptop is off"."""
        poller.tick()
        job_id = request_clip(
            backend, employee_id=config.employee_id, timestamp="2026-08-12T03:00:00Z"
        )
        poller.tick()

        status = job_status(backend, job_id)
        assert status["status"] == "failed"
        assert status["error"]["code"] == "NO_RECORDING"
        assert recorded.get_job(job_id).status == JOB_FAILED

    def test_no_recording_is_not_retried(self, poller, backend, config, recorded):
        poller.tick()
        job_id = request_clip(
            backend, employee_id=config.employee_id, timestamp="2026-08-12T03:00:00Z"
        )
        poller.tick()
        attempts = recorded.get_job(job_id).attempts
        poller.tick()
        assert recorded.get_job(job_id).attempts == attempts

    def test_a_window_that_has_not_happened_yet_is_waited_for(
        self, poller, backend, config, recorded
    ):
        """The failure this prevents: asking about a moment 30 seconds ago and
        being told, permanently, that it was never recorded."""
        poller.tick()
        just_now = format_utc(utcnow())
        job_id = request_clip(backend, employee_id=config.employee_id, timestamp=just_now)
        poller.tick()

        job = recorded.get_job(job_id)
        assert job.status != JOB_FAILED
        assert job.attempts == 0
        assert job.next_attempt_epoch > utcnow().timestamp()
        assert job_status(backend, job_id)["status"] == "delivered"

    def test_a_past_window_is_not_deferred(self, poller, backend, config, recorded):
        poller.tick()
        past = format_utc(utcnow() - timedelta(hours=2))
        job_id = request_clip(backend, employee_id=config.employee_id, timestamp=past)
        poller.tick()
        assert recorded.get_job(job_id).attempts == 1

    def test_upload_failure_is_retried_with_backoff(self, poller, backend, config, recorded, monkeypatch):
        poller.tick()
        job_id = request_clip(
            backend, employee_id=config.employee_id, timestamp="2026-08-12T11:22:15Z"
        )

        client = poller._ensure_client()
        monkeypatch.setattr(
            client, "upload_clip", lambda *a, **k: (_ for _ in ()).throw(BackendError("network down"))
        )
        poller.tick()

        job = recorded.get_job(job_id)
        assert job.status not in (JOB_DONE, JOB_FAILED)
        assert job.attempts == 1
        assert job.next_attempt_epoch > utcnow().timestamp()

    def test_repeated_upload_failure_eventually_gives_up(self, poller, backend, config, recorded, monkeypatch):
        config.upload_max_attempts = 2
        poller.tick()
        job_id = request_clip(
            backend, employee_id=config.employee_id, timestamp="2026-08-12T11:22:15Z"
        )
        client = poller._ensure_client()
        monkeypatch.setattr(
            client, "upload_clip", lambda *a, **k: (_ for _ in ()).throw(BackendError("network down"))
        )
        for _ in range(config.upload_max_attempts):
            recorded.update_job(job_id, next_attempt_epoch=0.0)
            poller.tick()

        assert recorded.get_job(job_id).status == JOB_FAILED


class TestHeartbeat:
    def test_heartbeat_reports_recording_state(self, poller, backend, config):
        poller.tick()
        _, state = backend
        device = state.by_install[config.install_id]
        assert device.last_heartbeat is not None
        assert device.last_heartbeat["employee_id"] == config.employee_id
        assert device.last_heartbeat["segment_count"] == 3

    def test_heartbeat_is_rate_limited(self, poller, backend, config):
        poller.tick()
        _, state = backend
        first = state.by_install[config.install_id].last_heartbeat["received_at"]
        poller.tick()
        assert state.by_install[config.install_id].last_heartbeat["received_at"] == first
