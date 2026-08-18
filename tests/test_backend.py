"""Tests for the deployable central backend.

The question these answer is not "do the routes exist?" but "does a clip
requested by an operator actually reach them, and does nothing else?". So the
happy path is exercised as the full four-hop round trip an operator really
makes, and the auth tests check the two credentials cannot do each other's
job -- which is the failure that would quietly expose every counsellor's audio.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.settings import Settings, SettingsError
from backend.store import Store

ADMIN_KEY = "admin-key-for-tests-0123456789"
ENROLL_KEY = "enroll-key-for-tests-0123456789"
ADMIN = {"X-Admin-Key": ADMIN_KEY}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        admin_api_key=ADMIN_KEY,
        enrollment_key=ENROLL_KEY,
        data_dir=tmp_path / "data",
        max_clip_mb=8,
        max_poll_wait=2,
        job_retention_days=30,
    )


@pytest.fixture
def client(settings) -> TestClient:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def enrol(client: TestClient, *, install_id="install-1", employee_id="EMP001") -> str:
    response = client.post(
        "/devices/register",
        json={
            "enrollment_key": ENROLL_KEY,
            "install_id": install_id,
            "email_id": f"{employee_id.lower()}@pw.live",
            "employee_id": employee_id,
            "hostname": "laptop-1",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["device_token"]


@pytest.fixture
def open_client(tmp_path) -> TestClient:
    """A deployment with no keys configured: the shipped arrangement."""
    settings = Settings(
        admin_api_key="",
        enrollment_key="",
        data_dir=tmp_path / "open-data",
        max_clip_mb=8,
        max_poll_wait=2,
        job_retention_days=30,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


class TestConfiguration:
    def test_starts_with_no_credentials_at_all(self, monkeypatch):
        """Both keys are optional, so the service deploys with nothing to set
        up and the URL is what gates access."""
        monkeypatch.delenv("ADMIN_API_KEY", raising=False)
        monkeypatch.delenv("ENROLLMENT_KEY", raising=False)
        settings = Settings.from_env()
        assert settings.admin_open
        assert settings.registration_open

    def test_refuses_a_short_key(self, monkeypatch):
        """A guessable key is worse than none: it looks like protection."""
        monkeypatch.setenv("ADMIN_API_KEY", "short")
        monkeypatch.delenv("ENROLLMENT_KEY", raising=False)
        with pytest.raises(SettingsError, match="16 characters"):
            Settings.from_env()

    def test_a_real_key_turns_gating_on(self, monkeypatch):
        monkeypatch.setenv("ADMIN_API_KEY", ADMIN_KEY)
        monkeypatch.delenv("ENROLLMENT_KEY", raising=False)
        settings = Settings.from_env()
        assert not settings.admin_open
        assert settings.registration_open


class TestOpenDeployment:
    """No keys set. Anyone with the URL can enrol a laptop and fetch audio."""

    def test_registration_needs_no_key(self, open_client):
        response = open_client.post(
            "/devices/register",
            json={
                "install_id": "install-1",
                "email_id": "ravi@pw.live",
                "employee_id": "PW33744",
            },
        )
        assert response.status_code == 200
        assert response.json()["device_token"]

    def test_a_stale_key_from_an_older_installer_is_ignored_not_refused(self, open_client):
        """The same installer keeps working after the backend stops gating."""
        response = open_client.post(
            "/devices/register",
            json={
                "enrollment_key": "left-over-from-before",
                "install_id": "install-1",
                "email_id": "ravi@pw.live",
                "employee_id": "PW33744",
            },
        )
        assert response.status_code == 200

    def test_admin_needs_no_key(self, open_client):
        assert open_client.get("/admin/devices").status_code == 200
        assert open_client.get("/admin/jobs").status_code == 200

    def test_a_clip_can_be_fetched_with_no_credentials_at_all(self, open_client):
        open_client.post(
            "/devices/register",
            json={
                "install_id": "install-1",
                "email_id": "ravi@pw.live",
                "employee_id": "PW33744",
            },
        )
        response = open_client.get(
            "/admin/recordings/fetch",
            params={
                "employee_id": "PW33744",
                "timestamp": "2026-08-17T19:06:52",
                "wait_seconds": 0,
            },
        )
        assert response.status_code == 202  # queued; no laptop is answering here

    def test_identity_rules_still_apply(self, open_client):
        """Open does not mean unvalidated -- a request still has to name
        somebody who actually has a laptop."""
        assert open_client.get(
            "/admin/recordings/fetch",
            params={"employee_id": "NOBODY", "timestamp": "2026-08-17T19:00:00", "wait_seconds": 0},
        ).status_code == 404
        assert open_client.get(
            "/admin/recordings/fetch",
            params={"timestamp": "2026-08-17T19:00:00", "wait_seconds": 0},
        ).status_code == 400


class TestEnrolment:
    def test_a_wrong_enrollment_key_is_refused(self, client):
        response = client.post(
            "/devices/register",
            json={
                "enrollment_key": "wrong",
                "install_id": "i1",
                "email_id": "a@pw.live",
                "employee_id": "EMP001",
            },
        )
        assert response.status_code == 401

    def test_missing_identity_is_refused(self, client):
        response = client.post(
            "/devices/register",
            json={"enrollment_key": ENROLL_KEY, "install_id": "i1"},
        )
        assert response.status_code == 400

    def test_re_enrolling_replaces_the_token_without_duplicating_the_device(self, client):
        first = enrol(client)
        second = enrol(client)
        assert first != second

        devices = client.get("/admin/devices", headers=ADMIN).json()
        assert len(devices) == 1

        # The old token must stop working, or a leaked one would outlive its
        # replacement forever.
        assert client.get(
            "/jobs/poll", params={"install_id": "install-1", "wait": 0},
            headers={"Authorization": f"Bearer {first}"},
        ).status_code == 401
        assert client.get(
            "/jobs/poll", params={"install_id": "install-1", "wait": 0},
            headers={"Authorization": f"Bearer {second}"},
        ).status_code == 200

    def test_identity_changes_are_picked_up_on_re_enrolment(self, client):
        enrol(client, employee_id="EMP001")
        enrol(client, employee_id="PW33744")
        queued = client.post(
            "/admin/request-clip",
            json={"employee_id": "PW33744", "timestamp": "2026-08-17T19:00:00"},
            headers=ADMIN,
        )
        assert queued.status_code == 200
        assert queued.json()["employee_id"] == "PW33744"


class TestAuthSeparation:
    def test_admin_routes_reject_a_missing_key(self, client):
        for path in ("/admin/devices", "/admin/jobs"):
            assert client.get(path).status_code == 401

    def test_admin_routes_reject_the_enrollment_key(self, client):
        """The fleet key ships inside the installer and will leak. It must not
        open the door to anyone's recordings."""
        response = client.get("/admin/devices", headers={"X-Admin-Key": ENROLL_KEY})
        assert response.status_code == 401

    def test_admin_routes_reject_a_device_token(self, client):
        token = enrol(client)
        response = client.get("/admin/devices", headers={"X-Admin-Key": token})
        assert response.status_code == 401

    def test_admin_key_works_as_a_bearer_token_too(self, client):
        response = client.get("/admin/devices", headers={"Authorization": f"Bearer {ADMIN_KEY}"})
        assert response.status_code == 200

    def test_agent_routes_reject_the_admin_key(self, client):
        response = client.get(
            "/jobs/poll",
            params={"install_id": "install-1", "wait": 0},
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        )
        assert response.status_code == 401

    def test_a_device_cannot_poll_another_devices_queue(self, client):
        enrol(client, install_id="install-1", employee_id="EMP001")
        token_b = enrol(client, install_id="install-2", employee_id="EMP002")
        response = client.get(
            "/jobs/poll",
            params={"install_id": "install-1", "wait": 0},
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert response.status_code == 403

    def test_healthz_is_open_and_leaks_nothing(self, client):
        body = client.get("/healthz").json()
        assert body["status"] == "ok"
        assert set(body) == {"status", "devices", "jobs", "pending_jobs"}


class TestClipRoundTrip:
    def test_operator_gets_back_the_bytes_the_laptop_uploaded(self, client):
        token = enrol(client, employee_id="PW33744")
        auth = {"Authorization": f"Bearer {token}"}

        job_id = client.post(
            "/admin/request-clip",
            json={
                "employee_id": "pw33744",  # deliberately wrong case
                "timestamp": "2026-08-17T19:06:52",
                "window_seconds": 60,
            },
            headers=ADMIN,
        ).json()["job_id"]

        assert client.get(f"/admin/jobs/{job_id}", headers=ADMIN).json()["status"] == "queued"

        jobs = client.get(
            "/jobs/poll", params={"install_id": "install-1", "wait": 0}, headers=auth
        ).json()["jobs"]
        assert [j["job_id"] for j in jobs] == [job_id]
        assert jobs[0]["window_seconds"] == 60

        audio = b"ID3fake-mp3-payload" * 100
        metadata = {
            "employee_id": "PW33744",
            "clip_start": "2026-08-17T13:36:22Z",
            "clip_end": "2026-08-17T13:37:22Z",
            "gaps": [],
            "partial": False,
        }
        upload = client.post(
            f"/jobs/{job_id}/clip",
            headers=auth,
            files={
                "clip": ("clip.mp3", audio, "audio/mpeg"),
                "metadata": (None, json.dumps(metadata), "application/json"),
            },
        )
        assert upload.status_code == 200

        status = client.get(f"/admin/jobs/{job_id}", headers=ADMIN).json()
        assert status["status"] == "complete"
        assert status["clip_bytes"] == len(audio)
        assert status["metadata"]["clip_start"] == "2026-08-17T13:36:22Z"

        downloaded = client.get(f"/admin/jobs/{job_id}/clip", headers=ADMIN)
        assert downloaded.status_code == 200
        assert downloaded.headers["content-type"] == "audio/mpeg"
        assert downloaded.content == audio

    def test_a_delivered_job_is_not_handed_out_twice(self, client):
        token = enrol(client)
        auth = {"Authorization": f"Bearer {token}"}
        client.post(
            "/admin/request-clip",
            json={"employee_id": "EMP001", "timestamp": "2026-08-17T19:00:00"},
            headers=ADMIN,
        )
        first = client.get(
            "/jobs/poll", params={"install_id": "install-1", "wait": 0}, headers=auth
        ).json()["jobs"]
        second = client.get(
            "/jobs/poll", params={"install_id": "install-1", "wait": 0}, headers=auth
        ).json()["jobs"]
        assert len(first) == 1
        assert second == []

    def test_a_job_survives_a_backend_restart(self, settings):
        """The reason this backend exists rather than the in-memory mock: a
        redeploy must not lose enrolled devices or queued work."""
        with TestClient(create_app(settings)) as first:
            token = enrol(first)
            job_id = first.post(
                "/admin/request-clip",
                json={"employee_id": "EMP001", "timestamp": "2026-08-17T19:00:00"},
                headers=ADMIN,
            ).json()["job_id"]

        with TestClient(create_app(settings)) as second:
            assert second.get(f"/admin/jobs/{job_id}", headers=ADMIN).status_code == 200
            # The same token still works, so the agent never has to re-enrol.
            jobs = second.get(
                "/jobs/poll",
                params={"install_id": "install-1", "wait": 0},
                headers={"Authorization": f"Bearer {token}"},
            ).json()["jobs"]
            assert [j["job_id"] for j in jobs] == [job_id]

    def test_clip_download_before_upload_explains_the_status(self, client):
        enrol(client)
        job_id = client.post(
            "/admin/request-clip",
            json={"employee_id": "EMP001", "timestamp": "2026-08-17T19:00:00"},
            headers=ADMIN,
        ).json()["job_id"]
        response = client.get(f"/admin/jobs/{job_id}/clip", headers=ADMIN)
        assert response.status_code == 404
        assert "queued" in response.json()["detail"]

    def test_a_laptop_cannot_upload_to_another_laptops_job(self, client):
        enrol(client, install_id="install-1", employee_id="EMP001")
        token_b = enrol(client, install_id="install-2", employee_id="EMP002")
        job_id = client.post(
            "/admin/request-clip",
            json={"employee_id": "EMP001", "timestamp": "2026-08-17T19:00:00"},
            headers=ADMIN,
        ).json()["job_id"]
        response = client.post(
            f"/jobs/{job_id}/clip",
            headers={"Authorization": f"Bearer {token_b}"},
            files={
                "clip": ("clip.mp3", b"not-yours", "audio/mpeg"),
                "metadata": (None, "{}", "application/json"),
            },
        )
        assert response.status_code == 403

    def test_oversized_clip_is_refused(self, client, settings):
        token = enrol(client)
        job_id = client.post(
            "/admin/request-clip",
            json={"employee_id": "EMP001", "timestamp": "2026-08-17T19:00:00"},
            headers=ADMIN,
        ).json()["job_id"]
        oversized = b"\0" * (settings.max_clip_mb * 1024 * 1024 + 1)
        response = client.post(
            f"/jobs/{job_id}/clip",
            headers={"Authorization": f"Bearer {token}"},
            files={
                "clip": ("clip.mp3", oversized, "audio/mpeg"),
                "metadata": (None, "{}", "application/json"),
            },
        )
        assert response.status_code == 413


class TestFailureReporting:
    def test_a_reported_failure_reaches_the_operator(self, client):
        token = enrol(client)
        job_id = client.post(
            "/admin/request-clip",
            json={"employee_id": "EMP001", "timestamp": "2020-01-01T10:00:00"},
            headers=ADMIN,
        ).json()["job_id"]

        client.post(
            f"/jobs/{job_id}/error",
            headers={"Authorization": f"Bearer {token}"},
            json={"code": "NO_RECORDING", "detail": "no recording covers that time"},
        )
        status = client.get(f"/admin/jobs/{job_id}", headers=ADMIN).json()
        assert status["status"] == "failed"
        assert status["error"]["code"] == "NO_RECORDING"

    def test_unknown_identifier_is_refused_before_a_job_exists(self, client):
        enrol(client)
        response = client.post(
            "/admin/request-clip",
            json={"employee_id": "NOBODY", "timestamp": "2026-08-17T19:00:00"},
            headers=ADMIN,
        )
        assert response.status_code == 404
        assert client.get("/admin/jobs", headers=ADMIN).json() == []


class TestOneStepFetch:
    """Identifier plus timestamp in, audio out, in a single request.

    These need real concurrency: the fetch is still waiting while the laptop
    collects the job and uploads, which is exactly the interleaving that
    happens in production and the only way the endpoint can be shown to work.
    """

    @staticmethod
    async def _enrol(ac, *, install_id="install-1", employee_id="PW33744") -> str:
        response = await ac.post(
            "/devices/register",
            json={
                "enrollment_key": ENROLL_KEY,
                "install_id": install_id,
                "email_id": "ravi@pw.live",
                "employee_id": employee_id,
            },
        )
        return response.json()["device_token"]

    @staticmethod
    async def _act_as_laptop(ac, token: str, *, audio: bytes | None = None, error=None):
        """Collect whatever is queued and answer it, as the agent would."""
        auth = {"Authorization": f"Bearer {token}"}
        for _ in range(100):
            jobs = (
                await ac.get(
                    "/jobs/poll", params={"install_id": "install-1", "wait": 0}, headers=auth
                )
            ).json()["jobs"]
            if jobs:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("no job was ever queued for the laptop")

        job_id = jobs[0]["job_id"]
        if error is not None:
            await ac.post(f"/jobs/{job_id}/error", headers=auth, json=error)
            return job_id
        await ac.post(
            f"/jobs/{job_id}/clip",
            headers=auth,
            files={
                "clip": ("clip.mp3", audio, "audio/mpeg"),
                "metadata": (
                    None,
                    json.dumps(
                        {
                            "requested_at": "2026-08-17T13:36:52Z",
                            "clip_start": "2026-08-17T13:36:22Z",
                            "clip_end": "2026-08-17T13:37:22Z",
                            "gaps": [{"start": "x", "end": "y", "seconds": 4.0}],
                            "partial": True,
                        }
                    ),
                    "application/json",
                ),
            },
        )
        return job_id

    @pytest.mark.asyncio
    async def test_employee_id_and_timestamp_return_the_audio(self, settings):
        audio = b"ID3one-step-payload" * 200
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://test"
        ) as ac:
            token = await self._enrol(ac)
            fetch = asyncio.create_task(
                ac.get(
                    "/admin/recordings/fetch",
                    params={
                        "employee_id": "pw33744",  # deliberately wrong case
                        "timestamp": "2026-08-17T19:06:52",
                        "window_seconds": 60,
                    },
                    headers=ADMIN,
                    timeout=30,
                )
            )
            await self._act_as_laptop(ac, token, audio=audio)
            response = await fetch

        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mpeg"
        assert response.content == audio

    @pytest.mark.asyncio
    async def test_email_id_works_as_the_identifier_too(self, settings):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://test"
        ) as ac:
            token = await self._enrol(ac)
            fetch = asyncio.create_task(
                ac.get(
                    "/admin/recordings/fetch",
                    params={"email_id": "RAVI@pw.live", "timestamp": "2026-08-17T19:06:52"},
                    headers=ADMIN,
                    timeout=30,
                )
            )
            await self._act_as_laptop(ac, token, audio=b"audio-by-email")
            response = await fetch

        assert response.status_code == 200
        assert response.content == b"audio-by-email"

    @pytest.mark.asyncio
    async def test_the_response_says_what_the_clip_really_covers(self, settings):
        """A caller streaming the body straight to a file still has to learn
        that four seconds of it were never recorded."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://test"
        ) as ac:
            token = await self._enrol(ac)
            fetch = asyncio.create_task(
                ac.get(
                    "/admin/recordings/fetch",
                    params={"employee_id": "PW33744", "timestamp": "2026-08-17T19:06:52"},
                    headers=ADMIN,
                    timeout=30,
                )
            )
            await self._act_as_laptop(ac, token, audio=b"padded")
            response = await fetch

        assert response.headers["x-ongoingrec-clip-start"] == "2026-08-17T13:36:22Z"
        assert response.headers["x-ongoingrec-clip-end"] == "2026-08-17T13:37:22Z"
        assert response.headers["x-ongoingrec-partial"] == "true"
        assert json.loads(response.headers["x-ongoingrec-gaps"])[0]["seconds"] == 4.0
        assert "PW33744" in response.headers["content-disposition"]

    @pytest.mark.asyncio
    async def test_post_form_works_the_same_as_the_url_form(self, settings):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://test"
        ) as ac:
            token = await self._enrol(ac)
            fetch = asyncio.create_task(
                ac.post(
                    "/admin/recordings/fetch",
                    json={"employee_id": "PW33744", "timestamp": "2026-08-17T19:06:52"},
                    headers=ADMIN,
                    timeout=30,
                )
            )
            await self._act_as_laptop(ac, token, audio=b"via-post")
            response = await fetch

        assert response.status_code == 200
        assert response.content == b"via-post"

    def test_an_offline_laptop_gives_back_a_job_to_collect_later(self, client):
        """Nothing is thrown away when the wait runs out -- the laptop may be
        shut for the night and the request is still valid tomorrow."""
        enrol(client, employee_id="PW33744")
        response = client.get(
            "/admin/recordings/fetch",
            params={
                "employee_id": "PW33744",
                "timestamp": "2026-08-17T19:06:52",
                "wait_seconds": 0,
            },
            headers=ADMIN,
        )
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert "switched off" in body["detail"]

        # The job really is still there, and still deliverable.
        assert client.get(f"/admin/jobs/{body['job_id']}", headers=ADMIN).status_code == 200

    @pytest.mark.asyncio
    async def test_no_recording_for_that_moment_is_a_404(self, settings):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://test"
        ) as ac:
            token = await self._enrol(ac)
            fetch = asyncio.create_task(
                ac.get(
                    "/admin/recordings/fetch",
                    params={"employee_id": "PW33744", "timestamp": "2020-01-01T10:00:00"},
                    headers=ADMIN,
                    timeout=30,
                )
            )
            await self._act_as_laptop(
                ac, token, error={"code": "NO_RECORDING", "detail": "nothing covers that time"}
            )
            response = await fetch

        assert response.status_code == 404
        assert "nothing covers that time" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_laptop_side_failure_is_not_reported_as_missing_audio(self, settings):
        """`EXTRACT_FAILED` means the audio exists but could not be rendered.
        Returning 404 would tell the caller the recording is gone, and they
        would stop asking for something that is still there."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://test"
        ) as ac:
            token = await self._enrol(ac)
            fetch = asyncio.create_task(
                ac.get(
                    "/admin/recordings/fetch",
                    params={"employee_id": "PW33744", "timestamp": "2026-08-17T19:06:52"},
                    headers=ADMIN,
                    timeout=30,
                )
            )
            await self._act_as_laptop(
                ac, token, error={"code": "EXTRACT_FAILED", "detail": "ffmpeg exited 1"}
            )
            response = await fetch

        assert response.status_code == 502

    def test_unknown_identifier_is_refused(self, client):
        enrol(client)
        response = client.get(
            "/admin/recordings/fetch",
            params={"employee_id": "NOBODY", "timestamp": "2026-08-17T19:00:00", "wait_seconds": 0},
            headers=ADMIN,
        )
        assert response.status_code == 404
        assert client.get("/admin/jobs", headers=ADMIN).json() == [], (
            "a job must not be queued for a person with no laptop"
        )

    def test_a_timestamp_alone_is_refused(self, client):
        enrol(client)
        response = client.get(
            "/admin/recordings/fetch",
            params={"timestamp": "2026-08-17T19:00:00", "wait_seconds": 0},
            headers=ADMIN,
        )
        assert response.status_code == 400

    def test_it_needs_the_admin_key_like_everything_else(self, client):
        enrol(client)
        assert client.get(
            "/admin/recordings/fetch",
            params={"employee_id": "EMP001", "timestamp": "2026-08-17T19:00:00"},
        ).status_code == 401


