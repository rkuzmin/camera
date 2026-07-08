// Service worker: offline app shell + Web Push handling.
const CACHE = "cam-v3";
const SHELL = [
  "/", "/index.html", "/app.js", "/styles.css",
  "/manifest.webmanifest", "/icons/icon-192.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // Never cache the API or the video streams (clips or live HLS) — always network.
  // hls.min.js is NOT precached: iOS plays HLS natively and never needs it, so it
  // is left to the runtime cache below (fetched on demand only where it's used).
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/_media")
      || url.pathname.startsWith("/_hls")) return;
  if (e.request.method !== "GET") return;
  // App shell: network-first so code updates roll out immediately; the cache is
  // refreshed on every successful fetch and used as the offline fallback.
  e.respondWith(
    fetch(e.request).then((res) => {
      if (res && res.ok && url.origin === location.origin) {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy));
      }
      return res;
    }).catch(() =>
      caches.match(e.request).then((hit) =>
        hit || (e.request.mode === "navigate" ? caches.match("/") : Response.error())
      )
    )
  );
});

// ---- Web Push (notifications phase) ----
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data.json(); } catch (_) {}
  const title = d.title || "Камеры";
  const body = d.body || "Событие на камере";
  e.waitUntil(
    self.registration.showNotification(title, {
      body,
      icon: "/icons/icon-192.png",
      badge: "/icons/icon-192.png",
      tag: (d.data && d.data.cam_id) || "camera",
      data: d.data || {},
    })
  );
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const target = "/?cam=" + encodeURIComponent((e.notification.data || {}).cam_id || "");
  e.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((cs) => {
      for (const c of cs) {
        if ("focus" in c) { c.navigate && c.navigate(target); return c.focus(); }
      }
      return clients.openWindow(target);
    })
  );
});
