# ClipForge AI

Self-hosted AI clipper: upload a long video (or paste a link), get captioned, vertical, ready-to-post short clips. No watermarks except your own, no limits except your hardware, every file in open formats.

## What it does

1. **Ingest**: upload a file (video or audio) or paste a link (YouTube, Vimeo, X, Twitch VODs and ~1800 other sites via yt-dlp).
2. **Transcribe**:
   - **Links**: uses the platform's own speech-recognition captions with per-word timing when available (YouTube: an hour of video in ~10 seconds), rebuilt into phrases at natural speech pauses. Falls back to local transcription automatically. Choose "Always transcribe on this computer" for maximum accuracy or translation.
   - **Uploads**: local faster-whisper in batched mode (99 languages, word timestamps, memory-bounded for multi-hour files). Optional translation of captions to English.
3. **Find highlights** with one of:
   - **Claude** (cloud, best picks): set `ANTHROPIC_API_KEY`. Uses `claude-opus-5-5` with structured JSON output.
   - **Ollama** (fully local LLM, free, no API key): install [Ollama](https://ollama.com), run `ollama pull qwen2.5:3b`.
   - **Built-in heuristic** (no AI, no network): speech density, loudness and hook phrases.
   The default (`FC_AI_PROVIDER=auto`) uses Claude if a key is set, otherwise Ollama if it is running with the configured model, otherwise the heuristic.
   If an AI provider fails, the job falls back to the heuristic instead of failing.
4. **Render** each clip with ffmpeg: 9:16 / 1:1 / 4:5 / 16:9, face-following crop (or center, blurred background, black bars), burned-in animated captions with active-word highlight, loudness normalization, optional logo watermark. Audio-only uploads become branded waveform videos.
5. **Review and edit** in the web UI: play clips, change start/end, title, hashtags and caption style, re-render, add custom clips from the transcript, re-run highlight detection with new settings (transcript is reused).

## Outputs (open standards)

Per clip: `clip.mp4` (H.264/AAC), `thumb.jpg`, `captions.srt`, `captions.vtt`, `captions.ass`, `meta.json`.
Per project: full transcript as SRT, WebVTT, TXT and JSON, the original source, and a ZIP of everything with a `clips.json` index.
Everything lives under `data/jobs/<project-id>/` on your disk.

## Run on Windows / macOS / Linux

Needs Python 3.11+ and ffmpeg on PATH.

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python -m pip install -r requirements.txt
```

(macOS/Linux: `.venv/bin/python -m pip install -r requirements.txt`)

```bash
.venv\Scripts\python run.py
```

Open http://127.0.0.1:8000 and create the admin account. The first run downloads the chosen Whisper model.

Copy `.env.example` to `.env` to change settings (AI provider, capacity, models, port, storage folder).

## Run with Docker

```bash
docker compose up -d --build
```

Open http://127.0.0.1:8000. Data is kept in the Docker volume `clipforge-data` (survives restarts and rebuilds). For a fully local AI highlight finder: `docker compose --profile ollama up -d`, then `docker compose exec ollama ollama pull qwen2.5:3b`, and set `FC_OLLAMA_URL=http://ollama:11434` in `.env`.

## Deploy free on Oracle Cloud (Always Free)

Oracle's Always Free tier includes Arm compute equal to 2 OCPUs and 12 GB of memory, plus 200 GB of disk, free for the life of the account (per Oracle's Always Free resources page). That is enough to run ClipForge AI with the free local AI model, always on, with HTTPS.

1. **Create an account** at oracle.com/cloud/free. Oracle asks for a card to verify your identity; Always Free resources are not charged.
2. **Create the server**: Compute, then Instances, then Create instance.
   - Image: **Canonical Ubuntu 24.04** (or 22.04).
   - Shape: Change shape, then **Ampere**, then `VM.Standard.A1.Flex` with **2 OCPUs and 12 GB memory**.
   - Networking: keep "Assign a public IPv4 address" on.
   - SSH keys: let Oracle generate a key pair and **download the private key**.
   - Boot volume: 50 GB or more (up to 200 GB is free in total).
   - If you see "Out of host capacity", try another availability domain or try again later.
3. **Open the web ports**: on the instance page open the subnet, then its Security List, then Add Ingress Rules: source `0.0.0.0/0`, TCP, destination port `80`; add a second rule for port `443`.
4. **Connect** (Windows PowerShell, macOS or Linux terminal), using the server's public IP:
   ```bash
   ssh -i path/to/your-private-key.key ubuntu@YOUR_SERVER_IP
   ```
5. **Install ClipForge AI** on the server:
   ```bash
   curl -fsSLO https://raw.githubusercontent.com/sizwendlovu01/Free-Clipper/main/deploy/oracle-setup.sh
   ```
   ```bash
   bash oracle-setup.sh
   ```
   It installs Docker, opens the server firewall, builds the app, downloads the free local AI model, sets up HTTPS and prints your address (like `https://203-0-113-7.sslip.io`, a free hostname for your IP) and a **setup code**. The first run takes 10-20 minutes.
