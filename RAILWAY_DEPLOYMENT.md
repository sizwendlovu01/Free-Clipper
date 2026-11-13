# Deploying ClipForge AI on Railway

This guide takes the GitHub repository to a running public beta on Railway: one service, built from the
`Dockerfile`, with a persistent volume at `/data`.

Railway facts below were checked against Railway's documentation on 2026-11-13. Railway changes its
platform over time; if a dashboard label differs, the setting usually still exists under a similar name.

---

## 1. Architecture: one service

ClipForge AI runs as **one Railway service**: the web app and its background workers share one process.

| Option | Verdict |
|---|---|
| One service (web + workers) with a volume | **Use this.** SQLite and all video files live on one volume. |
| Separate web and worker services | Not possible with this design: a Railway volume attaches to **one** service only, and both parts need the database and the video files. It would require PostgreSQL plus object storage (S3-style), i.e. new paid services and a rewrite. Revisit when one server is no longer enough. |
| Several replicas | Not possible: Railway does not allow replicas on a service with a volume. |

Inside the one service, `MAX_CONCURRENT_JOBS` worker threads take jobs from a queue stored in SQLite. Claiming is a
single atomic database update, so a job can never be processed twice.

Why SQLite is fine for the beta: the app does a few writes per second at most (job progress), which SQLite in WAL mode
handles easily. The data is backed up by Railway volume backups and the built-in backup download. Move to
PostgreSQL only if you need several app servers.

## 2. What the repository contains for Railway

| File | Purpose |
|---|---|
| `Dockerfile` | Production image: Python 3.13 (Debian 13, pinned by digest), ffmpeg with libass, Noto fonts, all Python packages pinned via `constraints.txt`, face-detection model baked in, health check. Railway builds it automatically. |
| `docker-entrypoint.sh` | Railway mounts volumes as root. The container starts as root only to make `/data` writable, then runs the app as the unprivileged `clipper` user (uid 1000). You do **not** need `RAILWAY_RUN_UID=0`. |
| `constraints.txt` | Exact versions of all 50 Python packages, so every build installs what was tested. |
| `.dockerignore` | Keeps `.env`, local data, the virtualenv and docs out of the image. |
| `tests/e2e_smoke.py` | End-to-end test you can run against the live deployment. |

There is intentionally **no `railway.json`**: Railway has deprecated "config as code" and new services can't use it.
Railway always builds with the `Dockerfile` when it finds one; the few settings below go in the dashboard.

## 3. Deploy step by step

### 3.1 Push to GitHub
The repository is already on GitHub (`sizwendlovu01/Free-Clipper`). After any local change:
```bash
git add -A
```
```bash
git commit -m "Describe the change"
```
```bash
git push origin main
```
Railway redeploys automatically on every push to `main`.

### 3.2 Create the Railway project
1. Sign in at railway.com (Hobby plan or higher; the Free/Trial plans cap memory at 0.5-1 GB and volumes at 0.5 GB, which is not enough).
2. **New Project**, then **Deploy from GitHub repo**, then pick `sizwendlovu01/Free-Clipper` (authorize the Railway GitHub app if asked).
3. Railway detects the `Dockerfile` and starts a build. The first build takes about 5-10 minutes (ffmpeg, fonts, Python packages); later builds reuse cached layers.

### 3.3 Add the persistent volume (before adding users)
1. On the project canvas, right-click (or press Cmd/Ctrl+K), choose **Volume**, and attach it to the ClipForge service.
2. **Mount path: `/data`**
3. Size: Hobby volumes start at 5 GB and can be resized later (resizing up has no downtime; shrinking is not possible). Storage costs $0.15 per GB per month.

Everything that must survive redeploys lives in `/data`:

| Path | Contents |
|---|---|
| `/data/freeclipper.db` | SQLite database (users, projects, clips, settings). WAL files sit next to it. |
| `/data/jobs/<project-id>/` | Source video, transcript (JSON/SRT/VTT), and `clips/<clip-id>/` with clip.mp4, thumb.jpg, captions.srt/.vtt/.ass, meta.json |
| `/data/models/` | Whisper models (downloaded once, about 75 MB for `base`, 480 MB for `small`) |
| `/data/branding/`, `/data/fonts/` | Your logo and caption fonts |
| `/data/secret_key` | Only if `SECRET_KEY` is not set (set it; see below) |

