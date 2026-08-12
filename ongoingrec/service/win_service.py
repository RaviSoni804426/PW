"""Windows service wrapper (PRD sections 5 and 19).

A thin shell over :class:`~ongoingrec.service.runner.ServiceRunner`, so what
runs under LocalSystem is the code that was developed and tested everywhere
else. What is genuinely Windows-specific lives here:

* **Automatic start.** Registered to start with Windows, with no logon and no
  user action, so a counsellor cannot forget to turn recording on.
* **Preshutdown.** The default shutdown notification gives a service only a
  few seconds -- often not enough to flush an encoder and close a file
  cleanly. Preshutdown asks for longer, which is what makes "the current
  segment is finalized" (section 19) actually true rather than aspirational.
* **Power events.** Sleep and wake are reported here and turned into a
  capture restart, so a segment never spans a suspend.

Installed and controlled with::

    OngoingRec.exe --startup auto install
    OngoingRec.exe start
    OngoingRec.exe stop
    OngoingRec.exe remove
"""

from __future__ import annotations

import sys

from ..config import Config
from ..logging_setup import configure, get_logger

log = get_logger(__name__)

SERVICE_NAME = "OngoingRec"
SERVICE_DISPLAY_NAME = "PW OngoingRec Recording Service"
SERVICE_DESCRIPTION = (
    "Records microphone audio in 30-minute segments and delivers requested "
    "clips to the PW central backend."
)

try:
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    HAVE_PYWIN32 = True
except ImportError:  # pragma: no cover - not importable off Windows
    HAVE_PYWIN32 = False


if HAVE_PYWIN32:

    class OngoingRecService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        # Audiosrv owns the audio endpoints. Without this dependency the
        # service can win the race at boot and find no capture device at all.
        _svc_deps_ = ["Audiosrv"]

        # Preshutdown buys time to flush the encoder and close the segment;
        # the plain shutdown notification often does not.
        _svc_accept_ = (
            win32service.SERVICE_ACCEPT_STOP
            | win32service.SERVICE_ACCEPT_PRESHUTDOWN
            | win32service.SERVICE_ACCEPT_POWEREVENT
        )

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.runner = None

        # -- control handlers ------------------------------------------

        def SvcStop(self) -> None:
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=30000)
            log.info("stop requested")
            self._shutdown()

        def SvcPreShutdown(self) -> None:
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=30000)
            log.info("system is shutting down; finalizing recording")
            self._shutdown()

        def SvcOtherEx(self, control, event_type, data) -> None:
            """Power transitions.

            PBT_APMSUSPEND fires as the machine goes to sleep and
            PBT_APMRESUMEAUTOMATIC as it wakes. Both end the current segment,
            because audio recorded either side of a suspend belongs to
            different moments and must not share a file.
            """
            if control == win32service.SERVICE_CONTROL_POWEREVENT:
                PBT_APMSUSPEND = 0x4
                PBT_APMRESUMEAUTOMATIC = 0x12
                PBT_APMRESUMESUSPEND = 0x7
                if self.runner is None:
                    return
                if event_type == PBT_APMSUSPEND:
                    self.runner.on_suspend()
                elif event_type in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                    self.runner.on_resume()

        def _shutdown(self) -> None:
            try:
                if self.runner is not None:
                    self.runner.stop()
            except Exception:  # noqa: BLE001 - shutdown must always complete
                log.exception("error while stopping")
            finally:
                win32event.SetEvent(self.stop_event)

        # -- main ------------------------------------------------------

        def SvcDoRun(self) -> None:
            from .runner import ServiceRunner

            try:
                config = Config.load()
            except Exception as exc:  # noqa: BLE001 - report, then exit cleanly
                servicemanager.LogErrorMsg(f"OngoingRec configuration error: {exc}")
                self.ReportServiceStatus(win32service.SERVICE_STOPPED)
                return

            configure(config.log_dir, level=config.log_level, console=False)
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )

            self.runner = ServiceRunner(config)
            try:
                self.runner.start()
            except Exception as exc:  # noqa: BLE001
                log.exception("failed to start")
                servicemanager.LogErrorMsg(f"OngoingRec failed to start: {exc}")
                self.ReportServiceStatus(win32service.SERVICE_STOPPED)
                return

            self.ReportServiceStatus(win32service.SERVICE_RUNNING)
            win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)
            log.info("service loop exited")


def main() -> int:
    """Entry point for service registration and control."""
    if not HAVE_PYWIN32:
        print(
            "The Windows service requires pywin32 on Windows "
            "(pip install 'ongoingrec[windows]').",
            file=sys.stderr,
        )
        return 1

    if len(sys.argv) == 1:
        # No arguments means the Service Control Manager launched us.
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(OngoingRecService)
        servicemanager.StartServiceCtrlDispatcher()
        return 0

    win32serviceutil.HandleCommandLine(OngoingRecService)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
