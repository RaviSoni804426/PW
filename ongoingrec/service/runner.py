"""The supervisor that runs everything, on any platform.

Four things run concurrently for the life of the service: the recorder, the
outbound poller, the retention sweep, and the loopback API. This module owns
their startup order, their shutdown order, and the rule that binds them --
**recording is the priority**. If the backend is unreachable, if the API port
is taken, if retention fails, the microphone keeps recording. Audio not
captured is gone forever; everything else can be retried.

The Windows service wrapper is a thin shell over this class, so what runs
under LocalSystem is the same code a developer runs from a terminal.
"""

from __future__ import annotations

import threading
import time

from ..config import Config
from ..index import Database
from ..logging_setup import get_logger
from ..retention import enforce
from ..segments import Recorder
from ..transport.poller import JobPoller

log = get_logger(__name__)

# A restarting service can find the port still held by the process it replaced.
API_BIND_ATTEMPTS = 4
API_BIND_RETRY_SECONDS = 3.0


def build_source_factory(config: Config):
    """Return a callable producing a fresh microphone source on each attempt.

    A factory rather than an instance because the recorder reopens capture
    after a device failure, and a used PortAudio stream cannot be restarted.
    """
    from ..audio.capture import DeviceAudioSource

    def factory():
        return DeviceAudioSource(
            sample_rate=config.sample_rate,
            channels=config.channels,
            device=config.input_device,
        )

    return factory


