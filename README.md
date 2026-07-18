# YouTube Downloader

A small Flask + `yt-dlp` utility that prepares MP3 and MP4 files from a YouTube link.

Use it only for media you own or have permission to download.

## Requirements

- Python 3.10+
- `ffmpeg` and `ffprobe` on `PATH`

The Python dependencies include `yt-dlp`'s default components and Deno runtime for full YouTube support.

## Run locally

1. Create and activate a virtual environment.

   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   ```

2. Install dependencies.

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. Start the server.

   ```powershell
   python app.py
   ```

4. Open `http://localhost:8080`.

Optional environment variables:

- `DOWNLOAD_DIR` -> defaults to `<repo>/downloads`
- `HOST` -> defaults to `0.0.0.0`
- `PORT` -> defaults to `8080`
- `MAX_RETRIES` -> defaults to `3`
- `MAX_DURATION_SECONDS` -> defaults to `7200`
- `MAX_MEDIA_BYTES` -> defaults to `536870912` per output file
- `MAX_STORED_BYTES` -> defaults to `2147483648`
- `STALE_FILE_AGE_SECONDS` -> defaults to `3600`
- `MAX_REQUEST_BYTES` -> defaults to `16384`
- `RATE_LIMIT_REQUESTS` -> defaults to `5`
- `RATE_LIMIT_WINDOW_SECONDS` -> defaults to `3600`
- `RATE_LIMIT_MAX_CLIENTS` -> defaults to `2048`
- `ALLOWED_ORIGINS` -> comma-separated browser origins allowed to call the API; defaults to `https://majkey25.github.io`

## Deploy the static UI

GitHub Pages serves only the UI. The Flask API must run on a separate host.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Majkey25/youtube_downloader)

The included `Dockerfile` installs `ffmpeg`; `render.yaml` creates one free web service in Frankfurt.

1. Deploy the Render Blueprint and copy its `onrender.com` URL.
2. Set the backend's `ALLOWED_ORIGINS` to the exact Pages origin.
3. Set the repository variable `API_BASE_URL` to the backend base URL.
4. Enable GitHub Pages with **GitHub Actions** as the source.

Without `API_BASE_URL`, Pages still deploys the UI but disables downloads with a clear configuration message.

Only direct video links are accepted; playlists, live streams, and media over the configured limits are rejected. The included Render service allows videos up to 10 minutes and 128 MiB per output, with five download requests per client per hour. Generated files are served once from `/downloads/<filename>` and then removed. The browser also requests cleanup when replacing a result or leaving the page, and the server expires abandoned artifacts before new jobs.

## License

MIT. See [LICENSE](LICENSE).
