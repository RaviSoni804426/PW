"""Tests for the start/end session API.

The question these answer is not "do the routes respond?" but "does the pair
of moments an operator sent come back as exactly that stretch of audio?".

Two conversions sit between the caller and the audio, and both are silent when
wrong. An IST date and clock time become one UTC instant; and because the agent
centres a clip on a timestamp, a start/end pair then becomes a midpoint plus a
length. Get either backwards and every session returns audio from the wrong
moment while every status code still looks right.

The other half is the state machine. A session is the only thing in this system
that says no to a request, so each refusal is checked for the message that
tells the operator what to do next.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import MAX_SESSION_SECONDS, create_app
from backend.settings import Settings

ADMIN_KEY = "admin-key-for-tests-0123456789"
ENROLL_KEY = "enroll-key-for-tests-0123456789"
ADMIN = {"X-Admin-Key": ADMIN_KEY}
IST = timezone(timedelta(hours=5, minutes=30), "IST")


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


def enrol(client: TestClient, *, install_id="install-1", employee_id="PW33744") -> str:
    response = client.post(
        "/devices/register",
        json={
            "enrollment_key": ENROLL_KEY,
            "install_id": install_id,
            "email_id": "ravi@pw.live",
            "employee_id": employee_id,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["device_token"]


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ist(dt: datetime) -> tuple[str, str]:
    """The IST date and clock time a caller would send for this instant."""
    local = dt.astimezone(IST)
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M:%S")


def ago(**kwargs) -> tuple[str, str]:
    return ist(datetime.now(timezone.utc) - timedelta(**kwargs))


def ahead(**kwargs) -> tuple[str, str]:
    return ist(datetime.now(timezone.utc) + timedelta(**kwargs))


def start_body(when: tuple[str, str], employee_id: str | None = "PW33744", **extra) -> dict:
    body = {"start_date": when[0], "start_time": when[1], **extra}
    if employee_id is not None:
        body["employee_id"] = employee_id
    return body


def end_body(when: tuple[str, str], employee_id: str | None = "PW33744", **extra) -> dict:
    body = {"end_date": when[0], "end_time": when[1], **extra}
    if employee_id is not None:
        body["employee_id"] = employee_id
    return body


def start(client, when, **kw):
    return client.post("/admin/sessions/start", json=start_body(when, **kw), headers=ADMIN)


def end(client, when, *, wait=0, **kw):
    return client.post(
        "/admin/sessions/end",
        json=end_body(when, **kw),
        params={"wait_seconds": wait},
        headers=ADMIN,
    )


class TestIstIsTheOnlyInputFormat:
    """Everything else in this system takes an ISO instant. Sessions take an
    IST date and clock time, because that is the shape an operator reads them
    in -- and because a fixed +05:30 leaves nothing to infer."""

    def test_an_ist_pair_becomes_the_right_utc_instant(self, client):
        enrol(client)
        body = start(client, ("2026-08-17", "14:30:00")).json()
        assert body["start_utc"] == "2026-08-17T09:00:00Z"
        assert body["start_date"] == "2026-08-17"
        assert body["start_time"] == "14:30:00"

    def test_an_ist_time_before_the_offset_lands_on_the_previous_utc_day(self, client):
        """03:00 IST is 21:30 UTC *yesterday* -- the case a naive +0 conversion
        gets wrong by a whole day."""
        enrol(client)
        body = start(client, ("2026-08-17", "03:00:00")).json()
        assert body["start_utc"] == "2026-08-16T21:30:00Z"

    def test_seconds_may_be_omitted(self, client):
        enrol(client)
        body = start(client, ("2026-08-17", "14:30")).json()
        assert body["start_utc"] == "2026-08-17T09:00:00Z"

    @pytest.mark.parametrize("bad", ["17-08-2026", "2026/08/17", "17 Aug 2026", "", "today"])
    def test_a_date_that_is_not_yyyy_mm_dd_is_refused(self, client, bad):
        """Lenient parsing would accept some of these as a different real date
        and return audio from a day nobody asked for."""
        enrol(client)
        response = start(client, (bad, "14:30:00"))
        assert response.status_code == 400
        assert "start_date must be YYYY-MM-DD" in response.json()["detail"]

    @pytest.mark.parametrize("bad", ["2:30 PM", "14.30", "25:00:00", "", "half past two"])
    def test_a_time_that_is_not_24_hour_is_refused(self, client, bad):
        enrol(client)
        response = start(client, ("2026-08-17", bad))
        assert response.status_code == 400
        assert "start_time must be HH:MM:SS or HH:MM" in response.json()["detail"]

    @pytest.mark.parametrize(
        "body",
        [
            {"employee_id": "PW33744"},
            {"employee_id": "PW33744", "start_date": "2026-08-17"},
            {"employee_id": "PW33744", "start_time": "14:30:00"},
        ],
    )
    def test_both_halves_are_required(self, client, body):
        enrol(client)
        response = client.post("/admin/sessions/start", json=body, headers=ADMIN)
        assert response.status_code == 422

    def test_end_requires_both_halves_too(self, client):
        enrol(client)
        start(client, ago(minutes=5))
        response = client.post(
            "/admin/sessions/end", json={"employee_id": "PW33744"}, headers=ADMIN
        )
        assert response.status_code == 422


class TestStart:
    def test_a_past_start_is_recording(self, client):
        enrol(client)
        response = start(client, ago(minutes=5))
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "recording"
        assert body["end_utc"] is None
        assert body["session_id"].startswith("ses-")

    def test_a_future_start_is_scheduled_not_recording(self, client):
        enrol(client)
        assert start(client, ahead(minutes=30)).json()["status"] == "scheduled"

    def test_the_identifier_is_case_insensitive(self, client):
        enrol(client)
        assert start(client, ago(minutes=1), employee_id="  pw33744 ").status_code == 201

    def test_email_works_as_the_identifier(self, client):
        enrol(client)
        response = start(client, ago(minutes=1), employee_id=None, email_id="RAVI@pw.live")
        assert response.status_code == 201

    def test_an_unknown_person_is_404_not_a_silent_session(self, client):
        enrol(client)
        assert start(client, ago(minutes=1), employee_id="NOBODY").status_code == 404

    def test_no_identifier_at_all_is_rejected(self, client):
        enrol(client)
        assert start(client, ago(minutes=1), employee_id=None).status_code == 400

    def test_a_start_far_in_the_future_is_treated_as_a_typo(self, client):
        enrol(client)
        response = start(client, ahead(days=400))
        assert response.status_code == 400
        assert "typo" in response.json()["detail"]


class TestStartingTwice:
    """The response to a second start is the only place an operator learns
    what is already running, so it carries the whole state, not just a 409."""

    def test_second_start_reports_the_one_in_progress(self, client):
        enrol(client)
        first = start(client, ago(minutes=5)).json()

        response = start(client, ago(minutes=1))
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["status"] == "recording"
        assert detail["session_id"] == first["session_id"]
        assert detail["start_date"] == first["start_date"]
        assert detail["start_time"] == first["start_time"]
        assert "in progress" in detail["detail"]
        assert "IST" in detail["detail"]
        assert "cancel" in detail["hint"]

    def test_second_start_says_scheduled_when_the_first_has_not_begun(self, client):
        enrol(client)
        start(client, ahead(hours=1))
        detail = start(client, ago(minutes=1)).json()["detail"]
        assert detail["status"] == "scheduled"
        assert "scheduled to start" in detail["detail"]

    def test_a_different_person_is_not_blocked(self, client):
        enrol(client)
        enrol(client, install_id="install-2", employee_id="PW99999")
        assert start(client, ago(minutes=1)).status_code == 201
        assert start(client, ago(minutes=1), employee_id="PW99999").status_code == 201

    def test_concurrent_starts_produce_exactly_one_session(self, settings):
        """Two operators clicking together must not both open a session --
        the second would silently orphan the first."""
        when = ago(minutes=5)

        async def run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(settings)),
                base_url="http://test",
            ) as ac:
                await ac.post(
                    "/devices/register",
                    json={
                        "enrollment_key": ENROLL_KEY,
                        "install_id": "install-1",
                        "email_id": "ravi@pw.live",
                        "employee_id": "PW33744",
                    },
                )
                results = await asyncio.gather(
                    *[
                        ac.post(
                            "/admin/sessions/start", json=start_body(when), headers=ADMIN
                        )
                        for _ in range(8)
                    ]
                )
                return [r.status_code for r in results]

        codes = asyncio.run(run())
        assert codes.count(201) == 1, codes
        assert codes.count(409) == 7, codes


class TestEnd:
    def test_ending_without_starting_says_so(self, client):
        enrol(client)
        response = end(client, ago(minutes=1))
        assert response.status_code == 409
        assert "sessions/start" in response.json()["detail"]

    def test_the_pair_becomes_a_job_covering_exactly_that_window(self, client):
        """The whole point: IST in, and midpoint plus length has to reconstruct
        the same two instants."""
        enrol(client)
        start(client, ("2026-08-17", "14:30:00"))
        response = end(client, ("2026-08-17", "14:40:00"))
        assert response.status_code == 202, response.text
        job = client.get(f"/admin/jobs/{response.json()['job_id']}", headers=ADMIN).json()

        assert job["window_seconds"] == 600
        assert job["timestamp"] == "2026-08-17T09:05:00Z"

        # the agent's own arithmetic, run forwards again
        centre = datetime.fromisoformat(job["timestamp"].replace("Z", "+00:00"))
        half = timedelta(seconds=job["window_seconds"] / 2)
        assert iso(centre - half) == "2026-08-17T09:00:00Z"  # 14:30 IST
        assert iso(centre + half) == "2026-08-17T09:10:00Z"  # 14:40 IST

    def test_an_odd_length_window_still_lands_on_the_pair(self, client):
        enrol(client)
        start(client, ("2026-08-17", "14:30:00"))
        response = end(client, ("2026-08-17", "14:30:07"))
        job = client.get(f"/admin/jobs/{response.json()['job_id']}", headers=ADMIN).json()
        assert job["window_seconds"] == 7
        centre = datetime.fromisoformat(job["timestamp"].replace("Z", "+00:00"))
        half = timedelta(seconds=job["window_seconds"] / 2)
        assert iso(centre - half) == "2026-08-17T09:00:00Z"
        assert iso(centre + half) == "2026-08-17T09:00:07Z"

    def test_a_session_spanning_midnight_ist_works(self, client):
        """23:50 to 00:10 crosses both the IST date and the UTC one."""
        enrol(client)
        start(client, ("2026-08-16", "23:50:00"))
        response = end(client, ("2026-08-17", "00:10:00"))
        job = client.get(f"/admin/jobs/{response.json()['job_id']}", headers=ADMIN).json()
        assert job["window_seconds"] == 1200
        centre = datetime.fromisoformat(job["timestamp"].replace("Z", "+00:00"))
        half = timedelta(seconds=job["window_seconds"] / 2)
        assert iso(centre - half) == "2026-08-16T18:20:00Z"
        assert iso(centre + half) == "2026-08-16T18:40:00Z"

    def test_the_202_carries_the_session_and_the_window_back(self, client):
        enrol(client)
        started = start(client, ago(minutes=10)).json()
        body = end(client, ago(seconds=1)).json()
        assert body["session_id"] == started["session_id"]
        assert body["start_date"] and body["start_time"]
        assert body["end_date"] and body["end_time"]
        assert body["start_utc"] and body["end_utc"]
        assert "switched off" in body["detail"]

    def test_a_future_end_is_refused(self, client):
        enrol(client)
        start(client, ago(minutes=5))
        response = end(client, ahead(minutes=5))
        assert response.status_code == 400
        assert "not been recorded yet" in response.json()["detail"]

    def test_ending_a_session_that_has_not_started_points_at_cancel(self, client):
        enrol(client)
        start(client, ahead(hours=2))
        response = end(client, ago(seconds=1))
        assert response.status_code == 400
        assert "cancel" in response.json()["detail"]

    def test_an_end_before_the_start_is_refused(self, client):
        enrol(client)
        start(client, ago(minutes=5))
        response = end(client, ago(minutes=30))
        assert response.status_code == 400

    def test_a_zero_length_window_is_refused(self, client):
        enrol(client)
        when = ago(minutes=5)
        start(client, when)
        response = end(client, when)
        assert response.status_code == 400
        assert "at least" in response.json()["detail"]

    def test_a_session_longer_than_one_clip_can_carry_is_refused_up_front(self, client):
        enrol(client)
        start(client, ago(hours=20))
        response = end(client, ago(seconds=1))
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert f"{MAX_SESSION_SECONDS // 3600}h limit" in detail
        assert "recordings/fetch" in detail

    def test_ending_twice_fails_the_second_time(self, client):
        enrol(client)
        start(client, ago(minutes=5))
        assert end(client, ago(seconds=1)).status_code == 202
        assert end(client, ago(seconds=1)).status_code == 409

    def test_a_new_session_can_start_once_the_last_one_ended(self, client):
        enrol(client)
        start(client, ago(minutes=5))
        end(client, ago(seconds=1))
        assert start(client, ago(minutes=1)).status_code == 201


class TestCancel:
    def test_cancel_returns_what_it_discarded(self, client):
        enrol(client)
        started = start(client, ago(minutes=5)).json()
        body = client.post(
            "/admin/sessions/cancel", json={"employee_id": "PW33744"}, headers=ADMIN
        ).json()
        assert body["status"] == "cancelled"
        assert body["started"] is True
        assert body["was"] == "recording"
        assert body["start_date"] == started["start_date"]
        assert body["start_time"] == started["start_time"]

    def test_cancelling_a_scheduled_session_says_it_had_not_started(self, client):
        enrol(client)
        start(client, ahead(hours=1))
        body = client.post(
            "/admin/sessions/cancel", json={"employee_id": "PW33744"}, headers=ADMIN
        ).json()
        assert body["started"] is False
        assert body["was"] == "scheduled"

    def test_cancelling_nothing_is_404(self, client):
        enrol(client)
        response = client.post(
            "/admin/sessions/cancel", json={"employee_id": "PW33744"}, headers=ADMIN
        )
        assert response.status_code == 404

    def test_cancel_then_start_is_the_documented_way_to_fix_a_wrong_time(self, client):
        enrol(client)
        start(client, ("2020-01-01", "00:00:00"))
        client.post("/admin/sessions/cancel", json={"employee_id": "PW33744"}, headers=ADMIN)
        assert start(client, ago(minutes=1)).status_code == 201

    def test_a_cancelled_session_cannot_be_ended(self, client):
        enrol(client)
        start(client, ago(minutes=5))
        client.post("/admin/sessions/cancel", json={"employee_id": "PW33744"}, headers=ADMIN)
        assert end(client, ago(seconds=1)).status_code == 409


class TestReEnrolment:
    def test_a_session_survives_the_laptop_re_enrolling(self, client):
        """A redeployed backend or a reinstalled agent changes install_id.
        A session opened before that still has to be closeable after."""
        enrol(client, install_id="install-1")
        start(client, ago(minutes=5))
        enrol(client, install_id="install-2")  # same person, new machine identity

        response = end(client, ago(seconds=1))
        assert response.status_code == 202
        job = client.get(f"/admin/jobs/{response.json()['job_id']}", headers=ADMIN).json()
        assert job["install_id"] == "install-2"


class TestAuthAndListing:
    def test_every_session_route_needs_the_admin_key(self, client):
        enrol(client)
        assert client.post(
            "/admin/sessions/start", json=start_body(ago(minutes=1))
        ).status_code == 401
        assert client.post(
            "/admin/sessions/end", json=end_body(ago(minutes=1))
        ).status_code == 401
        assert client.post(
            "/admin/sessions/cancel", json={"employee_id": "PW33744"}
        ).status_code == 401
        assert client.get("/admin/sessions").status_code == 401

    def test_the_listing_shows_what_happened_newest_first(self, client):
        enrol(client)
        start(client, ago(minutes=9))
        client.post("/admin/sessions/cancel", json={"employee_id": "PW33744"}, headers=ADMIN)
        start(client, ago(minutes=3))

        rows = client.get(
            "/admin/sessions", params={"employee_id": "pw33744"}, headers=ADMIN
        ).json()
        assert [r["status"] for r in rows] == ["recording", "cancelled"]
        assert all(r["start_date"] and r["start_utc"] for r in rows)


class TestRoundTrip:
    @staticmethod
    async def _enrol(ac) -> str:
        response = await ac.post(
            "/devices/register",
            json={
                "enrollment_key": ENROLL_KEY,
                "install_id": "install-1",
                "email_id": "ravi@pw.live",
                "employee_id": "PW33744",
            },
        )
        return response.json()["device_token"]

    @staticmethod
    async def _act_as_laptop(ac, token: str, *, audio: bytes, metadata: dict):
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
        job = jobs[0]
        await ac.post(
            f"/jobs/{job['job_id']}/clip",
            headers=auth,
            files={
                "clip": ("clip.mp3", audio, "audio/mpeg"),
                "metadata": (None, json.dumps(metadata), "application/json"),
            },
        )
        return job

    @pytest.mark.asyncio
    async def test_start_then_end_hands_back_the_audio(self, settings):
        audio = b"ID3session-payload" * 200
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://test"
        ) as ac:
            token = await self._enrol(ac)
            started = (
                await ac.post(
                    "/admin/sessions/start",
                    json=start_body(("2026-08-17", "14:30:00")),
                    headers=ADMIN,
                )
            ).json()

            finish = asyncio.create_task(
                ac.post(
                    "/admin/sessions/end",
                    json=end_body(("2026-08-17", "14:35:00")),
                    headers=ADMIN,
                    timeout=30,
                )
            )
            job = await self._act_as_laptop(
                ac,
                token,
                audio=audio,
                metadata={
                    "clip_start": "2026-08-17T09:00:00Z",
                    "clip_end": "2026-08-17T09:05:00Z",
                    "gaps": [],
                    "partial": False,
                },
            )
            response = await finish

        assert job["window_seconds"] == 300
        assert job["timestamp"] == "2026-08-17T09:02:30Z"

        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mpeg"
        assert response.content == audio
        assert response.headers["X-OngoingRec-Session-Id"] == started["session_id"]
        assert response.headers["X-OngoingRec-Clip-Start"] == "2026-08-17T09:00:00Z"
        assert response.headers["X-OngoingRec-Clip-End"] == "2026-08-17T09:05:00Z"

    @pytest.mark.asyncio
    async def test_a_gap_in_the_session_is_reported_not_hidden(self, settings):
        """Audio the laptop never captured is padded, so the operator has to be
        told which stretches are absence rather than a quiet room."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://test"
        ) as ac:
            token = await self._enrol(ac)
            await ac.post(
                "/admin/sessions/start",
                json=start_body(("2026-08-17", "14:30:00")),
                headers=ADMIN,
            )
            finish = asyncio.create_task(
                ac.post(
                    "/admin/sessions/end",
                    json=end_body(("2026-08-17", "14:35:00")),
                    headers=ADMIN,
                    timeout=30,
                )
            )
            await self._act_as_laptop(
                ac,
                token,
                audio=b"partial-audio",
                metadata={
                    "clip_start": "2026-08-17T09:00:00Z",
                    "clip_end": "2026-08-17T09:05:00Z",
                    "gaps": [{"start": "2026-08-17T09:02:00Z", "seconds": 45.0}],
                    "partial": True,
                },
            )
            response = await finish

        assert response.status_code == 200
        assert response.headers["X-OngoingRec-Partial"] == "true"
        assert json.loads(response.headers["X-OngoingRec-Gaps"])[0]["seconds"] == 45.0

    @pytest.mark.asyncio
    async def test_no_recording_for_that_window_is_a_404_not_a_broken_file(self, settings):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://test"
        ) as ac:
            token = await self._enrol(ac)
            await ac.post(
                "/admin/sessions/start",
                json=start_body(("2020-01-01", "10:00:00")),
                headers=ADMIN,
            )
            finish = asyncio.create_task(
                ac.post(
                    "/admin/sessions/end",
                    json=end_body(("2020-01-01", "10:05:00")),
                    headers=ADMIN,
                    timeout=30,
                )
            )
            auth = {"Authorization": f"Bearer {token}"}
            for _ in range(100):
                jobs = (
                    await ac.get(
                        "/jobs/poll",
                        params={"install_id": "install-1", "wait": 0},
                        headers=auth,
                    )
                ).json()["jobs"]
                if jobs:
                    break
                await asyncio.sleep(0.05)
            await ac.post(
                f"/jobs/{jobs[0]['job_id']}/error",
                headers=auth,
                json={"code": "NO_RECORDING", "detail": "nothing recorded then"},
            )
            response = await finish

        assert response.status_code == 404
