# Deploying the backend to Coolify

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

## 1. Create the application in Coolify

1. **New Resource → Application**, pointed at this repository.
2. **Build Pack: Dockerfile.** The `Dockerfile` at the repo root is the backend
   image; it deliberately does not install the agent's audio dependencies.
3. **Port: `8000`.**
4. **Environment variables** — only these, and all are optional:

   | Variable | Value | Why |
   |---|---|---|
   | `DATA_DIR` | `/data` | where the database and clips live |
   | `JOB_RETENTION_DAYS` | `30` | clips and finished jobs are deleted after this |
   | `MAX_CLIP_MB` | `128` | largest clip a laptop may upload |

5. **Persistent storage — do not skip this.** Add a volume mounted at `/data`.

   Without it, every redeploy starts with an empty device table. Laptops still
   hold tokens the new container has never seen, so clip delivery stops even
   though recording carries on perfectly — the most confusing possible failure.
   Section 5 covers how laptops recover if this happens anyway.

6. **Health check path: `/healthz`.** Carries no counsellor data, so it is safe
   for the platform to poll.
7. **Assign a domain.** Coolify issues the TLS certificate. Use the `https://`
   URL everywhere from here on.

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

& "C:\src\PW\.venv\Scripts\python.exe" -m ongoingrec configure `
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

### The rest

| Endpoint | Purpose |
|---|---|
| `GET`/`POST` `/admin/recordings/fetch` | **one step: identifier + timestamp → MP3** |
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
the laptop works. If Coolify's proxy closes long requests before
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

Nothing in the code needs to change. Set either variable in Coolify and
redeploy:

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
