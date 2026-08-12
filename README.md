# PW OngoingRec

A Windows background service that records counsellor microphone audio
continuously in 30-minute timestamped segments, and hands the central backend
an audio clip for any requested moment.

Built from the PRD (v5.0). Two things in it were deliberately changed, both
explained below.

---

## What it does

```
Windows laptop                                        Central backend
┌────────────────────────────────────────────┐
│ OngoingRec service (LocalSystem, auto)     │
│                                            │
│  microphone ─► ffmpeg ─► 30-minute MP3s    │
│                    │                       │
│                 SQLite index               │
│                    │                       │
│  extractor ◄───────┘                       │
│     ▲    │                                 │
│     │    └─ clip + metadata ───────────────┼──► POST /jobs/{id}/clip
│  poller ◄──────── job ─────────────────────┼─── GET  /jobs/poll
│                                            │──► POST /devices/register
│  loopback API 127.0.0.1:8765               │──► POST /devices/heartbeat
│   GET /health · POST /recordings/fetch     │
└────────────────────────────────────────────┘
```

The counsellor is asked for an Email ID and Employee ID **once**, by the
installer. After that the service starts with Windows, records automatically,
and never prompts again.

---

## Two departures from the PRD

**Retrieval is outbound, not inbound.** The PRD has the backend POST to the
laptop. That cannot work in production: counsellor laptops sit behind NAT on
networks the backend cannot route into and have no stable address. Instead
each laptop registers itself, long-polls for jobs, and uploads the clip. This
also supplies the routing section 23 assumes but never specifies — the backend
knows which employee ID belongs to which laptop because the laptop said so
when it enrolled. The PRD's `POST /recordings/fetch` is still implemented, on
loopback, and is the fastest way to tell during support whether a problem is
in recording or in delivery.

**Gaps are padded with silence and reported.** When the laptop was asleep or
the microphone was unplugged, no audio exists. Concatenating whatever remains
would silently compress the timeline — 11:24 would run straight into 11:31
with nothing to indicate six minutes were missing, and anyone reasoning about
*when* something was said would be wrong. Internal gaps are therefore filled,
so an offset within a clip always equals real elapsed time, and the gap list
travels with the audio.

Beyond the PRD: 72-hour retention with a free-disk floor (a 24/7 recorder
otherwise fills the disk), and a bearer-token enrolment scheme.

---

## Trying it out

Needs Python 3.11+ and ffmpeg on PATH.

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Prove the whole pipeline without a microphone: records watermarked audio,
# then checks a retrieved clip decodes to the seconds that were asked for.
.venv/bin/ongoingrec selftest --minutes 45
```

The full loop against a fake backend:

```bash
export ONGOINGREC_HOME=/tmp/ongoingrec-dev
.venv/bin/python -m mockbackend &                        # on :9000

.venv/bin/ongoingrec configure \
  --email you@pw.live --employee-id EMP001 \
  --backend-url http://127.0.0.1:9000 \
  --enrollment-key dev-enrollment-key

.venv/bin/ongoingrec run &                               # records from the mic

curl -X POST http://127.0.0.1:9000/admin/request-clip \
  -H 'Content-Type: application/json' \
  -d '{"employee_id":"EMP001","timestamp":"2026-08-12T11:22:15","window_seconds":60}'
# the laptop collects the job, cuts the clip, and uploads it to ./mock-uploads/
```

Other commands: `status`, `devices`, `fetch <timestamp>`.

---

## How correctness is verified

The question that matters is not "does it record?" but "is the audio returned
for timestamp T actually the audio from T?". File sizes and durations cannot
answer that — a test checking them would pass while the service handed the
backend the wrong half hour.

So the test audio carries the time inside it. `audio/watermark.py` encodes each
second's wall-clock value as a tone frequency; tests decode a returned clip
second by second and assert the recovered timestamps match the requested
window exactly. That is what verifies alignment across a 30-minute boundary,
across a recording gap, and after a clock jump.

```bash
.venv/bin/pytest -q          # ~145 tests, about a minute
```

The watermark survives 32 kbps MP3 with zero errors, which is why it can be
trusted as the oracle.

---

## Layout

```
ongoingrec/
  config.py          identity and settings; DPAPI-encrypted secrets
  timeutil.py        UTC discipline, monotonic clock, boundary maths
  index.py           SQLite index of segments and jobs
  audio/             device discovery, capture, ffmpeg, the test watermark
  segments.py        the recorder: rotation, gaps, crash recovery
  extract.py         timestamp -> clip, including boundary crossing
  retention.py       age and free-space limits
  transport/         backend client and the outbound job poller
  api/               loopback /health and /recordings/fetch
  service/           supervisor + the Windows service wrapper
mockbackend/         runnable reference backend
installer/           PyInstaller spec and Inno Setup script
docs/
  backend-api.yaml   the contract the backend team implements
  windows-setup.md   the Windows spike, build, and acceptance walkthrough
```

---

## Design decisions worth knowing

**Elapsed time comes from sample counts, never from subtracting two clock
readings.** An NTP correction or a DST shift mid-segment would otherwise
corrupt that segment's timeline — the one thing the product depends on.

**Segment boundaries are anchored to UTC midnight.** Local alignment breaks on
a DST fall-back day, where 01:00–01:30 happens twice and collides on both the
filename and the lookup. For IST, which has no DST and a +05:30 offset, this
still lands exactly on the local `:00`/`:30` grid.

**MP3 is constant bitrate with no Xing header.** The service gets killed
abruptly all the time — shutdown, battery, forced stop — and a truncated CBR
file is still a valid sequence of frames whose duration can be recovered. The
Xing header is suppressed because writing it requires seeking back on close,
which never happens if the process is killed, leaving a header that lies.

**A segment is indexed when it opens, not when it closes.** A recording
interrupted by power loss is then still discoverable, and the startup repair
pass probes the file to recover its true length rather than writing it off.

**Recording is never blocked by anything else.** If the backend is
unreachable, the API port is taken, or retention fails, the microphone keeps
running. Audio not captured is gone forever; everything else can be retried.

---

## Deploying to Windows

See [docs/windows-setup.md](docs/windows-setup.md). Start with the session-0
microphone spike in section 1 — it is the one open question that can change
the architecture, and it needs real hardware to answer.

---

## Not built

Per PRD section 29: no cloud storage, transcription, diarization, analytics,
dashboards, desktop UI, or separate device ID. Also not built: a recording
indicator, pause controls, or an admin console.

Continuous microphone recording of employees carries notice-and-consent
obligations that vary by jurisdiction, and this service has no on-screen
indicator. Worth confirming the HR and legal position before rollout.
