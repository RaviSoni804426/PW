"""Command line for OngoingRec.

The installer uses ``configure``. The service uses ``run``. The rest exist for
support: when a counsellor's laptop is not producing audio, ``status``,
``devices`` and ``selftest`` between them answer whether the problem is
configuration, the microphone, or the backend, without needing a debugger on
someone's machine.
"""

from __future__ import annotations

import argparse
import json
import signal
import sqlite3
import sys
import uuid
from datetime import timedelta
from pathlib import Path

from .config import AGENT_VERSION, Config, ConfigError, Secrets, default_home, secrets_are_encrypted
from .logging_setup import configure as configure_logging
from .logging_setup import get_logger

log = get_logger(__name__)


def _load_config(args) -> Config:
    home = Path(args.home) if args.home else default_home()
    return Config.load(home)


# --------------------------------------------------------------------------
# configure
# --------------------------------------------------------------------------


def cmd_configure(args) -> int:
    """Write the one-time identity (PRD sections 3.1 and 3.2).

    Called by the installer, exactly once. Everything afterwards reads what
    this wrote, which is what makes "never ask again" (section 3.3) true.
    """
    home = Path(args.home) if args.home else default_home()
    home.mkdir(parents=True, exist_ok=True)

    try:
        existing = Config.load(home)
    except ConfigError:
        existing = None

    if existing and not args.force:
        print(
            f"already configured for {existing.email_id} / {existing.employee_id}\n"
            f"use --force to overwrite",
            file=sys.stderr,
        )
        return 1

    config = existing or Config(home=home)
    config.home = home
    config.email_id = args.email.strip()
    config.employee_id = args.employee_id.strip()
    if args.backend_url:
        config.backend_base_url = args.backend_url.rstrip("/")
    if args.window_seconds:
        config.retrieval_window_seconds = args.window_seconds
    if args.retention_hours:
        config.retention_hours = args.retention_hours
    if not existing:
        config.install_id = str(uuid.uuid4())

    try:
        config.validate()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    config.ensure_dirs()
    config.save()

    secrets = Secrets.load(home)
    if args.enrollment_key:
        secrets.enrollment_key = args.enrollment_key.strip()
        secrets.device_token = ""  # a new key means re-enrolling
    secrets.save(home)

    print(f"configured {config.email_id} / {config.employee_id}")
    print(f"  install id : {config.install_id}")
    print(f"  home       : {config.home}")
    print(f"  backend    : {config.backend_base_url or '(none)'}")
    if not secrets_are_encrypted():
        print("  note       : secrets are NOT encrypted on this platform (development only)")

    if args.register and config.backend_base_url:
        return _register_now(config)
    return 0


def _register_now(config: Config) -> int:
    """Enrol immediately so a bad URL or key fails during installation.

    Discovering at 3 a.m. that every laptop in the fleet was installed with a
    typo in the backend URL is a much worse way to learn it.
    """
    from .transport.client import BackendClient, BackendError

    secrets = Secrets.load(config.home)
    try:
        with BackendClient(config) as client:
            token = client.register(secrets.enrollment_key)
    except BackendError as exc:
        print(f"registration failed: {exc}", file=sys.stderr)
        return 3
    Secrets(enrollment_key="", device_token=token).save(config.home)
    print("  registered : yes")
    return 0


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


def cmd_run(args) -> int:
    """Run the service in the foreground (development, and the capture agent)."""
    from .service.runner import ServiceRunner

    config = _load_config(args)
    configure_logging(config.log_dir, level=args.log_level or config.log_level, console=True)

    runner = ServiceRunner(config, serve_api=not args.no_api)

    def handle_signal(signum, _frame):
        log.info("received signal %s", signum)
        runner.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, OSError):
            pass  # not on the main thread, or unsupported on this platform

    runner.start()
    try:
        runner.wait()
    except KeyboardInterrupt:
        pass
    finally:
        runner.stop()
    return 0


# --------------------------------------------------------------------------
# status / devices
# --------------------------------------------------------------------------


