const state = { url: '', playlistIndex: null, file: null, busy: false };

const form = document.getElementById('inspectForm');
const linkInput = document.getElementById('mediaUrl');
const analyzeButton = document.getElementById('analyzeButton');
const pendingSource = document.getElementById('pendingSource');
const statusRegion = document.getElementById('statusRegion');
const statusText = document.getElementById('statusText');
const inspectionPanel = document.getElementById('inspectionPanel');
const detectedSource = document.getElementById('detectedSource');
const mediaTitle = document.getElementById('mediaTitle');
const durationLabel = document.getElementById('durationLabel');
const engineLabel = document.getElementById('engineLabel');
const playlistBlock = document.getElementById('playlistBlock');
const playlistSelect = document.getElementById('playlistSelect');
const playlistNote = document.getElementById('playlistNote');
const outputBlock = document.getElementById('outputBlock');
const outputButtons = document.getElementById('outputButtons');
const readyBlock = document.getElementById('readyBlock');
const readyMeta = document.getElementById('readyMeta');
const downloadAnchor = document.getElementById('downloadAnchor');
const themeToggle = document.getElementById('themeToggle');
const themeLabel = document.getElementById('themeLabel');
const themeColor = document.querySelector('meta[name="theme-color"]');
const darkPreference = matchMedia('(prefers-color-scheme: dark)');
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');

let savedTheme = null;
try {
    savedTheme = localStorage.getItem('media-dl-theme');
} catch {
    // The system preference remains available when browser storage is blocked.
}

const applyTheme = (theme, persist = false) => {
    const dark = theme === 'dark';
    const nextTheme = dark ? 'light' : 'dark';
    document.documentElement.dataset.theme = theme;
    themeToggle.setAttribute('aria-label', `Switch to ${nextTheme} theme`);
    themeLabel.textContent = nextTheme.toUpperCase();
    themeColor?.setAttribute('content', dark ? '#090909' : '#f5f5f2');
    if (persist) {
        try {
            localStorage.setItem('media-dl-theme', theme);
        } catch {
            // Theme persistence is optional; the active theme still works.
        }
    }
};

const initialTheme = savedTheme === 'light' || savedTheme === 'dark'
    ? savedTheme
    : darkPreference.matches ? 'dark' : 'light';
applyTheme(initialTheme);

const configuredApiBase = typeof APP_CONFIG === 'object' ? APP_CONFIG.apiBaseUrl : '';
const normalizedBase = typeof configuredApiBase === 'string'
    && configuredApiBase !== '__API_BASE_URL__'
    ? configuredApiBase.trim().replace(/\/+$/, '')
    : '';
const backendIsMissing = location.hostname.endsWith('github.io') && !normalizedBase;

const buildUrl = (path) => {
    const cleanPath = path.replace(/^\/+/, '');
    return normalizedBase ? `${normalizedBase}/${cleanPath}` : `/${cleanPath}`;
};

const setHidden = (element, hidden) => {
    element.hidden = hidden;
};

const setStatus = (message, tone = 'idle') => {
    statusText.textContent = message;
    statusRegion.dataset.tone = tone;
};

const setBusy = (busy, message = '') => {
    state.busy = busy;
    linkInput.disabled = busy;
    analyzeButton.disabled = busy || backendIsMissing;
    analyzeButton.textContent = busy ? 'Working…' : 'Analyze';
    playlistSelect.disabled = busy;
    outputButtons.querySelectorAll('button').forEach((button) => {
        button.disabled = busy;
    });
    if (message) {
        setStatus(message, 'busy');
    }
};

const updatePendingSource = () => {
    const value = linkInput.value.trim();
    if (!value) {
        pendingSource.textContent = 'WAITING FOR LINK';
        return;
    }
    try {
        const hostname = new URL(value).hostname;
        if (!hostname) {
            throw new TypeError('URL has no hostname');
        }
        pendingSource.textContent = `PENDING // ${hostname.toUpperCase()}`;
    } catch {
        pendingSource.textContent = 'PENDING // INVALID URL';
    }
};