def _backdate(store: Store, job_id: str, days: int) -> None:
    """Age a job as if it were created *days* ago.

    Retention only ever acts on rows older than the cutoff, so a job created
    during the test is never a candidate. Moving its timestamp is what puts it
    in the position a real job reaches after sitting on the server for weeks.
    """
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store._conn.execute("UPDATE jobs SET created_at=? WHERE job_id=?", (stamp, job_id))


class TestRetention:
    def test_old_finished_jobs_and_their_audio_are_purged(self, settings):
        store = Store(settings.db_path)
        store.register(install_id="i1", email_id="a@pw.live", employee_id="EMP001")
        job = store.create_job("i1", "2026-01-01T10:00:00", 60)

        settings.clips_dir.mkdir(parents=True, exist_ok=True)
        path = settings.clips_dir / f"{job.job_id}.mp3"
        path.write_bytes(b"audio")
        store.complete_job(job.job_id, clip_path=path, clip_bytes=5, metadata={})
        _backdate(store, job.job_id, days=45)

        assert store.purge_old_jobs(days=30) == 1
        assert not path.exists(), "the audio must go with the row, not linger on disk"
        assert store.get_job(job.job_id) is None
        store.close()

    def test_recent_jobs_are_left_alone(self, settings):
        store = Store(settings.db_path)
        store.register(install_id="i1", email_id="a@pw.live", employee_id="EMP001")
        job = store.create_job("i1", "2026-01-01T10:00:00", 60)
        store.complete_job(
            job.job_id, clip_path=settings.clips_dir / "x.mp3", clip_bytes=5, metadata={}
        )
        _backdate(store, job.job_id, days=5)

        assert store.purge_old_jobs(days=30) == 0
        assert store.get_job(job.job_id) is not None
        store.close()

    def test_pending_jobs_are_never_purged(self, settings):
        """An unfulfilled job outliving the retention window means the laptop
        has been off for weeks -- exactly when discarding the request silently
        would be worst."""
        store = Store(settings.db_path)
        store.register(install_id="i1", email_id="a@pw.live", employee_id="EMP001")
        job = store.create_job("i1", "2026-01-01T10:00:00", 60)
        _backdate(store, job.job_id, days=365)

        assert store.purge_old_jobs(days=30) == 0
        assert store.get_job(job.job_id) is not None
        store.close()
