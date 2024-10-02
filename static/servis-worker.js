const CACHE_NAME = 'yt-mp3-downloader-v1';
const urlsToCache = [
    '/',
    '/static/styles.css',
    '/static/app.js',
    '/static/logo/logo-192x192.png',
    '/static/logo/logo-512x512.png'
];

// Instalace service workeru a přidání souborů do cache
self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then(cache => {
                console.log('Opened cache');
                return cache.addAll(urlsToCache);
            })
    );
});

// Aktivace service workeru a vymazání starých cache
self.addEventListener('activate', event => {
    const cacheWhitelist = [CACHE_NAME];
    event.waitUntil(
        caches.keys().then(cacheNames => {
            return Promise.all(
                cacheNames.map(cacheName => {
                    if (!cacheWhitelist.includes(cacheName)) {
                        return caches.delete(cacheName);
                    }
                })
            );
        })
    );
});

// Interceptovat požadavky a servírovat je z cache
self.addEventListener('fetch', event => {
    event.respondWith(
        caches.match(event.request)
            .then(response => {
                return response || fetch(event.request);
            })
    );
});
