# YouTube Downloader

Fast MP3/MP4 downloads powered by `yt-dlp`, Flask, and a minimal glassy UI ready for GitHub Pages.

## Features
- Clean, responsive landing page with subtle glow animations.
- High-quality MP3 + MP4 downloads via `yt-dlp` (runs on the backend).
- Config-driven: all tunables live in `config.py` and can be overridden with environment variables.
- PWA support (manifest + service worker) with cache-friendly relative paths.
- GitHub Actions workflow to ship the static site to GitHub Pages.

## Requirements
- Python 3.10+
- `ffmpeg` available on the system path (required by `yt-dlp` for audio extraction).

## Quick start (local backend)
1. Create and activate a virtual environment.
   ```bash
   python -m venv .venv
   .venv/Scripts/Activate.ps1  # Windows PowerShell
   ```
2. Install dependencies.
   ```bash
   pip install -r requirements.txt
   ```
3. (Optional) Set environment variables to override defaults in `config.py`:
   - `SECRET_KEY` (Flask session key)
   - `DOWNLOAD_DIR` (default: `<repo>/downloads`)
   - `HOST` (default: `0.0.0.0`)
   - `PORT` (default: `8080`)
   - `USER_AGENT`, `MAX_RETRIES`, `RANDOM_DELAY_MIN_SECONDS`, `RANDOM_DELAY_MAX_SECONDS`
4. Run the server.
   ```bash
   python app.py
   ```
5. Open `http://localhost:8080` and paste a YouTube link.

## Deployment model
GitHub Pages is static-only. The UI deploys there, while the Flask backend must run elsewhere
(Render, Railway, Fly.io, VPS, etc.). Point the frontend to your backend URL via
`static/config.js` (or let the workflow rewrite it with `API_BASE_URL`).

## GitHub Pages workflow
`/.github/workflows/pages.yml` builds and publishes the static assets from `templates/` and
`static/` to GitHub Pages.

Before enabling Pages:
- In repository **Settings → Pages**, choose `GitHub Actions` as the source.
- (Optional) Add repository variable `API_BASE_URL` with your backend base URL (e.g.
  `https://your-backend.example.com`). The workflow will inject it into `static/config.js`.

Manual run:
- Trigger “Deploy static site to Pages” from the Actions tab, or push to `main`.

## Notes
- The service worker caches `index.html`, CSS/JS, and icons using relative paths, so it also works
  under the `/<repo>` GitHub Pages prefix.
- Downloads are cleaned up via the `/delete` endpoint on page unload; you can also POST to `/delete`
  to purge the last files.

## License
This project is licensed under the MIT License. See `LICENSE` for details.