6. **Open the address**, create your admin account with the setup code, and add users for your clients in Settings.

Updating to the latest version: run `bash oracle-setup.sh` again (data and settings are kept). Own domain: point its DNS A record at the server, then `bash oracle-setup.sh --domain clips.example.com`. Claude for better picks: put `ANTHROPIC_API_KEY=...` in `~/Free-Clipper/.env` and re-run the script.

Good to know: Oracle may reclaim Always Free servers that sit idle for 7 days (CPU, network and memory all under 20%). The free server has no GPU and 2 CPUs, so transcribing uploaded videos is slow (expect it to take longer than the video itself); YouTube links stay fast because they use platform captions.

## Deploy to another container host

ClipForge AI runs as one Docker container that needs a long-running process, ffmpeg and a persistent disk. Any container host works (Render, Railway, Fly.io, a VPS with Docker). The container:
- listens on `$PORT` if the platform sets it (otherwise 8000);
- answers health checks at `/healthz` (checks the database and ffmpeg);
- keeps videos, clips, models and the database in `/data`, so **mount a persistent volume at `/data`**, or everything is lost on each redeploy.

Example: Render (dashboard)
1. New, then Web Service, then connect this GitHub repo. Render detects the `Dockerfile`.
2. Instance: at least 2 GB RAM for transcription (persistent disks need a paid instance on Render).
3. Advanced: add a Disk with mount path `/data` (10 GB or more).
4. Health check path: `/healthz`.
5. Environment variables: `PORT=8000`, `FC_HTTPS_ONLY_COOKIES=true`, and optionally `ANTHROPIC_API_KEY` (Claude highlight picking; without it the built-in finder is used, since Ollama is not available on most hosts). Set `FC_SECRET_KEY` to a long random string so sign-ins survive restarts, and `FC_SETUP_TOKEN` to a code only you know.
6. Deploy, open the URL and create the admin account, entering your setup code. Without `FC_SETUP_TOKEN`, whoever opens a fresh deployment first could claim the admin account.

Hosts without GPUs transcribe uploads slowly (see Speed and capacity); YouTube links stay fast because they use platform captions.

### Why not Vercel?

Vercel runs short serverless functions: request bodies are capped at 4.5 MB (video uploads cannot pass), each request ends after a few minutes with no background work afterwards, there is no ffmpeg and no persistent disk. The processing pipeline cannot run there. Use a container host as above.

## Speed and capacity

How a job runs, fastest path first:
- **Links**: captions, highlight picking and the video download run at the same time, then clips render in parallel. Measured on a busy 15 W laptop (i5-1235U, no GPU): a 1-hour YouTube podcast went from link to 4 finished clips in **4.6 minutes**, download included.
- **Uploads**: local transcription is the slow step on CPU (about 2.3x faster than real time with `small` on that laptop, so roughly 25 minutes per hour of video). Use `base` for speed, or a machine with an NVIDIA GPU (used automatically) for large models.
- **Local AI** (Ollama) reviews a shortlist of the strongest moments rather than the whole transcript, so its time stays about 2 minutes regardless of video length.

Settings: `FC_MAX_CONCURRENT_JOBS` (projects at once), `FC_RENDER_WORKERS` (clips rendered at once per project), `FC_WHISPER_BATCH_SIZE` (lower it if transcription runs out of memory).

## Branding and templates

- **Settings** (admin): app name, accent color, logo, logo watermark on clips (position, size, opacity), custom caption fonts (.ttf/.otf), users.
- **Caption styles**: font, size, colors, outline, shadow, background box, uppercase, words per caption, position, active-word highlight, pop animation, with a live preview. Three built-in styles included.

## API

Every action in the UI is a JSON API. Create a token in Settings and send `Authorization: Bearer <token>`. Interactive docs at `/api/docs`.

```bash
curl -H "Authorization: Bearer $TOKEN" -F "url=https://www.youtube.com/watch?v=..." -F 'options={"max_clips":5,"aspect":"9:16"}' http://127.0.0.1:8000/api/jobs
```

## Security notes

- Passwords hashed with scrypt; login attempts throttled per IP; sessions are signed cookies (SameSite=Lax) and cross-site write requests are rejected.
- Sign-up is off by default; the first account is the admin, who adds users.
- Behind HTTPS (reverse proxy), set `FC_HTTPS_ONLY_COOKIES=true`. Bind to `0.0.0.0` only when you intend to expose it.
- Only download content you have the rights to use.

## Tip: keep data out of cloud-synced folders

If the project sits in OneDrive/Dropbox, the sync client will re-upload every video and render, slowing everything down. Point `FC_DATA_DIR` at a local, unsynced folder (e.g. `C:\ClipForgeData`).
