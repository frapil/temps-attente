/* Service worker : met en cache les fichiers de l'appli (démarrage rapide, interface disponible hors ligne).
 * Les appels aux API (PRIM, données IDFM, /api/ du serveur local) ne sont jamais mis en cache.
 * Changer VERSION à chaque mise à jour de l'appli pour forcer le rafraîchissement du cache. */
const VERSION = "v2";
const CACHE = "prochains-passages-" + VERSION;
const SHELL = ["./", "index.html", "prim-direct.js", "manifest.webmanifest", "icon-192.png", "icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k.startsWith("prochains-passages-") && k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // uniquement les fichiers de l'appli (même origine, hors /api/)
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.includes("/api/")) return;
  // réseau d'abord (pour recevoir les mises à jour), cache en secours hors ligne
  e.respondWith(
    fetch(e.request)
      .then(r => {
        if (r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); }
        return r;
      })
      .catch(() => caches.match(e.request).then(r => r || caches.match("index.html")))
  );
});
