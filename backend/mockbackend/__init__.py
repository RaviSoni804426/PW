"""A stand-in for the central backend.

Implements the contract in ``docs/backend-api.yaml`` so the full loop --
register, poll, extract, upload -- can be run and tested without the real
backend existing yet. It is also the reference the backend team can run
against while building the real thing.

Not for production: enrollment keys are compared in plain text, state lives in
memory, and every device shares one job queue namespace.
"""

from .app import create_mock_backend

__all__ = ["create_mock_backend"]