const handleInput = () => {
    updatePendingSource();
    if (!state.url || linkInput.value.trim() === state.url) {
        return;
    }
    const filename = state.file;
    state.url = '';
    state.playlistIndex = null;
    state.file = null;
    clearInspection();
    setStatus('READY // Analyze the changed URL.', 'idle');
    void deleteFile(filename);
};

const deleteFile = async (filename, keepalive = false) => {
    if (!filename) {
        return;
    }
    const body = new FormData();
    body.append('files', filename);
    try {
        await fetch(buildUrl('delete'), { method: 'POST', body, keepalive });
    } catch {
        // Automatic server expiry remains the fallback when cleanup is interrupted.
    }
};

const releaseFile = async () => {
    const filename = state.file;
    state.file = null;
    setHidden(readyBlock, true);
    await deleteFile(filename);
};

const requestJson = async (path, body) => {
    let response;
    try {
        response = await fetch(buildUrl(path), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
    } catch {
        throw new Error('Cannot reach the download server. Try again later.');
    }

    let payload;
    try {
        payload = await response.json();
    } catch {
        throw new Error('The download server returned an invalid response.');
    }
    if (!response.ok) {
        throw new Error(typeof payload?.error === 'string' ? payload.error : 'Request failed.');
    }
    return payload;
};

const formatDuration = (seconds) => {
    if (!Number.isFinite(seconds) || seconds < 0) {
        return 'Unknown';
    }
    const total = Math.round(seconds);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const remainder = total % 60;
    return hours > 0
        ? `${hours}:${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`
        : `${minutes}:${String(remainder).padStart(2, '0')}`;
};

const showFacts = (payload) => {
    detectedSource.textContent = typeof payload.source === 'string' ? payload.source : 'Unknown';
    mediaTitle.textContent = typeof payload.title === 'string' ? payload.title : 'Untitled media';
    if (Object.hasOwn(payload, 'duration_seconds')) {
        durationLabel.textContent = formatDuration(payload.duration_seconds);
    }
    engineLabel.textContent = `ENGINE // ${typeof payload.engine === 'string' ? payload.engine : 'unknown'}`;
    setHidden(inspectionPanel, false);
};

const clearInspection = () => {
    setHidden(inspectionPanel, true);
    setHidden(playlistBlock, true);
    setHidden(outputBlock, true);
    setHidden(readyBlock, true);
    outputButtons.replaceChildren();
    const placeholder = new Option('Select an item…', '');
    placeholder.disabled = true;
    placeholder.selected = true;
    playlistSelect.replaceChildren(placeholder);
};

const showReadyFile = (payload) => {
    const file = payload?.file;
    if (!file || typeof file.name !== 'string' || !file.name) {
        throw new Error('The download server returned incomplete file information.');
    }
    state.file = file.name;
    const extension = typeof file.extension === 'string' ? file.extension.toUpperCase() : 'FILE';
    const mode = typeof file.mode === 'string' ? file.mode.toUpperCase() : 'MEDIA';
    const size = Number.isFinite(file.size_bytes)
        ? ` // ${(file.size_bytes / 1048576).toFixed(1)} MiB`
        : '';
    readyMeta.textContent = `${mode} // ${extension}${size}`;
    downloadAnchor.href = `${buildUrl('downloads')}/${encodeURIComponent(file.name)}`;
    downloadAnchor.download = file.name;
    downloadAnchor.textContent = `Download .${extension}`;
    setHidden(readyBlock, false);
    setStatus('READY // Open the file once. The server copy is then removed.', 'ready');
};

const downloadMode = async (mode) => {
    if (state.busy) {
        return;
    }
    setBusy(true, `CREATING ${mode.toUpperCase()} // Keep this tab open.`);
    try {
        await releaseFile();
        const body = { url: state.url, mode };
        if (state.playlistIndex !== null) {
            body.playlist_index = state.playlistIndex;
        }
        const payload = await requestJson('download', body);
        showFacts(payload);
        showReadyFile(payload);
    } catch (error) {
        const message = error instanceof Error ? error.message : 'Download failed. Try again.';
        setStatus(`ERROR // ${message}`, 'error');
    } finally {
        setBusy(false);
    }
};

const showOutputChoices = (payload) => {
    const modes = Array.isArray(payload.outputs)
        ? [...new Set(payload.outputs.filter((mode) => mode === 'audio' || mode === 'video'))]
        : [];
    if (modes.length === 0) {
        throw new Error('No downloadable output is available for this item.');
    }
    outputButtons.replaceChildren();
    modes.forEach((mode) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'output-button';
        button.textContent = mode === 'audio' ? 'AUDIO // MP3' : 'VIDEO // BEST AVAILABLE';
        button.addEventListener('click', () => void downloadMode(mode));
        outputButtons.append(button);
    });
    setHidden(outputBlock, false);
    setStatus('INSPECTED // Choose one available output.', 'ready');
};

