"""Deployment settings, read from the environment.

Everything that differs between a laptop and a hosted deployment arrives as an
environment variable, because that is what Coolify, Docker and systemd all
agree on.

Both credentials are optional, so the service deploys with nothing to
configure and anyone who has the URL can fetch a clip. That is a deliberate
choice for this deployment, not an oversight -- the URL itself is then the only
thing gating access, so treat it like a password. Setting either key later
turns its protection back on without a code change:

* ``ADMIN_API_KEY`` -- when set, ``/admin`` requires it. When unset, ``/admin``
  is open.
* ``ENROLLMENT_KEY`` -- when set, a laptop must present it to register. When
  unset, registration is open.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class SettingsError(Exception):
    """A setting is present but unusable."""


@dataclass(frozen=True)
class Settings:
    admin_api_key: str  # empty -> /admin is open
    enrollment_key: str  # empty -> device registration is open
    data_dir: Path
    max_clip_mb: int
    max_poll_wait: int
    job_retention_days: int

    @classmethod
    def from_env(cls) -> "Settings":
        admin_api_key = os.environ.get("ADMIN_API_KEY", "").strip()
        enrollment_key = os.environ.get("ENROLLMENT_KEY", "").strip()

        # A short key is worse than none: it looks like protection while adding
        # almost none. So an empty value is fine (open, by choice), but a value
        # that is present must be a real key rather than a guessable word.
        for name, value in (("ADMIN_API_KEY", admin_api_key), ("ENROLLMENT_KEY", enrollment_key)):
            if value and len(value) < 16:
                raise SettingsError(
                    f"{name} is set but shorter than 16 characters; "
                    f"use a real key or leave it unset to run open"
                )

        return cls(
            admin_api_key=admin_api_key,
            enrollment_key=enrollment_key,
            data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            max_clip_mb=int(os.environ.get("MAX_CLIP_MB", "128")),
            max_poll_wait=int(os.environ.get("MAX_POLL_WAIT", "60")),
            job_retention_days=int(os.environ.get("JOB_RETENTION_DAYS", "30")),
        )

    @property
    def admin_open(self) -> bool:
        return not self.admin_api_key

    @property
    def registration_open(self) -> bool:
        return not self.enrollment_key

    @property
    def db_path(self) -> Path:
        return self.data_dir / "backend.db"

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"
