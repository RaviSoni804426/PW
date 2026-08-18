# PW OngoingRec

Continuous microphone recording on counsellor Windows laptops, with clip
retrieval by timestamp from a central backend.

Two deployables live in this repo:

| Piece | Where it runs | Entry point |
|---|---|---|
| **agent** (`agent/ongoingrec/`) | Windows laptop, LocalSystem service | `ongoingrec` CLI / `ongoingrec.service.win_service` |
| **backend** (`backend/backend/`) | Docker container | `python -m backend` |

Each is a separate installable project with its own `pyproject.toml`. They share
no code — only the HTTP contract in `docs/backend-api.yaml`. There is no
frontend; UI, dashboards and an admin console are all out of scope (PRD §29).

`backend/mockbackend/` is a runnable reference implementation of the same
contract, used for local development and by the agent's transport tests. It is
*not* the deployed backend, and `.dockerignore` keeps it out of the image.

It also predates the real backend by six days and is now largely redundant: the
agent's production `BackendClient` has been driven against `backend/backend/`
directly through the whole round trip (register → heartbeat → poll → upload →
download) and the contracts agree. **Nothing tests that they still agree**, so a
contract change landing in only one of them would leave the agent's tests green
while production broke. Either delete it and point `test_transport.py` at the
real backend, or add a conformance test — do not leave it as-is indefinitely.

Python 3.11+, FastAPI + uvicorn + httpx + pydantic, SQLite, ffmpeg for audio.
Built from a PRD (v5.0); README.md documents where it deliberately departs from it.

---

## The core invariant

Everything in the agent exists to protect one property:

> The audio returned for timestamp *T* is the audio captured at *T*.

Consequences that show up all over the code — do not "simplify" these away:

- **Elapsed time comes from sample counts or `time.monotonic()`, never from
  subtracting two wall-clock readings.** NTP corrections and DST shifts would
  otherwise corrupt a segment's timeline.
- **Segment boundaries anchor to UTC midnight**, not local midnight (DST
  fall-back would produce two colliding `01:00–01:30` segments).
- **MP3 is CBR with the Xing header suppressed.** The service gets killed
  abruptly; a truncated CBR file is still a recoverable sequence of frames, and
  a Xing header written on close would never be written at all.
- **A segment is indexed when it opens**, not when it closes, so a power-loss
  recording is still discoverable; the startup repair pass probes for its real
  length.
- **Recording is never blocked by anything else.** Backend unreachable, API port
  taken, retention failing — the microphone keeps running. Audio not captured is
  gone forever; everything else retries.
- **Internal recording gaps are padded with silence and reported**, so an offset
  inside a clip equals real elapsed time. Leading/trailing gaps are trimmed
  instead, and the clip's true start/end is returned.
- **Every timestamp crossing a module boundary or hitting storage is an aware UTC
  `datetime`.** Naive values only exist at the edges and are resolved immediately
  as *laptop local time*.

---

## Architecture

```
Windows laptop                                       Central backend (Docker)
 mic ─► ffmpeg ─► 30-min CBR MP3s ─► SQLite index
                          │
        extractor ◄───────┘
           ▲   └── clip + metadata ──► POST /jobs/{id}/clip
        poller ◄──── job ───────────── GET  /jobs/poll
                                       POST /devices/register, /devices/heartbeat
 loopback API 127.0.0.1:8765
   GET /health · POST /recordings/fetch
```

**The laptop is a 72-hour rolling buffer, not an archive.** Two rules delete
segments: age (`retention_hours`, default 72) and a free-disk floor
(`min_free_disk_mb`, default 2048) that removes the oldest segments early when
the counsellor's own disk fills. Only clips someone explicitly asked for reach
the backend, where they live for `JOB_RETENTION_DAYS` (30). So audio nobody
requested within ~72 hours is gone permanently — that window, not storage cost,
is the binding constraint on the whole product.

**Retrieval is outbound.** Laptops sit behind NAT with no stable address, so the
backend never connects inward: it queues a job and the laptop collects it on its
next long poll, then uploads the clip. Routing comes from enrolment — the laptop
reports its employee ID when it registers. The PRD's inbound
`POST /recordings/fetch` still exists, on loopback, as the support tool for
telling recording problems apart from delivery problems.

### Repo layout

```
agent/                 -> Windows laptop      backend/          -> Docker
  ongoingrec/                                   backend/          deployed service
  installer/    PyInstaller + Inno Setup        mockbackend/      reference impl
  tools/        fetch_clip.py                   Dockerfile        context = backend/
  tests/                                        docker-compose.yml
  pyproject.toml                                tests/
                                                pyproject.toml
docs/  backend-api.yaml (shared contract) · windows-setup.md · deploy-backend.md
pytest.ini   runs both suites from the root
```

### Agent module map (`agent/ongoingrec/`)

