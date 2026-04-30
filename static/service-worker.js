const CACHE_NAME = "iron-log-v6";
const CORE_ASSETS = [
    "/",
    "/static/css/styles.css",
    "/static/js/workout.js",
    "/static/manifest.webmanifest",
    "/static/icons/app-icon.svg",
];

self.addEventListener("install", (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => cache.addAll(CORE_ASSETS))
    );
    self.skipWaiting();
});

self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches.keys().then((keys) =>
            Promise.all(
                keys
                    .filter((key) => key !== CACHE_NAME)
                    .map((key) => caches.delete(key))
            )
        )
    );
    self.clients.claim();
});

self.addEventListener("fetch", (event) => {
    const { request } = event;
    if (request.method !== "GET") {
        return;
    }

    const url = new URL(request.url);
    const isHtmlRequest = request.headers.get("accept")?.includes("text/html");
    const isApiRequest = url.pathname.startsWith("/api/");

    if (isHtmlRequest || isApiRequest) {
        event.respondWith(
            fetch(request)
                .then((response) => {
                    const cloned = response.clone();
                    caches.open(CACHE_NAME).then((cache) => cache.put(request, cloned));
                    return response;
                })
                .catch(() => caches.match(request).then((response) => response || caches.match("/")))
        );
        return;
    }

    event.respondWith(
        caches.match(request).then((cached) => {
            if (cached) {
                return cached;
            }
            return fetch(request)
                .then((response) => {
                    const cloned = response.clone();
                    caches.open(CACHE_NAME).then((cache) => cache.put(request, cloned));
                    return response;
                })
                .catch(() => caches.match("/static/icons/app-icon.svg"));
        })
    );
});
