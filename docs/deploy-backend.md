# Deploying the backend

What gets deployed is the **backend only** — the thing counsellor laptops talk
to. The recording agent is a Windows service and stays on the laptop; nothing
about it is containerised.

Once this is up you get a stable HTTPS URL. Anyone who has that URL can fetch
any enrolled counsellor's audio by employee ID and timestamp, from any machine.

> **The URL is the only thing protecting the recordings.** This deployment runs
> without credentials by choice, so treat the URL like a password: don't put it
> in a public repo, a ticket, or a group chat you wouldn't paste a password
> into. Section 6 covers turning authentication on later — it needs no code
> change.

---

## 1. Run the container

The backend is a plain Docker image with one volume and no required
configuration. Anything that runs a container will do -- `docker compose` on a
VPS, a managed container platform, systemd with `podman`. What matters is the
five things below, not which tool applies them.

**The build context is `backend/`, not the repository root**, because the repo
also holds the agent and the image deliberately does not install its audio
dependencies. On a PaaS this is usually called *Base Directory*; getting it
wrong is the one mistake here that fails the build outright rather than
quietly.

```bash
docker compose -f backend/docker-compose.yml up -d
```

or by hand:

```bash
docker build -t ongoingrec-backend backend/
docker run -d --name ongoingrec-backend \
  -p 8000:8000 \
  -v ongoingrec-data:/data \
  ongoingrec-backend
```

What a hosted platform needs to be told, if you are not using compose:

| Setting | Value | Why |
|---|---|---|
| Build context / base directory | `backend/` | the `Dockerfile` is not at the repo root |
| Port | `8000` | |
| Volume | mounted at `/data` | **do not skip** -- see below |
| Health check path | `/healthz` | carries no counsellor data, safe to poll |
| `DATA_DIR` | `/data` | where the database and clips live |
| `JOB_RETENTION_DAYS` | `30` | clips and finished jobs are deleted after this |
| `MAX_CLIP_MB` | `128` | largest clip a laptop may upload |

The environment variables are all optional and already default to these values.
The volume is not optional: without it, every redeploy starts with an empty
device table. Laptops still hold tokens the new container has never seen, so
clip delivery stops even though recording carries on perfectly -- the most
confusing possible failure. Section 5 covers how laptops recover if it happens
anyway.

Put TLS in front of it -- a reverse proxy, or whatever your platform issues
certificates with -- and use the `https://` URL everywhere from here on.

Deploy, then confirm:

```bash
curl https://your-domain/healthz
# {"status":"ok","devices":0,"jobs":0,"pending_jobs":0}
```

---

## 2. Point a laptop at it

On the counsellor's laptop, in an **administrator** PowerShell:

```powershell
Stop-Service OngoingRec

& "C:\src\PW\agent\.venv\Scripts\python.exe" -m ongoingrec configure `
    --email counsellor@pw.live --employee-id PW33744 `
    --backend-url https://your-domain `
    --enrollment-key open --force

Start-Service OngoingRec
```

`--enrollment-key open` is a placeholder whose only job is to clear the stored
device token so the laptop enrols fresh against the new backend. The value is
ignored while the backend accepts open registration, and the enrolment happens
on the next poll — within about a minute.

Confirm from anywhere:

```bash
curl https://your-domain/admin/devices
```

You want `"recording": true` in `last_heartbeat`, and a `received_at` within
the last five minutes.

### Let laptops re-enrol on their own

Worth setting `"retain_enrollment_key": true` in the laptop's
`C:\ProgramData\PW\OngoingRec\config.json` if you later turn enrolment gating
on. While registration is open it makes no difference — the agent throws away a
rejected token and re-enrols regardless, so a laptop recovers by itself from a
backend that has forgotten it.

---

## 3. Hand these to whoever is integrating

Base URL: `https://your-domain`. No headers, no keys.

### The one call they need

Employee ID (or email) plus a timestamp, and the MP3 comes straight back:

