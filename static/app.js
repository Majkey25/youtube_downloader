const form = document.getElementById('downloadForm');
const linkInput = document.getElementById('youtubeLink');
const submitButton = document.getElementById('submitButton');
const hint = document.getElementById('hint');
const loadingBar = document.getElementById('loadingText');
const errorBox = document.getElementById('error');
const downloadArea = document.getElementById('downloadLink');
const downloadMp3Anchor = document.getElementById('downloadMp3Anchor');
const downloadMp4Anchor = document.getElementById('downloadMp4Anchor');

const configuredApiBase = window.APP_CONFIG?.apiBaseUrl;
const normalizedBase = typeof configuredApiBase === 'string'
    && configuredApiBase !== '__API_BASE_URL__'
    ? configuredApiBase.trim().replace(/\/+$/, '')
    : '';
const backendIsMissing = window.location.hostname.endsWith('github.io') && !normalizedBase;

const buildUrl = (path) => {
    const cleanPath = path.replace(/^\/+/, '');
    return normalizedBase ? `${normalizedBase}/${cleanPath}` : `/${cleanPath}`;
};

let isDownloading = false;
let currentFiles = [];

const setHidden = (element, hidden) => {
    element.classList.toggle('hidden', hidden);
};

const setBusy = (busy) => {
    isDownloading = busy;
    linkInput.disabled = busy;
    submitButton.disabled = busy || backendIsMissing;
    submitButton.textContent = busy ? 'Preparing…' : 'Prepare files';
};

const deleteFiles = async (files, keepalive = false) => {
    if (files.length === 0) {
        return;
    }
    const formData = new FormData();
    files.forEach((file) => formData.append('files', file));
    try {
        await fetch(buildUrl('delete'), {
            method: 'POST',
            body: formData,
            keepalive,
        });
    } catch {
        // Server-side expiration is the fallback for interrupted cleanup.
    }
};

const releaseCurrentFiles = async () => {
    const files = currentFiles;
    currentFiles = [];
    await deleteFiles(files);
};

const resetFeedback = () => {
    setHidden(hint, false);
    setHidden(loadingBar, true);
    setHidden(errorBox, true);
    setHidden(downloadArea, true);
};

const startLoading = () => {
    setHidden(hint, true);
    setHidden(loadingBar, false);
};

const showError = (message) => {
    errorBox.textContent = message;
    setHidden(hint, true);
    setHidden(loadingBar, true);
    setHidden(downloadArea, true);
    setHidden(errorBox, false);
};

const showDownloads = (files) => {
    const mp3File = typeof files?.mp3_file === 'string' ? files.mp3_file : '';
    const mp4File = typeof files?.mp4_file === 'string' ? files.mp4_file : '';
    if (!mp3File || !mp4File) {
        throw new Error('Download server returned incomplete file information.');
    }

    downloadMp3Anchor.href = `${buildUrl('downloads')}/${encodeURIComponent(mp3File)}`;
    downloadMp3Anchor.download = mp3File;
    downloadMp4Anchor.href = `${buildUrl('downloads')}/${encodeURIComponent(mp4File)}`;
    downloadMp4Anchor.download = mp4File;
    currentFiles = [mp3File, mp4File];

    setHidden(hint, true);
    setHidden(loadingBar, true);
    setHidden(errorBox, true);
    setHidden(downloadArea, false);
};

const warnIfBackendIsMissing = () => {
    if (backendIsMissing) {
        showError('Download server is not configured. Set API_BASE_URL before deploying.');
        setBusy(false);
    }
};

form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (backendIsMissing) {
        warnIfBackendIsMissing();
        return;
    }
    if (isDownloading) {
        return;
    }
    const youtubeLink = linkInput.value.trim();
    if (!youtubeLink) {
        showError('Paste a valid YouTube link.');
        return;
    }

    resetFeedback();
    startLoading();
    setBusy(true);

    try {
        await releaseCurrentFiles();
        const formData = new FormData();
        formData.append('youtubeLink', youtubeLink);
        const response = await fetch(buildUrl('download'), {
            method: 'POST',
            body: formData,
        });
        const contentType = response.headers.get('content-type') || '';
        if (!contentType.includes('application/json')) {
            throw new Error('Download server returned an invalid response.');
        }

        const payload = await response.json();
        if (!response.ok) {
            throw new Error(payload?.error || 'Download failed. Try again.');
        }
        showDownloads(payload?.files);
    } catch (error) {
        const message = error instanceof Error ? error.message : 'Download failed. Try again.';
        showError(message);
    } finally {
        setBusy(false);
    }
});

window.addEventListener('beforeunload', () => {
    if (isDownloading || currentFiles.length === 0) {
        return;
    }
    void deleteFiles(currentFiles, true);
});

document.getElementById('year').textContent = new Date().getFullYear();
warnIfBackendIsMissing();
