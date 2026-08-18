from __future__ import annotations

import json

import pytest

from ongoingrec.config import Config, ConfigError, Secrets


class TestPersistence:
    def test_identity_survives_a_restart(self, home):
        """PRD section 3.3: Email ID and Employee ID are never asked for twice."""
        original = Config(email_id="abc@example.com", employee_id="EMP001", home=home)
        original.save()

        reloaded = Config.load(home)
        assert reloaded.email_id == "abc@example.com"
        assert reloaded.employee_id == "EMP001"
        assert reloaded.install_id == original.install_id

    def test_load_without_configuration_explains_how_to_fix_it(self, home):
        with pytest.raises(ConfigError, match="ongoingrec configure"):
            Config.load(home)

    def test_unknown_keys_are_ignored(self, home):
        """A newer config file must not stop an older agent from starting."""
        (home / "config.json").write_text(
            json.dumps(
                {
                    "email_id": "abc@example.com",
                    "employee_id": "EMP001",
                    "some_future_setting": True,
                }
            )
        )
        assert Config.load(home).employee_id == "EMP001"

    def test_corrupt_json_is_reported_clearly(self, home):
        (home / "config.json").write_text("{not json")
        with pytest.raises(ConfigError, match="not valid JSON"):
            Config.load(home)

    def test_save_is_atomic(self, home):
        config = Config(email_id="abc@example.com", employee_id="EMP001", home=home)
        config.save()
        config.save()
        assert not list(home.glob("*.tmp"))


class TestValidation:
    @pytest.mark.parametrize("email", ["", "not-an-email", "missing@domain", "@example.com"])
    def test_rejects_bad_email(self, home, email):
        with pytest.raises(ConfigError, match="email_id"):
            Config(email_id=email, employee_id="EMP001", home=home).validate()

    @pytest.mark.parametrize("employee_id", ["", "   "])
    def test_rejects_blank_employee_id(self, home, employee_id):
        with pytest.raises(ConfigError, match="employee_id"):
            Config(email_id="a@b.com", employee_id=employee_id, home=home).validate()

    def test_reports_every_problem_at_once(self, home):
        config = Config(email_id="bad", employee_id="", segment_seconds=0, home=home)
        with pytest.raises(ConfigError) as exc:
            config.validate()
        message = str(exc.value)
        assert "email_id" in message and "employee_id" in message and "segment_seconds" in message


class TestIdentityMatching:
    @pytest.fixture
    def config(self, home):
        return Config(email_id="abc@example.com", employee_id="EMP001", home=home)

    def test_email_only(self, config):
        assert config.identity_matches("abc@example.com", None)

    def test_employee_id_only(self, config):
        assert config.identity_matches(None, "EMP001")

    def test_both_when_they_agree(self, config):
        assert config.identity_matches("abc@example.com", "EMP001")

    def test_both_are_rejected_when_they_disagree(self, config):
        """PRD section 14: mismatched identifiers must be rejected, not guessed at."""
        assert not config.identity_matches("abc@example.com", "EMP999")
        assert not config.identity_matches("someone@else.com", "EMP001")

    def test_neither_identifier_is_not_a_match(self, config):
        assert not config.identity_matches(None, None)

    def test_tolerates_case_and_padding_from_backend_records(self, config):
        assert config.identity_matches("ABC@Example.com", " emp001 ")

    def test_other_installation_does_not_match(self, config):
        assert not config.identity_matches("xyz@example.com", None)
        assert not config.identity_matches(None, "EMP002")


class TestSecrets:
    def test_round_trip(self, home):
        Secrets(enrollment_key="enroll-123", device_token="tok-abc").save(home)
        loaded = Secrets.load(home)
        assert loaded.enrollment_key == "enroll-123"
        assert loaded.device_token == "tok-abc"

    def test_missing_file_yields_empty_secrets(self, home):
        assert Secrets.load(home) == Secrets()

    def test_secrets_live_outside_config_json(self, home):
        """The token must not leak into the file support staff read aloud."""
        config = Config(email_id="a@b.com", employee_id="E1", home=home)
        config.save()
        Secrets(device_token="tok-secret").save(home)
        assert "tok-secret" not in config.config_path.read_text()
