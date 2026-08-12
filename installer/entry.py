"""Frozen entry point for OngoingRec.exe.

One executable serves two callers with different conventions:

* The Service Control Manager launches it with **no arguments** and expects a
  service dispatcher. This is not a choice -- it is how Windows starts a
  service, and getting it wrong produces error 1053 with no further
  explanation.
* People and the installer run it with a subcommand (``configure``, ``status``,
  ``fetch``), and pywin32's own control verbs (``install``, ``start``, ``stop``,
  ``remove``) must keep working too.

Dispatching on argv here keeps that distinction in one obvious place.
"""

from __future__ import annotations

import multiprocessing
import sys

PYWIN32_VERBS = {"install", "update", "remove", "start", "stop", "restart", "debug"}


def main() -> int:
    # Required before anything else in a frozen build: without it, any
    # accidental process spawn re-runs the whole executable.
    multiprocessing.freeze_support()

    argv = sys.argv[1:]

    if not argv or (argv and argv[0] in PYWIN32_VERBS) or "--startup" in argv:
        from ongoingrec.service.win_service import main as service_main

        return service_main()

    from ongoingrec.__main__ import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