| File | Responsibility |
|---|---|
| `config.py` | identity + settings; `Config` / `Secrets`; DPAPI-encrypted secrets on Windows |
| `timeutil.py` | UTC discipline, monotonic clock, segment-boundary maths |
| `index.py` | SQLite `segments` and `jobs` tables; `Database` |
| `segments.py` | `Recorder`: rotation, sample-exact buffer splitting, gap fill, crash recovery |
| `extract.py` | `extract_clip()` — timestamp → MP3, including boundary crossing and gap padding |
| `retention.py` | age limit + free-disk floor |
| `audio/` | `devices.py` (discovery), `capture.py` (PortAudio + `SyntheticAudioSource`), `encoder.py` (ffmpeg), `watermark.py` (test oracle) |
| `transport/` | `client.py` (`BackendClient`), `poller.py` (`JobPoller`: register → poll → extract → upload) |
| `api/` | loopback FastAPI app: `GET /health`, `POST /recordings/fetch` |
| `service/` | `runner.py` (`ServiceRunner`, the cross-platform supervisor), `win_service.py` (thin pywin32 shell over it) |
| `__main__.py` | the `ongoingrec` CLI |

Both the poller and the loopback API call the same `extract_clip()`. There is
exactly one implementation of "timestamp to audio" — keep it that way.

### Status vocabularies

- Segments (`index.py`): `recording`, `complete`, `truncated`, `missing`
- Agent jobs (`index.py`): `pending`, `extracting`, `uploading`, `done`, `failed`
- Backend jobs (`backend/backend/store.py`): `queued`, `delivered`, `complete`, `failed`
- Backend sessions (`backend/backend/store.py`): stored as `active` / `completed` /
  `cancelled`; an `active` row reads as `scheduled` or `recording` depending on
  whether its start has passed — derived on read so nothing has to sweep the table

### Backend (`backend/backend/`)