def _open_index_read_only(config):
    """Open the segment index for a command that only ever reads it.

    The service writes as LocalSystem, so on a real installation the index
    belongs to Administrators and an ordinary user cannot write it. These
    commands have no business writing anyway, and read-only access is what
    lets support run them without an elevated prompt. Returns None, having
    explained itself, when even reading is not permitted.
    """
    from .index import Database

    if not config.db_path.exists():
        print(
            f"no recording index at {config.db_path}; the service has not run yet",
            file=sys.stderr,
        )
        return None
    try:
        return Database(config.db_path, read_only=True)
    except sqlite3.Error as exc:
        print(
            f"cannot read the recording index at {config.db_path}: {exc}\n"
            f"try again from an elevated prompt.",
            file=sys.stderr,
        )
        return None


def cmd_status(args) -> int:
    """What this installation knows about itself, without the service running."""
    from .retention import free_disk_mb
    from .timeutil import format_utc

    config = _load_config(args)
    db = _open_index_read_only(config)
    if db is None:
        return 1
    latest = db.latest_segment()
    secrets = Secrets.load(config.home)

    info = {
        "version": AGENT_VERSION,
        "email_id": config.email_id,
        "employee_id": config.employee_id,
        "install_id": config.install_id,
        "home": str(config.home),
        "backend_base_url": config.backend_base_url or None,
        "registered": bool(secrets.device_token),
        "secrets_encrypted": secrets_are_encrypted(),
        "segment_count": db.segment_count(),
        "latest_segment_end": format_utc(latest.effective_end) if latest else None,
        "free_disk_mb": round(free_disk_mb(config.recordings_dir), 1),
        "retention_hours": config.retention_hours,
        "retrieval_window_seconds": config.retrieval_window_seconds,
    }
    db.close()
    print(json.dumps(info, indent=2))
    return 0


