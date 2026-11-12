// Makes the app installable and quick to open: the shell is cached, everything under /api always goes to the box.
const SHELL = "franziska-shell-v5";
const WORLD = ["net", "noise", "props", "biomes", "land", "atmos", "team", "fly", "hud", "main"].map((m) => `/static/world/${m}.js`);
const THREE = ["three.module.min.js", ...["EffectComposer", "RenderPass", "UnrealBloomPass", "OutputPass", "ShaderPass", "MaskPass", "Pass"]
  .map((p) => `addons/postprocessing/${p}.js`), ...["CopyShader", "LuminosityHighPassShader", "OutputShader"].map((s) => `addons/shaders/${s}.js`)]
  .map((f) => `/static/vendor/three/${f}`);
const FILES = ["/", "/static/style.css", "/static/app.js", "/static/flows.js", "/icon.svg", "/manifest.webmanifest",
  "/world", "/static/world/world.css", ...WORLD, ...THREE];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(FILES)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
// Network first (a new version shows at once); the cached shell only when the box can't be reached.
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.pathname.startsWith("/api/") || url.pathname.startsWith("/login")
      || url.pathname.startsWith("/sql/") || url.pathname.startsWith("/phoenix/")) return;  // the tools: always the box
  e.respondWith(fetch(e.request).then((res) => {
    if (res.ok && FILES.includes(url.pathname)) caches.open(SHELL).then((c) => c.put(e.request, res.clone()));
    return res;
  }).catch(() => caches.match(e.request)));
});
