"""Local API behaviour, including the exact status codes PRD section 28 asks for."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from ongoingrec.api.app import AppContext, create_app
from ongoingrec.audio.watermark import CYCLE_SECONDS, decode_timeline
from ongoingrec.timeutil import parse_timestamp

from .helpers import decode_bytes

TIMESTAMP = "2026-08-12T11:22:15Z"


def client_for(config, db) -> TestClient:
    return TestClient(create_app(AppContext(config=config, db=db)))


@pytest.fixture
def client(config, recorded):
    return client_for(config, recorded)


class TestHealth:
    def test_reports_ok(self, client):
        """PRD section 20."""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_reports_the_configured_identity_and_stored_audio(self, client, config):
        body = client.get("/health").json()
        assert body["email_id"] == config.email_id
        assert body["employee_id"] == config.employee_id
        assert body["segment_count"] == 3
        assert body["last_segment_end"] == "2026-08-12T12:30:00Z"

    def test_enrolment_is_read_from_stored_secrets(self, config, recorded):
        """A laptop that registered days ago is still registered, whether or
        not the poller has completed a cycle since this process started."""
        from ongoingrec.config import Secrets

        assert client_for(config, recorded).get("/health").json()["registered"] is False
        Secrets(device_token="tok-abc").save(config.home)
        assert client_for(config, recorded).get("/health").json()["registered"] is True

    def test_health_needs_no_token_even_when_one_is_set(self, config, recorded):
        """Monitoring must be able to tell a dead service from a locked one."""
        config.local_api_token = "s3cret"
        client = TestClient(create_app(AppContext(config=config, db=recorded)))
        assert client.get("/health").status_code == 200


class TestFetchValidation:
    """PRD section 15."""

    def test_email_and_timestamp_is_valid(self, client, config):
        response = client.post(
            "/recordings/fetch",
            json={"email_id": config.email_id, "timestamp": TIMESTAMP},
        )
        assert response.status_code == 200

    def test_employee_id_and_timestamp_is_valid(self, client, config):
        response = client.post(
            "/recordings/fetch",
            json={"employee_id": config.employee_id, "timestamp": TIMESTAMP},
        )
        assert response.status_code == 200

    def test_both_identifiers_together_are_valid(self, client, config):
        response = client.post(
            "/recordings/fetch",
            json={
                "email_id": config.email_id,
                "employee_id": config.employee_id,
                "timestamp": TIMESTAMP,
            },
        )
        assert response.status_code == 200

    def test_timestamp_alone_is_rejected(self, client):
        response = client.post("/recordings/fetch", json={"timestamp": TIMESTAMP})
        assert response.status_code == 400
        assert "email_id or employee_id" in response.json()["detail"]

    def test_identifier_without_a_timestamp_is_rejected(self, client, config):
        response = client.post("/recordings/fetch", json={"email_id": config.email_id})
        assert response.status_code == 400

    def test_unparseable_timestamp_is_rejected(self, client, config):
        response = client.post(
            "/recordings/fetch",
            json={"email_id": config.email_id, "timestamp": "yesterday afternoon"},
        )
        assert response.status_code == 400

    def test_absurd_window_is_rejected(self, client, config):
        response = client.post(
            "/recordings/fetch",
            json={"email_id": config.email_id, "timestamp": TIMESTAMP, "window_seconds": 0},
        )
        assert response.status_code == 400


class TestIdentityResolution:
    """PRD sections 14 and 23."""

    def test_another_installations_employee_id_gets_nothing(self, client):
        response = client.post(
            "/recordings/fetch", json={"employee_id": "EMP002", "timestamp": TIMESTAMP}
        )
        assert response.status_code == 404

    def test_another_installations_email_gets_nothing(self, client):
        response = client.post(
            "/recordings/fetch", json={"email_id": "xyz@example.com", "timestamp": TIMESTAMP}
        )
        assert response.status_code == 404

    def test_mismatched_pair_is_refused_rather_than_resolved(self, client, config):
        """Returning one counsellor's audio under another's identifier would be
        the worst possible failure, so a disagreement is refused outright."""
        response = client.post(
            "/recordings/fetch",
            json={
                "email_id": config.email_id,
                "employee_id": "EMP999",
                "timestamp": TIMESTAMP,
            },
        )
        assert response.status_code == 404

    def test_identifiers_are_matched_tolerantly(self, client):
        response = client.post(
            "/recordings/fetch",
            json={"email_id": "ABC@Example.COM", "timestamp": TIMESTAMP},
        )
        assert response.status_code == 200


class TestFetchResponse:
    def test_returns_audio_with_the_right_content_type(self, client, config):
        """PRD section 21."""
        response = client.post(
            "/recordings/fetch",
            json={"employee_id": config.employee_id, "timestamp": TIMESTAMP},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mpeg"
        assert len(response.content) > 1000

    def test_returned_audio_is_from_the_requested_time(self, client, config):
        """The end-to-end guarantee, checked through the HTTP layer."""
        response = client.post(
            "/recordings/fetch",
            json={"employee_id": config.employee_id, "timestamp": TIMESTAMP},
        )
        clip_start = parse_timestamp(response.headers["X-OngoingRec-Clip-Start"])
        assert clip_start == parse_timestamp("2026-08-12T11:17:15Z")

        decoded = decode_timeline(
            decode_bytes(response.content, config.sample_rate), config.sample_rate
        )
        expected = [int(clip_start.timestamp() + i) % CYCLE_SECONDS for i in range(len(decoded))]
        assert decoded == expected

    def test_boundary_crossing_request_returns_both_segments(self, client, config):
        """PRD section 18, over HTTP."""
        response = client.post(
            "/recordings/fetch",
            json={"employee_id": config.employee_id, "timestamp": "2026-08-12T11:29:00Z"},
        )
        assert response.status_code == 200
        assert len(response.headers["X-OngoingRec-Segments"].split(",")) == 2
        assert response.headers["X-OngoingRec-Partial"] == "false"
        assert json.loads(response.headers["X-OngoingRec-Gaps"]) == []

    def test_naive_timestamp_is_read_as_laptop_local_time(self, client, config):
        """The PRD's own example is naive; IST puts 16:52:15 local at 11:22:15Z."""
        response = client.post(
            "/recordings/fetch",
            json={"employee_id": config.employee_id, "timestamp": "2026-08-12T16:52:15"},
        )
        assert response.status_code == 200
        assert response.headers["X-OngoingRec-Requested-At"] == "2026-08-12T11:22:15Z"

    def test_filename_identifies_the_counsellor_and_the_moment(self, client, config):
        response = client.post(
            "/recordings/fetch",
            json={"employee_id": config.employee_id, "timestamp": TIMESTAMP},
        )
        assert "EMP001_20260812T111715Z.mp3" in response.headers["content-disposition"]

    def test_window_can_be_overridden_per_request(self, client, config):
        response = client.post(
            "/recordings/fetch",
            json={
                "employee_id": config.employee_id,
                "timestamp": TIMESTAMP,
                "window_seconds": 60,
            },
        )
        start = parse_timestamp(response.headers["X-OngoingRec-Clip-Start"])
        end = parse_timestamp(response.headers["X-OngoingRec-Clip-End"])
        assert (end - start).total_seconds() == pytest.approx(60)

    def test_temporary_clip_is_cleaned_up_after_sending(self, client, config):
        clip_dir = config.data_dir / "clips"
        client.post(
            "/recordings/fetch",
            json={"employee_id": config.employee_id, "timestamp": TIMESTAMP},
        )
        assert list(clip_dir.glob("*.mp3")) == []


class TestMissingRecordings:
    def test_timestamp_with_no_recording_is_a_404(self, client, config):
        """PRD section 28."""
        response = client.post(
            "/recordings/fetch",
            json={"employee_id": config.employee_id, "timestamp": "2026-08-12T03:00:00Z"},
        )
        assert response.status_code == 404
        assert "no recording" in response.json()["detail"].lower()


class TestAuth:
    @pytest.fixture
    def secured(self, config, recorded):
        config.local_api_token = "s3cret"
        return TestClient(create_app(AppContext(config=config, db=recorded)))

    def test_missing_token_is_rejected(self, secured, config):
        response = secured.post(
            "/recordings/fetch",
            json={"employee_id": config.employee_id, "timestamp": TIMESTAMP},
        )
        assert response.status_code == 401

    def test_correct_token_is_accepted(self, secured, config):
        response = secured.post(
            "/recordings/fetch",
            json={"employee_id": config.employee_id, "timestamp": TIMESTAMP},
            headers={"Authorization": "Bearer s3cret"},
        )
        assert response.status_code == 200
