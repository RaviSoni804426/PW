"""Input device discovery.

Kept apart from capture so that listing devices during support (
``ongoingrec devices``) does not require opening a stream, and so the capture
code has one place to ask "which device should I be on right now?".
"""

from __future__ import annotations

from dataclasses import dataclass

from ..logging_setup import get_logger

log = get_logger(__name__)


class NoInputDeviceError(Exception):
    """No usable microphone is present."""


@dataclass(frozen=True)
class InputDevice:
    index: int
    name: str
    channels: int
    default_samplerate: float
    is_default: bool


def _sounddevice():
    try:
        import sounddevice
    except (ImportError, OSError) as exc:
        # OSError here means PortAudio itself is missing, which is a broken
        # install rather than a missing Python package -- worth distinguishing.
        raise NoInputDeviceError(f"audio backend unavailable: {exc}") from exc
    return sounddevice


def list_input_devices() -> list[InputDevice]:
    sd = _sounddevice()
    try:
        default_index = sd.default.device[0]
    except (TypeError, IndexError):
        default_index = None

    devices: list[InputDevice] = []
    for index, info in enumerate(sd.query_devices()):
        if info.get("max_input_channels", 0) < 1:
            continue
        devices.append(
            InputDevice(
                index=index,
                name=info.get("name", f"device {index}"),
                channels=info["max_input_channels"],
                default_samplerate=info.get("default_samplerate", 0.0),
                is_default=(index == default_index),
            )
        )
    return devices


def resolve_device(preferred: str | None) -> InputDevice:
    """Pick the device to record from.

    *preferred* may be a device index or a case-insensitive substring of the
    name; when it is None or no longer present, the system default is used.
    Falling back rather than failing matters because a counsellor's configured
    USB headset will not always be plugged in, and recording from the built-in
    microphone beats recording nothing.
    """
    devices = list_input_devices()
    if not devices:
        raise NoInputDeviceError("no input devices with capture channels")

    if preferred:
        wanted = preferred.strip()
        if wanted.isdigit():
            for device in devices:
                if device.index == int(wanted):
                    return device
        for device in devices:
            if wanted.lower() in device.name.lower():
                return device
        log.warning(
            "configured input device %r not found; falling back to the system default",
            preferred,
        )

    for device in devices:
        if device.is_default:
            return device
    return devices[0]