const showMedia = (payload) => {
    if (payload?.kind !== 'media') {
        throw new Error('The download server returned an invalid media result.');
    }
    showFacts(payload);
    showOutputChoices(payload);
};

const inspectPlaylistItem = async () => {
    const index = Number(playlistSelect.value);
    if (!Number.isInteger(index) || index < 1 || state.busy) {
        return;
    }
    state.playlistIndex = index;
    setHidden(outputBlock, true);
    setHidden(readyBlock, true);
    setBusy(true, `INSPECTING ITEM ${index} // Checking available outputs.`);
    try {
        await releaseFile();
        const payload = await requestJson('inspect', {
            url: state.url,
            playlist_index: index,
        });
        showMedia(payload);
    } catch (error) {
        const message = error instanceof Error ? error.message : 'Inspection failed. Try again.';
        setStatus(`ERROR // ${message}`, 'error');
    } finally {
        setBusy(false);
    }
};

const showPlaylist = (payload) => {
    const entries = Array.isArray(payload.entries) ? payload.entries : [];
    if (entries.length === 0) {
        throw new Error('This playlist has no selectable public items.');
    }
    showFacts(payload);
    durationLabel.textContent = 'Choose one item';
    const placeholder = new Option('Select an item…', '');
    placeholder.disabled = true;
    placeholder.selected = true;
    playlistSelect.replaceChildren(placeholder);
    entries.forEach((entry) => {
        if (!Number.isInteger(entry?.index) || entry.index < 1) {
            return;
        }
        const title = typeof entry.title === 'string' ? entry.title : `Item ${entry.index}`;
        playlistSelect.append(new Option(`${entry.index}. ${title}`, String(entry.index)));
    });
    if (playlistSelect.options.length === 1) {
        throw new Error('This playlist has no selectable public items.');
    }
    playlistNote.textContent = payload.truncated
        ? 'Showing the first 50 items. Only one item can be downloaded.'
        : 'Only one selected item can be downloaded.';
    setHidden(playlistBlock, false);
    setHidden(outputBlock, true);
    setStatus('PLAYLIST // Choose one item to inspect.', 'ready');
};

form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (state.busy || backendIsMissing || !form.reportValidity()) {
        return;
    }
    const url = linkInput.value.trim();
    state.url = url;
    state.playlistIndex = null;
    setBusy(true, 'ANALYZING // Identifying source and available outputs.');
    try {
        await releaseFile();
        clearInspection();
        const payload = await requestJson('inspect', { url });
        if (payload?.kind === 'playlist') {
            showPlaylist(payload);
        } else {
            showMedia(payload);
        }
    } catch (error) {
        const message = error instanceof Error ? error.message : 'Inspection failed. Try again.';
        clearInspection();
        setStatus(`ERROR // ${message}`, 'error');
    } finally {
        setBusy(false);
    }
});

linkInput.addEventListener('input', handleInput);
playlistSelect.addEventListener('change', () => void inspectPlaylistItem());
downloadAnchor.addEventListener('click', () => {
    state.file = null;
});
themeToggle.addEventListener('click', () => {
    const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    const update = () => applyTheme(theme, true);
    if (typeof document.startViewTransition === 'function' && !reducedMotion.matches) {
        document.startViewTransition(update);
    } else {
        update();
    }
});

window.addEventListener('beforeunload', () => {
    if (state.file) {
        void deleteFile(state.file, true);
    }
});

if (backendIsMissing) {
    setStatus('ERROR // Download backend is not configured for this Pages build.', 'error');
    setBusy(false);
}