```bash
curl "https://your-domain/admin/recordings/fetch?employee_id=PW33744&timestamp=2026-08-17T19:06:52&window_seconds=60" \
  --output clip.mp3
```

Same thing as a POST, if that suits the client better:

```bash
curl -X POST https://your-domain/admin/recordings/fetch \
  -H 'Content-Type: application/json' \
  -d '{"employee_id":"PW33744","timestamp":"2026-08-17T19:06:52","window_seconds":60}' \
  --output clip.mp3
```

The backend queues the job, waits for the laptop to deliver, and streams the
audio back — typically within a second or two of the laptop picking it up.

| Response | Meaning |
|---|---|
| `200` + `audio/mpeg` | the clip, with `X-OngoingRec-*` headers describing what it really covers |
| `202` + JSON | the laptop has not answered within `wait_seconds` — it is probably switched off. The job is **still queued**; collect it later from `/admin/jobs/{job_id}/clip` |
| `404` | unknown identifier, or the laptop reported no audio for that moment |
| `502` | the laptop has the audio but could not render the clip |

`wait_seconds` defaults to 120 and caps at 600.

### Sessions: mark a window, collect its audio

Start and end are given as an **IST date and clock time**, in separate fields.
All four are required — nothing defaults to "now".

```bash
# 1. mark where it began
curl -X POST https://your-domain/admin/sessions/start \
  -H 'Content-Type: application/json' \
  -d '{"employee_id":"PW33744","start_date":"2026-08-17","start_time":"14:30:00"}'
```
```json
{"session_id":"ses-c400fd672649","status":"recording",
 "start_date":"2026-08-17","start_time":"14:30:00","start_utc":"2026-08-17T09:00:00Z",
 "end_date":null,"end_time":null,"end_utc":null,"job_id":null}
```

```bash
# 2. mark where it ended -- the MP3 comes straight back
curl -X POST https://your-domain/admin/sessions/end \
  -H 'Content-Type: application/json' \
  -d '{"employee_id":"PW33744","end_date":"2026-08-17","end_time":"15:30:00"}' \
  --output clip.mp3

# 3. or throw the marker away without collecting anything
curl -X POST https://your-domain/admin/sessions/cancel \
  -H 'Content-Type: application/json' -d '{"employee_id":"PW33744"}'
```

Times are IST at a fixed `+05:30`, so nothing has to be inferred and nothing
carries a timezone suffix. Responses echo back the IST pair you sent *and* the
UTC instant it resolved to, because the UTC is what every other endpoint,
header and log line in this system speaks. `start_time` accepts `HH:MM:SS` or
`HH:MM`; `start_date` is `YYYY-MM-DD` and nothing else — a lenient parser would
read `17-08-2026` as some other real date and return audio from a day nobody
asked for.

**Nothing here starts or stops the microphone.** The agent records
continuously regardless; a session only marks which stretch of that recording
you intend to collect. Cancelling therefore destroys no audio, and a start may
be in the future (it reads as `scheduled` until then).

One session at a time per person. Starting a second returns `409` carrying the
first one's state, so the caller learns what is already running:

```json
{"detail": {
  "detail": "a recording is already in progress for PW33744, started at 2026-08-17 14:30:00 IST",
  "session_id": "ses-c400fd672649", "status": "recording",
  "start_date": "2026-08-17", "start_time": "14:30:00",
  "start_utc": "2026-08-17T09:00:00Z",
  "hint": "POST /admin/sessions/cancel to discard it and start a new one, or POST /admin/sessions/end to close it and get the audio"}}
```

| Response | Meaning |
|---|---|
| `201` | session opened |
| `200` + `audio/mpeg` | the session's audio, plus `X-OngoingRec-Session-Id` |
| `202` | laptop has not delivered yet; the `job_id` is in the body |
| `400` | the date/time is unparseable, or the window is unusable — see below |
| `404` | unknown identifier, nothing to cancel, or no audio for that window |
| `409` | already started, or nothing was started |
| `422` | a required field is missing |