def cmd_devices(args) -> int:
    """List microphones. The first thing to check when nothing is recording."""
    from .audio.devices import NoInputDeviceError, list_input_devices, resolve_device

    try:
        devices = list_input_devices()
    except NoInputDeviceError as exc:
        print(f"no input devices: {exc}", file=sys.stderr)
        return 1

    for device in devices:
        marker = " (system default)" if device.is_default else ""
        print(f"[{device.index}] {device.name}{marker} - {device.channels}ch")

    try:
        config = _load_config(args)
    except ConfigError:
        return 0
    try:
        chosen = resolve_device(config.input_device)
        print(f"\nthis installation would record from: {chosen.name}")
    except NoInputDeviceError as exc:
        print(f"\ncould not resolve an input device: {exc}", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------
# fetch / selftest
# --------------------------------------------------------------------------


def cmd_fetch(args) -> int:
    """Extract a clip locally, bypassing HTTP and the backend entirely.

    On a laptop where the backend never receives audio, this separates "the
    recording is wrong" from "the delivery is wrong" in one command.
    """
    from .extract import ExtractionError, NoRecordingError, extract_clip
    from .timeutil import parse_timestamp

    config = _load_config(args)
    db = _open_index_read_only(config)
    if db is None:
        return 1
    try:
        clip = extract_clip(
            config,
            db,
            parse_timestamp(args.timestamp),
            window_seconds=args.window_seconds,
            output_dir=Path(args.output).parent if args.output else None,
        )
    except (NoRecordingError, ExtractionError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        db.close()

    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        clip.path.replace(destination)
        clip.path = destination

    print(json.dumps({**clip.to_dict(), "path": str(clip.path)}, indent=2))
    return 0


def cmd_selftest(args) -> int:
    """Record watermarked audio and prove retrieval returns the right moments.

    Runs the real recorder, segmenter and extractor against a synthetic source
    whose every second carries its own timestamp, then decodes a clip and
    checks the seconds match what was asked for. It verifies the machine's
    ffmpeg and the whole pipeline without needing a microphone -- useful on a
    laptop where you want to know whether the plumbing works before trusting
    it with a day of recording.
    """
    import numpy as np

    from .audio.capture import SyntheticAudioSource
    from .audio.watermark import CYCLE_SECONDS, decode_timeline
    from .extract import extract_clip
    from .index import Database
    from .segments import Recorder
    from .timeutil import parse_timestamp

    configure_logging(None, level="WARNING", console=True)

    home = Path(args.home) if args.home else default_home()
    config = Config(email_id="selftest@example.com", employee_id="SELFTEST", home=home / "selftest")
    config.ensure_dirs()
    db = Database(config.db_path)

    start = parse_timestamp("2026-01-01T10:00:00Z")
    seconds = int(args.minutes * 60)
    print(f"recording {args.minutes} simulated minutes of watermarked audio...")

    recorder = Recorder(
        config,
        db,
        lambda: SyntheticAudioSource(
            sample_rate=config.sample_rate,
            channels=config.channels,
            start_time=start,
            duration_seconds=seconds,
        ),
        resync_to_wall_clock=False,
    )
    recorder.run_until_source_exhausted()

    segments = db.segments_overlapping(start, start + timedelta(seconds=seconds + 60))
    print(f"  {len(segments)} segment(s):")
    for segment in segments:
        print(
            f"    {segment.start:%H:%M:%S} -> {segment.effective_end:%H:%M:%S}  "
            f"{segment.duration_seconds:7.1f}s  {segment.file_path.name}"
        )

    # Ask for a moment near a 30-minute boundary, so the clip has to be
    # stitched from two segments if the run was long enough to have one.
    requested = start + timedelta(seconds=min(seconds - 60, 29 * 60))
    clip = extract_clip(config, db, requested, window_seconds=min(600, seconds - 30))
    print(f"\nrequested {requested:%H:%M:%S}, got {clip.start:%H:%M:%S}-{clip.end:%H:%M:%S}")
    print(f"  from segment(s) {clip.segment_ids}, {len(clip.gaps)} gap(s)")

    from .audio.encoder import ffmpeg_path
    import subprocess

    decoded = subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-i", str(clip.path),
         "-f", "s16le", "-ar", str(config.sample_rate), "-ac", "1", "pipe:1"],
        capture_output=True,
    )
    samples = np.frombuffer(decoded.stdout, dtype="<i2")
    found = decode_timeline(samples, config.sample_rate)
    expected = [int(clip.start.timestamp() + i) % CYCLE_SECONDS for i in range(len(found))]
    mismatches = sum(1 for a, b in zip(found, expected) if a != b)

    db.close()
    if mismatches:
        print(f"\nFAILED: {mismatches} of {len(found)} seconds decoded to the wrong time")
        return 1
    print(f"\nOK: all {len(found)} seconds of the returned clip carry the expected timestamps")
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ongoingrec", description=__doc__)
    parser.add_argument("--version", action="version", version=f"OngoingRec {AGENT_VERSION}")
    parser.add_argument("--home", help="installation root (default: platform ProgramData/home)")
    sub = parser.add_subparsers(dest="command", required=True)

    configure_cmd = sub.add_parser("configure", help="write the one-time identity (installer)")
    configure_cmd.add_argument("--email", required=True)
    configure_cmd.add_argument("--employee-id", required=True)
    configure_cmd.add_argument("--backend-url", default="")
    configure_cmd.add_argument("--enrollment-key", default="")
    configure_cmd.add_argument("--window-seconds", type=int)
    configure_cmd.add_argument("--retention-hours", type=int)
    configure_cmd.add_argument(
        "--register",
        action="store_true",
        help="enrol with the backend now, so a bad URL or key fails at install time",
    )
    configure_cmd.add_argument("--force", action="store_true", help="overwrite existing settings")
    configure_cmd.set_defaults(func=cmd_configure)

    run_cmd = sub.add_parser("run", help="run the service in the foreground")
    run_cmd.add_argument("--no-api", action="store_true", help="skip the loopback API")
    run_cmd.add_argument("--log-level", default=None)
    run_cmd.set_defaults(func=cmd_run)

    status_cmd = sub.add_parser("status", help="show this installation's state")
    status_cmd.set_defaults(func=cmd_status)

    devices_cmd = sub.add_parser("devices", help="list microphones")
    devices_cmd.set_defaults(func=cmd_devices)

    fetch_cmd = sub.add_parser("fetch", help="extract a clip locally")
    fetch_cmd.add_argument("timestamp", help="ISO-8601; naive values are local time")
    fetch_cmd.add_argument("--window-seconds", type=int)
    fetch_cmd.add_argument("--output", help="write the clip here")
    fetch_cmd.set_defaults(func=cmd_fetch)

    selftest_cmd = sub.add_parser(
        "selftest", help="verify the pipeline end to end without a microphone"
    )
    selftest_cmd.add_argument("--minutes", type=float, default=45.0)
    selftest_cmd.set_defaults(func=cmd_selftest)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