`app.py` (routes), `store.py` (SQLite devices + jobs + clip files), `settings.py`
(env vars only). Agent-facing: `/devices/register`, `/devices/heartbeat`,
`/jobs/poll`, `/jobs/{id}/clip`, `/jobs/{id}/error`. Admin-facing (all under
`/admin`): `request-clip`, `recordings/fetch` (GET and POST — the one-step
identifier+timestamp → audio call that hides the polling loop; 202 if the laptop
hasn't delivered within `wait_seconds`), `sessions/start`, `sessions/end`,
`sessions/cancel`, `sessions`, `jobs`, `jobs/{id}`, `jobs/{id}/clip`, `devices`.
Plus `GET /healthz`.

**Sessions are markers, not recorder control.** The mic never starts or stops;
a session only records which stretch an operator intends to collect, which is
why cancelling costs nothing and a start time may be in the future. One open
session per employee, enforced by a partial unique index rather than a
check-then-insert — two concurrent starts would both pass a check. Ending
converts the pair into the one thing the agent understands: `extract_clip`
centres its window on a timestamp, so a start/end pair travels as *midpoint +
length*, and the midpoint keeps fractional seconds because an odd-length window
lands on a half second that whole-second formatting would silently drop.
Session times are the one place in this system that does **not** take an ISO
instant: they arrive as a separate IST **date and clock time**, all four fields
required, nothing defaulting to "now". That is the shape an operator reads them
in, and it removes the two silent failures a combined field invites — an
omitted time meaning midnight, and a reordered date parsing as a different
valid date. `IST` is a fixed `+05:30` rather than a `ZoneInfo` lookup: India
has no DST, and a fixed offset needs no `tzdata` package, which Windows does
not ship and this backend runs there during testing. Responses echo the IST
pair back alongside the resolved `*_utc`, since UTC is what every other
endpoint, header and log line speaks. A session is capped at **8 hours**: at 32 kbps
that is ~110 MB, comfortably inside the 128 MB `MAX_CLIP_MB` upload limit that
one clip has to pass through. Refused up front rather than after the laptop has
spent minutes encoding something it cannot send.

Two credentials, doing different jobs:
- `ENROLLMENT_KEY` — fleet-wide, ships in the installer, only accepted by
  `/devices/register`. Assume it leaks; it grants nothing but the right to enrol.
- `ADMIN_API_KEY` — guards every `/admin` route, including read-only ones.

**Both are optional and the current deployment runs open by choice** — the URL is
the secret. A set-but-short (<16 char) key is rejected at startup. Turning either
on later needs no code change.

`docs/backend-api.yaml` is the contract the backend team implements; changes to
agent↔backend HTTP must land there, in `backend/backend/app.py`, and in
`backend/mockbackend/` together.

The Docker build context is `backend/`, not the repo root — a hosted platform
building the Dockerfile needs its base directory set to `/backend`.

---

## Commands

```bash
# Both projects into one venv: the agent's transport tests import mockbackend.
python -m venv .venv
.venv/bin/pip install -e "./agent[dev]" -e "./backend[dev]"   # add "windows" extra on Windows

.venv/bin/pytest -q                  # both suites, 242 tests, ~70s
.venv/bin/pytest -q agent/tests      # or one side at a time
.venv/bin/pytest -q backend/tests

.venv/bin/ongoingrec selftest --minutes 45   # whole pipeline, no microphone needed
.venv/bin/ongoingrec configure --email you@pw.live --employee-id EMP001 \
    --backend-url http://127.0.0.1:9000 --enrollment-key dev-enrollment-key
.venv/bin/ongoingrec run                     # foreground service
.venv/bin/ongoingrec status | devices | fetch <timestamp>

.venv/bin/python -m mockbackend              # reference backend on :9000
python agent/tools/fetch_clip.py <timestamp> --employee-id PW33744   # laptop's loopback API
docker build -t ongoingrec-backend backend/                         # context is backend/

# The real backend. DATA_DIR is worth setting explicitly: it defaults to /data,
# which is right in the container and a surprise anywhere else. HOST=0.0.0.0 is
# already the default, and is what makes it reachable from another machine.
DATA_DIR=./backend/data PORT=8010 .venv/bin/python -m backend
```

Everything runs on one machine when testing, agent included — point the agent at
`http://127.0.0.1:<port>` and no networking is involved. The backend is pure
Python (no PortAudio, no POSIX-only calls), so that works on Windows too, which
is the simplest way to exercise the whole product without a second host.
`GET /docs` serves an interactive page for every route.

`ONGOINGREC_HOME` overrides the install root (`%ProgramData%\PW\OngoingRec` on
Windows, `~/.ongoingrec` elsewhere). `ONGOINGREC_FFMPEG` / `ONGOINGREC_FFPROBE`
point at ffmpeg when it is not on PATH.

Windows packaging: `agent/installer/build.ps1`, run from `agent/` (PyInstaller +
Inno Setup, must run on Windows; bundles ffmpeg because LocalSystem's PATH is
not the counsellor's).

---

## Testing

The tests do not check file sizes or durations — those would pass while the
service handed back the wrong half hour. Instead **the test audio carries the
time inside it**: `audio/watermark.py` encodes each second's wall-clock value as
a tone frequency, and tests decode a returned clip second by second and assert
the recovered timestamps match the requested window. That is the oracle for
alignment across a 30-minute boundary, across a gap, and after a clock jump. It
survives 32 kbps MP3 with zero errors.

- `agent/tests/helpers.py::record_synthetic` drives the **real** `Recorder` against a
  `SyntheticAudioSource`, so segmentation, splitting, encoding and indexing are
  genuinely exercised; only the microphone is substituted.
- `agent/tests/conftest.py` records a 90-minute corpus once per session and copies it per
  test (`recorded` fixture), pins `TZ=Asia/Kolkata`, and isolates
  `ONGOINGREC_HOME` under `tmp_path`.
- ffmpeg must be findable or roughly a third of the tests fail — with a captured
  log line rather than an assertion, which is easy to misread.

When changing anything that touches alignment, add a watermark-decoding
assertion. A test that only checks the clip exists is not a test of this system.

---

## Conventions

- Module and function docstrings explain **why**, not what — they carry the
  reasoning behind non-obvious choices (CBR, UTC anchoring, index-on-open).
  Match that density when adding code; do not strip the rationale.
- `from __future__ import annotations` at the top of every module; modern typing
  (`str | None`), dataclasses for records.
- Errors are typed per layer: `ConfigError`, `CaptureError`, `EncoderError`,
  `ExtractionError` / `NoRecordingError`, `BackendError` / `AuthError`,
  `SettingsError`. Distinguishable failures get distinguishable exceptions and,
  in `agent/tools/fetch_clip.py`, distinct exit codes.
- Never commit `config.json` or `secrets.bin` (device token + enrolment key) —
  both are gitignored. `.claude/` is gitignored too.
- Root `pytest.ini` uses `--import-mode=importlib`; without it the two
  directories named `tests` collide and only the first is collected.
- **`created_at` / `registered_at` are second-resolution strings, so they tie.**
  Any `ORDER BY` on them needs `, rowid DESC` as a tiebreaker or the winner is
  whatever SQLite happened to return. This has already caused two real bugs —
  `device_by_identifier` picking the wrong laptop after a same-second re-enrol,
  and session listings coming back in arbitrary order.

## Out of scope (per PRD §29)

No cloud storage, transcription, diarization, analytics, dashboards, desktop UI,
or separate device ID. Also not built: recording indicator, pause controls, admin
console.

**Session-0 capture: answered, on one machine.** `docs/windows-setup.md` §1 was
run on Windows 11 Pro 26200 with Realtek onboard audio and a LocalSystem
service captured real audio (`mean_volume: -38.2 dB`, not silence). So the
architecture holds. It is one configuration, not the fleet — re-run the spike
on the actual counsellor image before rollout, and if some machine says no,
`Config.capture_in_session_agent` switches the service to supervising a capture
agent in the interactive session instead (that path is designed but not built).

**Still unresolved:** continuous employee recording carries notice-and-consent
obligations that vary by jurisdiction, and this service has no on-screen
indicator. HR/legal sign-off is a prerequisite for rollout, not a follow-up.
The deployed backend also runs with no authentication and no audit trail, so
anyone with the URL can fetch any counsellor's audio and nothing records who
did — a deliberate trade for shipping quickly, not a posture to keep once real
conversations flow through it.