`400` covers the window problems, each with the reason in `detail`: an end in
the future, ending a session whose start has not arrived yet (cancel it
instead), an end before the start, a window under a second, and a window over
**8 hours** — one clip's practical ceiling at 32 kbps under the 128 MB upload
limit. For anything longer, cancel and collect it in pieces with
`/admin/recordings/fetch`.

`GET /admin/sessions?employee_id=PW33744` lists recent sessions and their
states: `scheduled`, `recording`, `completed`, `cancelled`.

### The rest

| Endpoint | Purpose |
|---|---|
| `GET`/`POST` `/admin/recordings/fetch` | **one step: identifier + timestamp → MP3** |
| `POST /admin/sessions/start` | mark where the audio you want begins |
| `POST /admin/sessions/end` | close it and get the MP3 |
| `POST /admin/sessions/cancel` | discard the open marker |
| `GET /admin/sessions` | recent sessions and their states |
| `GET /admin/devices` | which laptops are enrolled and recording |
| `POST /admin/request-clip` | queue without waiting, returns `job_id` |
| `GET /admin/jobs/{job_id}` | `queued` → `delivered` → `complete` / `failed` |
| `GET /admin/jobs/{job_id}/clip` | the MP3 for an earlier job |
| `GET /admin/jobs?limit=50` | recent jobs |

Two things worth telling them explicitly:

- **Read the response headers.** `X-OngoingRec-Clip-Start` / `-Clip-End` say
  which window actually came back, and `X-OngoingRec-Gaps` lists stretches the
  laptop never recorded — padded with silence so later audio keeps its true
  offset. Silence with an empty gap list is a quiet room; silence with a gap
  entry is audio that never existed.
- **A switched-off laptop is not an error.** The `202` is not a failure — the
  request survives and is fulfilled whenever the laptop comes back.

---

## 4. Operating it

**Asking for a moment that just happened.** The agent will not cut a clip until
the whole window has elapsed and the recorder has flushed that far — roughly 45
seconds past the end of the window. A one-step fetch that takes a minute to
return, or a job sitting at `delivered`, is correct rather than stuck.

**Proxy timeouts on the one-step fetch.** It holds the HTTP request open while
the laptop works. If a reverse proxy in front of it closes long requests before
`wait_seconds`, either raise the proxy timeout or pass a smaller
`wait_seconds` and fall back to collecting the `202` job id — the queued work
survives either way.

**Backups.** Everything lives in the `/data` volume: `backend.db` and
`clips/`. Back up the volume, or accept that losing it means every laptop
re-enrols.

**If the volume is lost anyway.** Laptops notice their token was rejected,
discard it and enrol again by themselves, usually within a poll cycle.

**Audio lifetime.** Two independent limits: the laptop keeps 72 hours of
recording, and the backend deletes delivered clips after `JOB_RETENTION_DAYS`.
Ask for audio before the first one expires.

---

## 5. Turning authentication on later

Nothing in the code needs to change. Set either variable in the container's
environment and redeploy:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

| Variable | Effect once set |
|---|---|
| `ADMIN_API_KEY` | every `/admin` call needs `X-Admin-Key: <key>` (or `Authorization: Bearer <key>`) |
| `ENROLLMENT_KEY` | a laptop must present this key to enrol; already-enrolled laptops keep working |

Both are rejected if shorter than 16 characters — a guessable key looks like
protection while providing almost none, which is worse than running open on
purpose.

Setting `ADMIN_API_KEY` breaks existing callers until they add the header, so
tell whoever is integrating before you do it.

---

## 6. Before this carries real recordings

- **There is no access control and no audit trail.** Anyone with the URL can
  fetch any counsellor's audio, and the server has no record of who did. That
  is a deliberate trade for getting this working quickly; it is not a posture
  to keep once real conversations are flowing through it.
- **A URL leaks more easily than a password.** It ends up in browser history,
  shell history, screenshots, and forwarded messages. Section 5 is the fix.
- **Continuous recording of employees carries notice-and-consent obligations**
  that vary by jurisdiction, and the agent has no on-screen recording
  indicator. Worth settling the HR and legal position before rollout.