Temporary files (upload spooling, ZIP exports) use the container's own disk (`TEMP_DIR`, default `/tmp/clipforge`), not the volume, and are cleaned automatically.

### 3.4 Set the variables
Service, then **Variables**, then **Raw Editor**, and paste (replace the placeholders):

```
SECRET_KEY=<64 random hex characters>
SETUP_TOKEN=<a code only you know>
SESSION_HTTPS_ONLY=true
CLIENT_IP_HEADER=X-Real-IP
ALLOW_SIGNUP=true
MAX_CONCURRENT_JOBS=1
MAX_WORKERS=1
MAX_UPLOAD_SIZE_MB=500
MAX_VIDEO_DURATION=1800
WHISPER_MODEL=base
WHISPER_ALLOWED_MODELS=tiny,base,small
USER_MAX_STORAGE_MB=500
SOURCE_RETENTION_DAYS=3
ANTHROPIC_API_KEY=
```

Generate the secret on any machine with Python:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

What each one does, and the full list of optional limits: see [Environment variables](#6-environment-variables).
Do **not** set `PORT`: Railway injects it and the app listens on it automatically. Do not set `STORAGE_ROOT` either:
it defaults to the volume's mount path (`RAILWAY_VOLUME_MOUNT_PATH`).

### 3.5 Health check and restarts
Service, then **Settings**:
- **Healthcheck Path:** `/health`. Railway waits for a 2xx answer before switching traffic to a new deployment (default wait 300 s). `/health` checks only the database and ffmpeg; it never starts video work. Railway calls it from `healthcheck.railway.app` and only during deployment, not continuously.
- **Restart Policy:** `On Failure` (if the process ever crashes, Railway restarts it; interrupted jobs then resume, see section 5).

### 3.6 Public domain
Service, then **Settings**, then **Networking**, then **Public Networking**, then **Generate Domain**. You get
`https://<name>.up.railway.app`. Railway terminates HTTPS. Custom domains are added in the same place.

### 3.7 First sign-in
Open the domain, create the admin account with your `SETUP_TOKEN`, then invite users or let them sign up
(`ALLOW_SIGNUP=true`). The admin is exempt from quotas.

## 4. Logs

- **Build logs:** Service, then **Deployments**, then click a deployment, then **Build Logs**. Look for `pip install` and the YuNet download step.
- **Runtime logs:** the same deployment, then **Deploy Logs**. On start the app logs its limits, storage paths and highlight provider, for example:
  `ClipForge AI ready: 1 job worker(s), 1 render worker(s), data in /data, database /data/freeclipper.db`.
  Each job logs a timing line like `job ... done in 275s {'media': 10, 'transcribe': 0, 'highlights': 120, 'render': 91}`.
- **CLI** (optional; on Windows Railway recommends WSL): `railway login`, `railway link`, `railway logs`, `railway logs --build`.

## 5. Reliability behaviour

| Situation | What happens |
|---|---|
| Redeploy or restart (SIGTERM) | New work stops, running jobs go back to the queue without counting as a failed attempt, and resume after start-up. Partial downloads are kept and resumed. There is a short downtime while a service with a volume redeploys (Railway behaviour). |
| Crash or out-of-memory kill | On start, jobs left "running" are retried. After `MAX_JOB_ATTEMPTS` (2) interrupted attempts a job is failed with a clear message, so one bad video can't crash the server forever. |
| A step hangs | Every ffmpeg call has a timeout, and each job has a total limit (`JOB_TIMEOUT_SECONDS`, 90 min). |
| Storage almost full | New videos are refused below `MIN_FREE_DISK_MB` (1 GB). |
| User cancels | The job stops at the next checkpoint; its temporary files are deleted. |
| Cleanup (every hour) | Deletes expired temp files, partial downloads, extracted audio and orphaned folders. Deletes source videos only when `SOURCE_RETENTION_DAYS` is set. **Never deletes rendered clips, captions or transcripts.** |

## 6. Environment variables

All optional except where noted. Names in brackets are older aliases that still work.

| Variable | Default | Meaning |
|---|---|---|
| `SECRET_KEY` (FC_SECRET_KEY) | generated, saved in `/data/secret_key` | **Required in production.** Signs session cookies. Changing it signs everyone out. |
| `SESSION_HTTPS_ONLY` (FC_HTTPS_ONLY_COOKIES) | false | Set `true` on Railway (always HTTPS). |
| `SETUP_TOKEN` (FC_SETUP_TOKEN) | none | Required to create the first admin account. |
| `ALLOW_SIGNUP` (FC_ALLOW_SIGNUP) | false | Let visitors create accounts. |
| `CLIENT_IP_HEADER` | none | Set `X-Real-IP` on Railway so login and sign-up limits see real client IPs. |
| `STORAGE_ROOT` (FC_DATA_DIR) | volume mount path, else `./data` | Root for all persistent data. |
| `DATABASE_PATH` | `$STORAGE_ROOT/freeclipper.db` | SQLite file. Keep it on the volume. |
| `PROJECTS_DIR` | `$STORAGE_ROOT/jobs` | Uploads, downloads and clips (one folder per project). |
| `MODEL_DIR` | `$STORAGE_ROOT/models` | Whisper models. |
| `TEMP_DIR` | `/tmp/clipforge` | Short-lived scratch files. |
| `MAX_CONCURRENT_JOBS` | 1 | Videos processed at once. |
| `MAX_WORKERS` (FC_RENDER_WORKERS) | 1 | Clips rendered in parallel per video (one ffmpeg each). |
| `FFMPEG_THREADS` | 0 (auto) | Threads per ffmpeg process. |
| `MAX_UPLOAD_SIZE_MB` (FC_MAX_UPLOAD_MB) | 500 | Upload limit (rejected before the upload is read). Railway requires uploads to finish within 5 minutes, so 500 MB needs about 2 MB/s upstream. |
| `MAX_DOWNLOAD_SIZE_MB` | 2048 | Largest video fetched from a link. |
| `MAX_VIDEO_DURATION` | 1800 | Seconds; checked before downloading. |
| `MAX_CLIPS_PER_JOB` | 10 | |
| `JOB_TIMEOUT_SECONDS` | 5400 | |
| `MAX_JOB_ATTEMPTS` | 2 | |
| `MAX_QUEUED_JOBS` | 20 | Server-wide queue; extra requests get "queue full". |
| `MIN_FREE_DISK_MB` | 1024 | |
| `USER_MAX_ACTIVE_JOBS` | 2 | Per user (admins exempt). |
| `USER_JOBS_PER_DAY` | 10 | Per user per 24 h. |
| `USER_RENDERS_PER_HOUR` | 30 | Clip edits/re-renders. |
| `USER_MAX_STORAGE_MB` | 2048 | Per user; delete projects to free space. |
| `SIGNUPS_PER_IP_PER_DAY` | 5 | |
| `CLEANUP_INTERVAL_MINUTES` | 60 | |
| `TEMP_FILE_MAX_AGE_HOURS` | 6 | |
| `SOURCE_RETENTION_DAYS` | 0 (keep) | Delete source videos after N days. Clips stay; they just can't be re-rendered. |
| `WHISPER_MODEL` | small | Default transcription model. `base` is the best fit for small servers. |
| `WHISPER_ALLOWED_MODELS` | all | Restrict choices, e.g. `tiny,base,small` (large models need several GB of RAM). |
| `WHISPER_CPU_THREADS`, `WHISPER_BATCH_SIZE` | auto | Lower `WHISPER_BATCH_SIZE` (e.g. 4) if transcription runs out of memory. |
| `PRELOAD_WHISPER` | true | Download the default model at start-up. |
| `AI_PROVIDER` | auto | `auto` uses Claude if `ANTHROPIC_API_KEY` is set, else Ollama if `OLLAMA_URL` is reachable, else the built-in scorer. An unavailable choice falls back the same way (the project shows a note). |
| `ANTHROPIC_API_KEY` | none | Optional, enables Claude. Stays server-side; never sent to browsers or logged. |
| `CLAUDE_MODEL`, `CLAUDE_EFFORT` | claude-opus-5-5, medium | |
| `OLLAMA_URL`, `OLLAMA_MODEL` | off, qwen2.5:3b | Railway doesn't provide Ollama; leave `OLLAMA_URL` unset there. |

## 7. Resources and cost

Measured on 2026-11-13 with the production image, limited to 2 GB RAM and 2 vCPUs (like a small Railway service),
`WHISPER_MODEL=base`, `MAX_CONCURRENT_JOBS=1`, `MAX_WORKERS=1`:

| Item | Measured / needed |
|---|---|
| Image size | 2.03 GB |
| Idle memory | about 114 MB |
| Peak memory during the full test run (transcription, rendering, link download) | 1,576 MB, so plan for **2 GB** |
| Memory with the `small` model | not measured in the 2 GB container; plan for **3 GB** |
| 70 s uploaded video, 2 clips | 1.5 min end to end |
| 19 s YouTube link, 1 clip | 0.6 min end to end |
| CPU | 2 vCPU minimum; more vCPU makes transcription and rendering proportionally faster |
| Volume | models 0.1-0.5 GB, plus per project roughly 5 MB per minute of source video (720p) to 8 MB/min (1080p), plus about 10 MB per 30 s clip |

Railway bills usage: about $10 per GB of RAM per month and $20 per vCPU per month (both per second of use), $0.15
per GB of volume per month, $0.05 per GB of outbound traffic. Hobby is $5/month including $5 of usage. A lightly used
beta that idles most of the time is typically within roughly $5-15 per month; heavy processing raises CPU cost.
Check your usage under Project, then **Usage**, and set a usage limit in your Railway workspace settings.

**Recommended initial Hobby configuration:** 1 service, volume 5 GB at `/data`, the variables in 3.4
(`MAX_CONCURRENT_JOBS=1`, `MAX_WORKERS=1`, `WHISPER_MODEL=base`, `SOURCE_RETENTION_DAYS=3`, `USER_MAX_STORAGE_MB=500`).
Raise limits once you see real usage in the metrics.

## 8. Testing the deployment

From your computer, with a short video that contains speech (under 2 minutes, under your upload limit):
```bash
python tests/e2e_smoke.py --base https://<your-app>.up.railway.app --video sample.mp4 --admin-user <admin> --admin-pass '<password>' --url https://www.youtube.com/watch?v=jNQXAC9IVRw
```
It checks health, sign-in/out, user isolation, an upload job, a link job, every export, failure handling, quotas,
backups and cleanup, and prints PASS/FAIL per check. It needs `httpx` (`pip install httpx`). Use a test admin
account or your own; it creates one extra normal user named `tester...`.

Manual check: upload a short video in the web UI, watch the progress, play a clip, download the ZIP.

### Test results before release (2026-11-13)

Run against the production image in Docker with a root-owned volume (as Railway mounts it), a non-default `PORT`,
2 GB RAM and 2 vCPUs:

| Test | Result |
|---|---|
| `tests/e2e_smoke.py` full suite (43 checks: health, auth, sessions, isolation, SSRF and upload validation, upload job, YouTube link job, MP4/SRT/VTT/ASS/JSON/ZIP/transcript exports, re-render, quotas, backup, cleanup) | 43/43 passed (also 43/43 on a local non-Docker run) |
| Root-owned volume made writable, app running as uid 1000 | passed |
| Graceful restart (SIGTERM) mid-job: job requeued without using an attempt, resumed, finished; same login cookie still valid | passed |
| Crash (SIGKILL) mid-job: job retried and finished | passed |
| Two crashes on the same job: job failed with an explanation instead of looping | passed |
| Cleanup: expired temp file, extracted audio and partial download removed; fresh temp file, source and all clips kept | passed |
| `SESSION_HTTPS_ONLY=true`: cookie is `Secure; HttpOnly; SameSite=Lax`, HSTS header sent | passed |

Not tested before release: an actual Railway deployment (needs your account), the Claude provider (no API key
available), and long videos on Railway hardware.

## 9. Backups and restore

**Railway volume backups (recommended):** Service, then **Backups**: enable a schedule (Daily keeps 6 days,
Weekly keeps 27 days, Monthly keeps 89 days) or create a manual backup. To restore: Backups tab, pick a backup,
**Restore**, then deploy the staged change. Restores work within the same project and environment. Deleting the
volume deletes its backups.

**Database-only backup (any time, from the browser):** as admin, Settings, then **Download database backup**
(or `GET /api/admin/backup`). This is a consistent snapshot taken while the app runs.

**Restore a database file:**
1. Stop traffic: scale the service down or note that users will be signed out briefly.
2. Upload the file to the volume, e.g. with the CLI: `railway volume files upload ./clipforge-backup.db /freeclipper.db`
   (paths are relative to the volume; or use `railway ssh` and `scp`).
3. Delete any leftover `freeclipper.db-wal` and `freeclipper.db-shm` files next to it.
4. Redeploy (Deployments, then **Redeploy**). On start the app runs an integrity check and logs a CRITICAL line if the database is damaged.

**User files** (videos and clips) are only in the volume backups, so keep the Railway backup schedule on.

## 10. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "The Dockerfile failed validation" within seconds | Railway rejects Dockerfiles that declare a `VOLUME` (fixed in this repo; storage is attached in the dashboard instead). Also make sure the service has no `RAILWAY_DOCKERFILE_PATH` variable pointing at another file such as `docker-compose.yml`. |
| Build fails at `pip install` | A package version changed upstream. The pinned `constraints.txt` prevents this; if you edited `requirements.txt`, regenerate constraints (section 11). |
| Deployment never becomes healthy | Open Deploy Logs. `ffmpeg/ffprobe not found` means the image is wrong; `PermissionError` on `/data` means the volume isn't mounted at `/data`, or `STORAGE_ROOT` points elsewhere. |
| Everyone is signed out after each deploy | `SECRET_KEY` isn't set (and there's no volume to store a generated one). Set it. |
| Sign-in doesn't stick | `SESSION_HTTPS_ONLY=true` but the site was opened over plain HTTP; use the `https://` domain. |
| Projects disappear after a redeploy | No volume, or it isn't mounted at `/data`. |
| Jobs fail with "stopped unexpectedly 2 times" | The service ran out of memory. Use `WHISPER_MODEL=base`, keep `MAX_WORKERS=1`, or give the service more memory. |
| "Storage is nearly full" | Delete projects, set `SOURCE_RETENTION_DAYS`, or resize the volume. Admin, Settings shows free space. |
| YouTube asks for a sign-in check | YouTube sometimes blocks datacenter IP addresses. Retry later or upload the file instead. |
| Uploads fail on slow connections | Railway closes uploads that take longer than 5 minutes. Lower `MAX_UPLOAD_SIZE_MB` or upload shorter files. |
| Claude not used | `ANTHROPIC_API_KEY` missing or invalid; the project page notes the fallback. |

## 11. Updating dependencies

`requirements.txt` lists direct dependencies; `constraints.txt` pins everything. After changing `requirements.txt`,
regenerate the lock inside the same base image:
```bash
docker run --rm -v "$PWD/requirements.txt:/r.txt:ro" python:3.13-slim sh -c "pip install -q -r /r.txt && pip freeze" > constraints.txt
```
Then rebuild and rerun `tests/e2e_smoke.py` before pushing.

## 12. Checklist

- [ ] Code pushed to `main` on GitHub
- [ ] Railway project created from the GitHub repo (Dockerfile build succeeded)
- [ ] Volume attached, mount path `/data`
- [ ] Variables set: `SECRET_KEY`, `SETUP_TOKEN`, `SESSION_HTTPS_ONLY=true`, `CLIENT_IP_HEADER=X-Real-IP`, limits
- [ ] Healthcheck path `/health`, restart policy On Failure
- [ ] Public domain generated
- [ ] Admin account created with the setup code
- [ ] Volume backups scheduled (Daily)
- [ ] `tests/e2e_smoke.py` passes against the live URL
- [ ] Usage limit set in Railway workspace settings
