// Define the cache name
const CACHE_NAME = 'yt-downloader-v2';

// Specify the files to cache
const urlsToCache = [
    './',
    'service-worker.js',
    'static/styles.css',
    'static/app.js',
    'static/config.js',
    'static/logo/logo-192x192.png',
    'static/logo/logo-512x512.png',
];
// Installation of the service worker and caching files
self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => {
            console.log('Opened cache and caching resources');
            return cache.addAll(urlsToCache);
        })
    );
});

// Activation of the service worker and clearing old caches
self.addEventListener('activate', event => {
    const cacheWhitelist = [CACHE_NAME];
    event.waitUntil(
        caches.keys().then(cacheNames => {
            return Promise.all(
                cacheNames.map(cacheName => {
                    // Delete old caches that are not in the whitelist
                    if (!cacheWhitelist.includes(cacheName)) {
                        console.log(`Deleting old cache: ${cacheName}`);
                        return caches.delete(cacheName);
                    }
                })
            );
        })
    );
});

// Intercept requests and serve them from the cache
self.addEventListener('fetch', event => {
    event.respondWith(
        caches.match(event.request).then(response => {
            // Return the cached response if found, otherwise fetch from network
            if (response) {
                console.log(`Serving from cache: ${event.request.url}`);
                return response;
            }
            console.log(`Fetching from network: ${event.request.url}`);
            return fetch(event.request).catch(error => {
                console.error(`Fetching failed: ${error}`);
                throw error; // Re-throw the error to handle it later if needed
            });
        })
    );
});