class ServiceRunner:
    """Starts, supervises and stops the whole service."""

    def __init__(self, config: Config, *, source_factory=None, serve_api: bool = True):
        self.config = config
        self.serve_api = serve_api
        self.source_factory = source_factory or build_source_factory(config)

        self.db: Database | None = None
        self.recorder: Recorder | None = None
        self.poller: JobPoller | None = None
        self._api_server = None
        self._api_thread: threading.Thread | None = None
        self._api_thread_supervisor: threading.Thread | None = None
        self._retention_thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.config.ensure_dirs()
        self.config.validate()
        self.db = Database(self.config.db_path)

        log.info(
            "OngoingRec starting for %s / %s (install %s)",
            self.config.email_id,
            self.config.employee_id,
            self.config.install_id,
        )

        # Recording first, and never conditional on anything else succeeding.
        self.recorder = Recorder(self.config, self.db, self.source_factory)
        self.recorder.start()

        if self.config.backend_base_url:
            self.poller = JobPoller(self.config, self.db, recorder=self.recorder)
            self.poller.start()
        else:
            log.warning(
                "no backend_base_url configured; recording locally with no way to "
                "deliver clips to the backend"
            )

        self._retention_thread = threading.Thread(
            target=self._retention_loop, name="retention", daemon=True
        )
        self._retention_thread.start()

        if self.serve_api:
            self._start_api()

    def wait(self) -> None:
        """Block until :meth:`stop` is called."""
        self._stop.wait()

    def stop(self) -> None:
        """Shut down in the order that preserves the most audio.

        The recorder goes first so the open segment is finalized while there
        is still time to do it -- Windows gives a limited grace period at
        shutdown, and a segment left unfinalized costs real recorded audio
        (PRD section 19).
        """
        if self._stop.is_set():
            return
        self._stop.set()
        log.info("OngoingRec stopping")

        if self.recorder is not None:
            self.recorder.stop()
        if self.poller is not None:
            self.poller.stop()
        self._stop_api()
        if self.db is not None:
            self.db.close()
        log.info("OngoingRec stopped")

    def run_forever(self) -> None:
        self.start()
        try:
            self.wait()
        finally:
            self.stop()

    # -- background work ---------------------------------------------------

    def _retention_loop(self) -> None:
        while not self._stop.wait(self.config.retention_sweep_seconds):
            try:
                result = enforce(
                    self.config,
                    self.db,
                    protect_segment_id=(
                        self.recorder.current_segment_id if self.recorder else None
                    ),
                )
                if result["deleted_by_age"] or result["deleted_by_space"]:
                    log.info("retention sweep: %s", result)
            except Exception:  # noqa: BLE001 - a failed sweep must not stop recording
                log.exception("retention sweep failed")

    def _start_api(self) -> None:
        """Bring up the loopback API without holding up startup.

        The bind is retried on its own thread rather than inline. On a service
        restart the previous process may still be shutting down and holding
        the port for several seconds, and waiting for that here would delay
        the service reporting itself RUNNING to Windows -- which the Service
        Control Manager treats as a failed start. Recording does not depend on
        the API, so it should never wait for it.
        """
        self._api_thread_supervisor = threading.Thread(
            target=self._api_bind_loop, name="api-supervisor", daemon=True
        )
        self._api_thread_supervisor.start()

    def _api_bind_loop(self) -> None:
        for attempt in range(API_BIND_ATTEMPTS):
            if self._stop.is_set() or self._start_api_once():
                return
            if attempt < API_BIND_ATTEMPTS - 1:
                log.info("retrying the local API bind in %.0fs", API_BIND_RETRY_SECONDS)
                if self._stop.wait(API_BIND_RETRY_SECONDS):
                    return
        log.error(
            "giving up on the local API after %d attempts; recording and clip "
            "delivery are unaffected",
            API_BIND_ATTEMPTS,
        )

    def _start_api_once(self) -> bool:
        try:
            import uvicorn

            from ..api.app import AppContext, create_app
        except ImportError as exc:
            log.error("local API unavailable: %s", exc)
            return True  # not a transient failure; retrying cannot help

        context = AppContext(
            config=self.config, db=self.db, recorder=self.recorder, poller=self.poller
        )
        app = create_app(context)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=self.config.api_host,
                port=self.config.api_port,
                log_level="warning",
                access_log=False,
            )
        )
        self._api_server = server

        def serve() -> None:
            try:
                server.run()
            except SystemExit:
                # uvicorn calls sys.exit() when it cannot bind. Without
                # catching it here the only trace is a line from uvicorn's own
                # logger, and this module would go on claiming the API started.
                log.error(
                    "local API could not bind %s:%d (port already in use?); "
                    "recording is unaffected",
                    self.config.api_host,
                    self.config.api_port,
                )
            except Exception:  # noqa: BLE001 - the API must never stop recording
                log.exception("local API stopped unexpectedly")

        self._api_thread = threading.Thread(target=serve, name="api", daemon=True)
        self._api_thread.start()

        # Report what actually happened rather than what was attempted: a
        # support engineer reading this log needs to know whether the port is
        # really there before spending time on why curl gets nothing.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if getattr(server, "started", False):
                log.info(
                    "local API listening on http://%s:%d",
                    self.config.api_host,
                    self.config.api_port,
                )
                return True
            if not self._api_thread.is_alive():
                return False  # serve() already logged why
            time.sleep(0.05)
        log.warning("local API did not report itself started within 5s")
        return False

    def _stop_api(self) -> None:
        if self._api_server is not None:
            self._api_server.should_exit = True
        # The supervisor may still be between bind attempts; self._stop is
        # already set, so it will notice and give up rather than bring the API
        # back after shutdown has begun.
        if self._api_thread_supervisor is not None:
            self._api_thread_supervisor.join(timeout=API_BIND_RETRY_SECONDS + 6)
        if self._api_thread is not None:
            self._api_thread.join(timeout=10)
        self._api_server = None
        self._api_thread = None
        self._api_thread_supervisor = None

    # -- events from the Windows service wrapper ---------------------------

    def on_suspend(self) -> None:
        """The machine is going to sleep: finalize and stand down."""
        log.info("system suspending; finalizing the current segment")
        if self.recorder is not None:
            self.recorder.request_restart()

    def on_resume(self) -> None:
        """The machine has woken.

        Capture is reopened rather than continued: the audio device may have
        changed while asleep, and the recorder needs a fresh time anchor. The
        sleep interval is left as a gap in the index, which is the truth about
        it, and retrieval pads it with silence so later audio keeps its real
        offset.
        """
        log.info("system resumed; reopening capture")
        if self.recorder is not None:
            self.recorder.request_restart()
