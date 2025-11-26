const form = document.getElementById('downloadForm');
const linkInput = document.getElementById('youtubeLink');
const loadingBar = document.getElementById('loadingText');
const loadingCopy = document.getElementById('loadingCopy');
const errorBox = document.getElementById('error');
const downloadArea = document.getElementById('downloadLink');
const downloadMp3Anchor = document.getElementById('downloadMp3Anchor');
const downloadMp4Anchor = document.getElementById('downloadMp4Anchor');

const config = window.APP_CONFIG || { apiBaseUrl: '__API_BASE_URL__' };
const normalizedBase = (() => {
    const base = config.apiBaseUrl || '';
    if (base === '__API_BASE_URL__') {
        return '';
    }
    return base.endsWith('/') ? base.slice(0, -1) : base;
})();

const buildUrl = (path) => {
    const trimmed = path.replace(/^\/+/, '');
    return normalizedBase ? `${normalizedBase}/${trimmed}` : trimmed;
};

let isDownloading = false;
let messageInterval;
let warnedAboutApi = false;

const setHidden = (element, hidden) => {
    if (!element) {
        return;
    }
    if (hidden) {
        element.classList.add('hidden');
    } else {
        element.classList.remove('hidden');
    }
};

const resetUI = () => {
    clearInterval(messageInterval);
    setHidden(loadingBar, true);
    setHidden(errorBox, true);
    setHidden(downloadArea, true);
    if (loadingCopy) {
        loadingCopy.textContent = 'Preparing your download';
    }
};

const startLoading = () => {
    const steps = [
        'Preparing your download',
        'Fetching the stream',
        'Converting formats',
        'Wrapping it up',
    ];
    let index = 0;
    setHidden(loadingBar, false);
    messageInterval = setInterval(() => {
        index = (index + 1) % steps.length;
        if (loadingCopy) {
            loadingCopy.textContent = steps[index];
        }
    }, 1200);
};

const showError = (message) => {
    if (!errorBox) {
        return;
    }
    errorBox.textContent = message;
    setHidden(errorBox, false);
};

const showDownloads = (files) => {
    if (!files) {
        return;
    }
    const { mp3_file: mp3File, mp4_file: mp4File } = files;
    if (mp3File && downloadMp3Anchor) {
        downloadMp3Anchor.href = `${buildUrl('downloads')}/${mp3File}`;
        downloadMp3Anchor.download = mp3File;
        setHidden(downloadMp3Anchor, false);
    } else if (downloadMp3Anchor) {
        setHidden(downloadMp3Anchor, true);
    }
    if (mp4File && downloadMp4Anchor) {
        downloadMp4Anchor.href = `${buildUrl('downloads')}/${mp4File}`;
        downloadMp4Anchor.download = mp4File;
        setHidden(downloadMp4Anchor, false);
    } else if (downloadMp4Anchor) {
        setHidden(downloadMp4Anchor, true);
    }
    setHidden(downloadArea, false);
};

const maybeWarnMissingApi = () => {
    const isGitHubPages = window.location.hostname.endsWith('github.io');
    if (!normalizedBase && isGitHubPages && !warnedAboutApi) {
        warnedAboutApi = true;
        showError('API endpoint is not configured. Set API_BASE_URL in config.js or Pages workflow.');
    }
};

form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!linkInput.value) {
        showError('Please paste a valid YouTube link.');
        return;
    }
    resetUI();
    startLoading();
    isDownloading = true;

    try {
        const formData = new FormData();
        formData.append('youtubeLink', linkInput.value.trim());

        const response = await fetch(buildUrl('download'), {
            method: 'POST',
            body: formData,
        });

        let payload = null;
        const contentType = response.headers.get('content-type') || '';
        if (contentType.includes('application/json')) {
            payload = await response.json();
        } else {
            const text = await response.text();
            const details = normalizedBase
                ? 'Server did not return JSON.'
                : 'Are you pointing the frontend to a running backend?';
            throw new Error(`${details} (${text.slice(0, 120)}...)`);
        }

        if (!response.ok) {
            throw new Error(payload.error || 'Download failed. Please retry.');
        }

        clearInterval(messageInterval);
        setHidden(loadingBar, true);
        showDownloads(payload.files);
    } catch (error) {
        clearInterval(messageInterval);
        setHidden(loadingBar, true);
        showError(error.message || 'Something went wrong.');
    } finally {
        isDownloading = false;
    }
});

window.addEventListener('beforeunload', () => {
    if (!isDownloading) {
        fetch(buildUrl('delete'), {
            method: 'POST',
            keepalive: true,
        }).catch(() => {});
    }
});

maybeWarnMissingApi();
