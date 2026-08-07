# Media Downloader

A source-aware web interface for preparing one audio or video file from public,
non-DRM media. [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) is the primary
engine; a pinned [`youtube-dl`](https://github.com/ytdl-org/youtube-dl) build is
kept as a narrow fallback for direct, single-video YouTube URLs.

[GitHub Pages frontend](https://majkey25.github.io/youtube_downloader/) ·
[Read the usage policy](USAGE_POLICY.md) ·
[View yt-dlp's extractor list](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)

> The hosted instance is for personal, lawful, non-commercial use only. Submit
> media you own or are authorized to download. You remain responsible for the
> URL, the download, and its later use.

## Architecture

| Part | Host | Responsibility |
| --- | --- | --- |
| Static HTML/CSS/JavaScript | GitHub Pages | Link input, source display, playlist selection, and output controls |
| Flask API + FFmpeg | alwaysdata | Metadata inspection, extraction, conversion, temporary file delivery, and cleanup |

GitHub Pages cannot run Python, `yt-dlp`, or FFmpeg. The Pages site therefore
calls the separately hosted backend over HTTPS; it is not a client-side media
downloader.

## What it does

- Inspects a public HTTP(S) URL before offering a download.
- Detects the source and exposes only the audio/video modes found in fresh
  backend metadata. Audio-only sources never receive a video option.
- Produces exactly one selected output. Audio is converted to MP3. Video
  prefers MP4-compatible streams but reports the actual final extension when a
  source requires another container.
- Lists at most 50 playlist entries, then downloads only the one entry selected
  by the user.
- Treats an ended stream as a normal VOD. Active, upcoming, and unbounded live
  streams are blocked.
- Allows video with unknown duration to proceed under the byte, storage, and
  server-time limits. Audio conversion requires a known positive duration.
  Media with a known duration over 120 minutes is blocked in every mode.

The project pins its direct primary engine requirement to
`yt-dlp[pin]==2026.8.4.234419.dev0`. Install Deno 2.6.6 or newer on `PATH`
(the hosted service uses the verified system Deno). The upstream `pin` extra
pins the primary engine's default Python dependencies. The direct legacy
fallback requirement is pinned to `youtube-dl` commit
[`956b8c5`](https://github.com/ytdl-org/youtube-dl/commit/956b8c585591b401a543e409accb163eeaaa1193).
Transitive dependencies are resolved by `pip`; this repository does not contain
a complete dependency lock file.
Fallback is attempted only after a primary extractor/download failure for a
direct, single-video YouTube URL. It is not used for playlists, other sites,
invalid requests, unsupported output modes, or resource-limit failures.

## Common sources

Examples handled by specialized `yt-dlp` extractors include:

- YouTube and YouTube Music public videos, Shorts, playlist items, and ended
  streams;
- Twitch VODs and clips;
- public Instagram posts/Reels, TikTok, Facebook, X/Twitter, and Reddit media;
- Vimeo, Dailymotion, Rumble, Streamable, 9GAG, and Imgur;
- SoundCloud, Bandcamp, and Mixcloud audio;
- Bilibili and Kick VODs/clips;
- publicly accessible BBC, ITV, ARD, ZDF, and other broadcaster media.

This list is illustrative, not a compatibility guarantee. Sites, URL formats,
geo-restrictions, and anti-bot controls change frequently. The authoritative
upstream reference is the
[yt-dlp supported-sites list](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md),
which also warns that listed extractors can temporarily break.

This deployment intentionally differs from an unrestricted local `yt-dlp`
installation:

- the Generic extractor is disabled to reduce server-side request-forgery risk;
- only public, specialized extractors are allowed;
- every Python HTTP/WebSocket DNS result is blocked unless all resolved
  addresses are globally routable;
- the `curl-cffi` browser-impersonation transport is not installed because its
  internal redirect resolver cannot use that process guard on this host;
- cookies, account credentials, plugins, remote components, proxies, and
  user-provided extractor options are not accepted;
- Spotify URLs are rejected. Protected Spotify music is not downloadable here;
- paywalled, DRM-protected, login-only, private, or geo-blocked media is not
  bypassed.

## Hosted limits and cleanup

The configured production profile uses these hard limits:

| Limit | Production value |
| --- | ---: |
| Media with known duration | 120 minutes |
| Audio conversion | Known positive duration required |
| One final output file | 216 MiB (226492416 bytes) |
| Temporary generated-file storage | 648 MiB (679477248 bytes) |
| One helper process-group memory | 144 MiB (150994944 bytes) |
| One complete API job | 15 minutes |
| One HLS manifest | 8 MiB |
| Playlist inspection | 50 entries |
| Concurrent extraction jobs | 1 |
| Extraction operations per client | 5 per hour |
| Extraction operations across all clients | 60 per hour |
| Request body | 16 KiB |

Inspection and download are separate API operations. Each runs in an isolated
helper process. The inspection deadline covers its complete extractor work. A
download has one shared 900-second budget covering metadata inspection,
validation, the primary engine, and the legacy fallback; retries do not reset
that deadline. On the Linux host, the 144 MiB ceiling is applied to the helper's
whole process group, including FFmpeg. HLS manifests are bounded by their actual
body size even when `Content-Length` is absent, malformed, or too small.

The rolling global quota is shared by all clients in addition to the per-client
quota. These anonymous, in-memory quotas protect a small public service; they
are not authentication and cannot eliminate coordinated or botnet abuse.

Generated files are temporary:

Both downloader engines run with their persistent caches disabled, so extractor
cache data cannot grow outside the bounded download directory.

| File state | Removal target |
| --- | --- |
| Successfully fetched with `GET` | Immediately after the complete response stream finishes |
| Interrupted or crashed transfer | Atomically restored for another attempt until its normal TTL |
| Abandoned file smaller than 64 MiB | 10 minutes |
| Abandoned file at least 64 MiB | 30 minutes |
| Failed/incomplete job artifacts | Immediate best-effort cleanup on the next cleanup pass |

Before a job starts, storage reserves twice the maximum output size (432 MiB)
for download and merge work. The 648 MiB ceiling therefore lets one maximum-size
ready file remain while another job runs. If space is needed, the oldest
inactive, unlocked ready outputs can be removed before their TTL; locked
in-flight transfers are never eviction candidates. A failed removal remains
counted and can make a new job fail with insufficient storage.

Each complete `GET` consumes its ready file. An interrupted transfer or a
released claim after a process crash can be restored without overwriting an
existing file. This is retry-safe, at-least-once delivery, not exactly-once
delivery.

Cleanup runs at startup, before work, during idle health requests, and through
an alwaysdata scheduled URL request every minute. Normal idle expiry is the TTL
plus less than one scheduler interval. This is a cleanup target, not an erasure
or uptime guarantee: an outage, stopped scheduler, filesystem error, backup, or
host failure can delay removal. See the [usage policy](USAGE_POLICY.md).

## Run locally

Requirements:

- Python 3.10 or newer;
- `ffmpeg` and `ffprobe` available on `PATH`;
- Deno 2.6.6 or newer available on `PATH`.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

Open `http://localhost:8080`. A local browser request needs an allowed `Origin`;
set `ALLOWED_ORIGINS=http://localhost:8080` when serving the UI from another
local origin.

### Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DOWNLOAD_DIR` | `<repo>/downloads` | Temporary generated-file directory |
| `HOST` | `0.0.0.0` | Development server bind address |
| `PORT` | `8080` | Development server port |
| `MAX_RETRIES` | `3` | Extractor and fragment retry count |
| `SOCKET_TIMEOUT_SECONDS` | `15` | Extractor network timeout |
| `JOB_TIMEOUT_SECONDS` | `900` | Total wall-clock budget for one helper job |
| `JOB_MEMORY_BYTES` | `150994944` hosted (`201326592` default) | Total helper process-group RSS ceiling on Linux |
| `MAX_HLS_MANIFEST_BYTES` | `8388608` | Maximum actual HLS manifest body size |
| `MAX_DURATION_SECONDS` | `7200` | Known-duration ceiling |
| `MAX_MEDIA_BYTES` | `226492416` | One final output ceiling |
| `MAX_STORED_BYTES` | `679477248` | Generated-file storage ceiling; must be at least three times `MAX_MEDIA_BYTES` |
| `MAX_PLAYLIST_ITEMS` | `50` | Maximum playlist entries returned for selection |
| `SMALL_FILE_MAX_BYTES` | `67108864` | Boundary between short and long expiry tiers |
| `SMALL_FILE_TTL_SECONDS` | `600` | Expiry for files below the boundary |
| `LARGE_FILE_TTL_SECONDS` | `1800` | Expiry for files at or above the boundary |
| `MAX_URL_LENGTH` | `2048` | Maximum submitted URL length |
| `MAX_REQUEST_BYTES` | `16384` | Maximum API request body |
| `RATE_LIMIT_REQUESTS` | `5` | API operations allowed in one rate window |
| `GLOBAL_RATE_LIMIT_REQUESTS` | `60` | API operations allowed globally in one rate window |
| `RATE_LIMIT_WINDOW_SECONDS` | `3600` | Rate-limit window |
| `RATE_LIMIT_MAX_CLIENTS` | `2048` | Bounded in-memory client tracking |
| `TRUST_PROXY_HEADERS` | `0` | Trust one proxy hop for HTTPS scheme and Render's client IP |
| `TRUST_X_REAL_IP` | `0` | alwaysdata only: trust its validated `X-Real-IP` instead of XFF |
| `ALLOWED_ORIGINS` | `https://majkey25.github.io` | Comma-separated exact browser origins allowed by CORS |

Run production with one Gunicorn worker so the process-local job semaphore
remains authoritative:

```sh
gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 2 \
  --timeout 1800 --graceful-timeout 300 --access-logfile - app:app
```

Gunicorn's 1800-second timeout is deliberately larger than the 900-second job
deadline. It is an outer worker-crash guard, not an extension of a job budget.

## Deploy

### Backend on alwaysdata

1. Create a Python virtual environment in the account and install
   `requirements.txt` with that environment's Python.
2. Verify FFmpeg and `deno --version`, set a writable `DOWNLOAD_DIR` inside the
   account, and use this one-worker site command (replace `<account>`):

   ```sh
   /home/<account>/venv/bin/gunicorn --bind "[$IP]:$PORT" --workers 1 \
     --threads 2 --timeout 1800 --graceful-timeout 300 \
     --access-logfile - app:app
   ```

   The current alwaysdata host provides Deno 2.9.2, but that host-managed
   version can drift; the Docker image separately pins `deno==2.8.3`.
3. Set the production values above, including
   `MAX_MEDIA_BYTES=226492416`, `MAX_STORED_BYTES=679477248`,
   `JOB_TIMEOUT_SECONDS=900`, `JOB_MEMORY_BYTES=150994944`,
   `MAX_HLS_MANIFEST_BYTES=8388608`, `GLOBAL_RATE_LIMIT_REQUESTS=60`, and the
   exact Pages origin in `ALLOWED_ORIGINS`. Set `TRUST_PROXY_HEADERS=1` and
   `TRUST_X_REAL_IP=1` so the app uses alwaysdata's trusted `X-Real-IP` for
   per-user limits and
   `X-Forwarded-Proto` for HTTPS same-origin checks. Do not enable it when the
   app is directly exposed without that proxy. See the official
   [alwaysdata HTTP stack documentation](https://help.alwaysdata.com/en/docs/web-hosting/sites/http-stack/).
4. Enable **Force HTTPS**, restart the site, and verify `/`, `/inspect`, and one complete
   prepare/fetch/delete flow from the Pages origin.
5. Add an alwaysdata **Scheduled URL** task for the backend root every minute,
   then verify its log under `/home/<account>/admin/logs/jobs/`. See the
   [alwaysdata scheduled-task documentation](https://help.alwaysdata.com/en/docs/web-hosting/tasks/).

The alwaysdata Free plan has finite CPU, memory, and disk quotas and cannot be
used for profit. Excessive scraping or CPU/network consumption can cause
throttling or service suspension. Free hosting and availability are not
guaranteed. Check the current
[alwaysdata Public Cloud limits](https://help.alwaysdata.com/en/docs/admin-billing/billing/public-cloud-prices/)
before a new deployment.

### Frontend on GitHub Pages

1. Set repository variable `API_BASE_URL` to the backend HTTPS base URL, for
   example `https://majkey25-ytdl.alwaysdata.net`.
2. In repository settings, choose **GitHub Actions** as the Pages source.
3. Push the frontend to `main` or run the **Deploy static site to Pages**
   workflow manually.

The workflow copies `templates/index.html` plus `static/` into the Pages
artifact and injects `API_BASE_URL` into `static/config.js`. If the variable is
empty, the UI loads but cannot call a backend. The Pages URL may return 404
until the first workflow deployment succeeds; verify it after deployment rather
than treating the repository link as proof that the app is live.

## Responsible use

The [Usage Policy](USAGE_POLICY.md) prohibits piracy, copyright infringement,
DRM/paywall/access-control bypass, credential use, abusive bulk extraction, and
unauthorized redistribution. It does not decide whether a particular download
is lawful; obtain permission or legal advice when unsure.

## License

Source code is available under the [MIT License](LICENSE). The hosted-service
usage policy governs the public instance and does not replace the software
license.
