"""Configuration and identity for OngoingRec.

The counsellor is asked for an Email ID and an Employee ID exactly once, by
the installer (PRD sections 3.1 and 26). Everything after that reads from
here: service start, Windows restart, segment rotation and clip retrieval all
load the stored values and never prompt.

Layout on disk::

    %ProgramData%\\PW\\OngoingRec\\        (Windows)
    ~/.ongoingrec/                        (macOS/Linux development)
      config.json     non-secret settings, human-readable for support
      secrets.bin     device token + enrollment key, DPAPI-encrypted
      data/index.db   segment and job index
      recordings/     audio segments
      logs/

Secrets are kept in a separate file rather than inline so config.json stays
readable and editable during support calls without risking the token.
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "config.json"
SECRETS_FILENAME = "secrets.bin"
AGENT_VERSION = "0.1.0"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ConfigError(Exception):
    """Configuration is missing, malformed, or fails validation."""


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


def default_home() -> Path:
    """Root directory for configuration, recordings, index and logs.

    ``ONGOINGREC_HOME`` overrides it, which is how the test suite and the
    ``--config`` development flow keep out of the real install's way.
    """
    override = os.environ.get("ONGOINGREC_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        program_data = os.environ.get("ProgramData", r"C:\ProgramData")
        return Path(program_data) / "PW" / "OngoingRec"
    return Path.home() / ".ongoingrec"


# --------------------------------------------------------------------------
# Secret storage
# --------------------------------------------------------------------------


def _dpapi_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import win32crypt  # noqa: F401
    except ImportError:
        return False
    return True


def _encrypt(payload: bytes) -> bytes:
    """Encrypt secrets at rest.

    On Windows this is DPAPI with ``CRYPTPROTECT_LOCAL_MACHINE``, so the
    LocalSystem service account can decrypt what the installer wrote. Off
    Windows there is no equivalent that buys anything real, so the bytes are
    stored as-is behind 0600 permissions -- development only, and
    :func:`secrets_are_encrypted` reports the difference so the service can
    log it loudly at startup.
    """
    if _dpapi_available():
        import win32crypt

        CRYPTPROTECT_LOCAL_MACHINE = 0x4
        return win32crypt.CryptProtectData(
            payload, "OngoingRec", None, None, None, CRYPTPROTECT_LOCAL_MACHINE
        )
    return payload


def _decrypt(payload: bytes) -> bytes:
    if _dpapi_available():
        import win32crypt

        return win32crypt.CryptUnprotectData(payload, None, None, None, 0)[1]
    return payload


def secrets_are_encrypted() -> bool:
    return _dpapi_available()


@dataclass
class Secrets:
    """Credentials for talking to the central backend.

    ``enrollment_key`` is the shared key shipped in the installer. It is used
    once, for :meth:`register`, and then cleared -- a laptop that has enrolled
    no longer carries the key that would let an attacker enroll a new device.
    ``device_token`` is the per-device credential the backend issues in return.
    """

    enrollment_key: str = ""
    device_token: str = ""

    @classmethod
    def load(cls, home: Path) -> "Secrets":
        path = home / SECRETS_FILENAME
        if not path.exists():
            return cls()
        try:
            raw = _decrypt(path.read_bytes())
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - corrupt secrets must not crash boot
            raise ConfigError(f"could not read {path}: {exc}") from exc
        return cls(
            enrollment_key=data.get("enrollment_key", ""),
            device_token=data.get("device_token", ""),
        )

    def save(self, home: Path) -> None:
        home.mkdir(parents=True, exist_ok=True)
        path = home / SECRETS_FILENAME
        payload = json.dumps(asdict(self)).encode("utf-8")
        _atomic_write_bytes(path, _encrypt(payload), mode=0o600)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass
class Config:
    """Non-secret settings. Defaults are the shipped production values."""

    # Identity, captured once at install time.
    email_id: str = ""
    employee_id: str = ""
    install_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    # Backend transport.
    backend_base_url: str = ""
    poll_timeout_seconds: int = 30
    poll_retry_seconds: int = 10
    heartbeat_interval_seconds: int = 300
    upload_max_attempts: int = 8

    # Audio capture. 16 kHz mono is speech-grade; 32 kbps CBR MP3 keeps a
    # 30-minute segment near 7 MB and, being constant rate, stays seekable
    # even when a file is truncated by a power loss.
    sample_rate: int = 16000
    channels: int = 1
    mp3_bitrate_kbps: int = 32
    input_device: str | None = None

    # Segmentation and retrieval.
    segment_seconds: int = 1800
    retrieval_window_seconds: int = 600

    # Retention. The backend pulls daily; 72 hours covers a weekend, a laptop
    # left switched off, or a backend outage without unbounded disk growth.
    retention_hours: int = 72
    min_free_disk_mb: int = 2048
    retention_sweep_seconds: int = 3600

    # Local API. Loopback only: the production path to the backend is the
    # outbound poller, so nothing needs to reach this port from off-box.
    api_host: str = "127.0.0.1"
    api_port: int = 8765
    # Empty means unauthenticated, which is the sane default on loopback where
    # the OS already decides who can connect. Set it on shared machines.
    local_api_token: str = ""

    log_level: str = "INFO"

    # Set when the session-0 microphone spike shows a LocalSystem service
    # cannot open a capture device; the service then supervises a capture
    # agent in the interactive session instead of recording in-process.
    capture_in_session_agent: bool = False

    home: Path = field(default_factory=default_home)

    # -- derived paths ----------------------------------------------------

    @property
    def recordings_dir(self) -> Path:
        return self.home / "recordings"

    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "index.db"

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"

    @property
    def config_path(self) -> Path:
        return self.home / CONFIG_FILENAME

    def ensure_dirs(self) -> None:
        for path in (self.home, self.recordings_dir, self.data_dir, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)

    # -- persistence ------------------------------------------------------

    @classmethod
    def load(cls, home: Path | None = None) -> "Config":
        """Load configuration, raising if the install was never configured."""
        home = Path(home) if home else default_home()
        path = home / CONFIG_FILENAME
        if not path.exists():
            raise ConfigError(
                f"no configuration at {path}. Run the installer, or "
                f"'ongoingrec configure --email ... --employee-id ...'"
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
        config = cls.from_dict(data, home=home)
        config.validate()
        return config

    @classmethod
    def from_dict(cls, data: dict[str, Any], home: Path | None = None) -> "Config":
        known = {f.name for f in fields(cls)} - {"home"}
        kwargs = {k: v for k, v in data.items() if k in known}
        config = cls(**kwargs)
        if home is not None:
            config.home = Path(home)
        return config

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("home", None)
        return data

    def save(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"
        _atomic_write_bytes(self.config_path, payload.encode("utf-8"))

    # -- validation -------------------------------------------------------

    def validate(self) -> None:
        """Fail loudly on a configuration that cannot record or be retrieved.

        Called at service start rather than at first use, so a bad install
        surfaces immediately instead of at the moment the backend needs a clip.
        """
        errors: list[str] = []
        if not _EMAIL_RE.match(self.email_id or ""):
            errors.append(f"email_id is not a valid address: {self.email_id!r}")
        if not (self.employee_id or "").strip():
            errors.append("employee_id must not be empty")
        if self.segment_seconds <= 0:
            errors.append("segment_seconds must be positive")
        if self.retrieval_window_seconds <= 0:
            errors.append("retrieval_window_seconds must be positive")
        if self.sample_rate < 8000:
            errors.append("sample_rate must be at least 8000 Hz")
        if self.channels not in (1, 2):
            errors.append("channels must be 1 or 2")
        if not 8 <= self.mp3_bitrate_kbps <= 320:
            errors.append("mp3_bitrate_kbps must be between 8 and 320")
        if self.retention_hours <= 0:
            errors.append("retention_hours must be positive")
        if errors:
            raise ConfigError("invalid configuration: " + "; ".join(errors))

    def identity_matches(self, email_id: str | None, employee_id: str | None) -> bool:
        """Whether a request's identifiers refer to this installation.

        PRD section 14: when both identifiers are supplied they must agree,
        and both must match the configured install. Comparison is
        case-insensitive on email and whitespace-tolerant on employee ID,
        because these arrive from backend records typed by humans.
        """
        if email_id is not None:
            if email_id.strip().lower() != self.email_id.strip().lower():
                return False
        if employee_id is not None:
            if employee_id.strip().casefold() != self.employee_id.strip().casefold():
                return False
        return email_id is not None or employee_id is not None


def _atomic_write_bytes(path: Path, payload: bytes, mode: int = 0o644) -> None:
    """Write via a temp file and replace, so a crash cannot truncate config."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(payload)
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass  # Windows ACLs govern instead; ProgramData is already restricted.
    os.replace(tmp, path)
