/* Offline runtime cache for school display pages, assets and media.
 *
 * Versioned-asset contract
 * ------------------------
 * Every asset the display page loads carries a `?v=<release>` cache buster.
 * The cache key therefore MUST include the query string: a release that bumps
 * `?v=` has to miss the cache and reach the network, otherwise a screen keeps
 * running the previous build forever and no deploy ever lands on it.
 *
 * The offline lifeline is the only place a version-agnostic lookup is allowed:
 * when the network is unreachable, serving last release's file beats serving
 * nothing. It never runs while the screen is online.
 */

const RELEASE = 'v10';
const SHELL_CACHE = 'school-display-shell-' + RELEASE;
const RUNTIME_CACHE = 'school-display-runtime-' + RELEASE;
const EXPECTED_CACHES = [SHELL_CACHE, RUNTIME_CACHE];

/* Offline fallbacks only. These are unversioned URLs, so they are never what a
 * live screen actually loads — the page always requests `?v=<release>`. Keep
 * this list aligned with the assets `templates/website/display.html` links. */
const SHELL_ASSETS = [
  '/static/css/tailwind.generated.css',
  '/static/css/app.css',
  '/static/css/display-board.css',
  '/static/css/display-legacy.css',
  '/static/css/custom-display-theme.css',
  '/static/css/display-controls.css',
  '/static/css/fonts.css',
  '/static/js/display-controls.js',
  '/static/js/display-theme.js',
  '/static/js/occasion-theme.js',
  '/static/js/display.min.js',
  '/static/js/display-sw-register.js',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      // `addAll` is all-or-nothing; a single 404 would discard the whole shell.
      .then((cache) => Promise.all(
        SHELL_ASSETS.map((asset) => cache.add(asset).catch(() => undefined))
      ))
      .catch(() => undefined)
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys
          .filter((key) => key.startsWith('school-display-') && EXPECTED_CACHES.indexOf(key) === -1)
          .map((key) => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

/**
 * Drop every cached entry that shares this request's path but carries a
 * different `?v=`. Without this the runtime cache would keep one copy per
 * release forever and slowly fill the device's storage quota.
 */
async function dropSupersededVersions(cache, request) {
  try {
    const keepUrl = request.url;
    const keepPath = new URL(keepUrl).pathname;
    const keys = await cache.keys();
    await Promise.all(keys.map((key) => {
      if (key.url === keepUrl) return undefined;
      let path = '';
      try {
        path = new URL(key.url).pathname;
      } catch (_) {
        return undefined;
      }
      return path === keepPath ? cache.delete(key) : undefined;
    }));
  } catch (_) {
    /* Pruning is housekeeping; never let it fail a response. */
  }
}

/**
 * Assets are immutable per release, so an exact-URL hit is served straight from
 * cache. A miss means a new release: go to the network, store it under the new
 * key, and retire the previous one. Only a network failure falls back to a
 * version-agnostic match, which keeps an offline screen booting.
 */
async function handleAsset(request) {
  const cache = await caches.open(RUNTIME_CACHE);

  const exact = await cache.match(request);
  if (exact) return exact;

  try {
    const response = await fetch(request);
    /* Cross-origin display images are opaque in no-cors mode. They are still
     * valid cache entries and can be replayed to an <img> while offline. */
    if (response && (response.ok || response.type === 'opaque')) {
      await cache.put(request, response.clone());
      await dropSupersededVersions(cache, request);
    }
    return response;
  } catch (offline) {
    const anyVersion =
      (await cache.match(request, { ignoreSearch: true })) ||
      (await caches.match(request, { ignoreSearch: true, cacheName: SHELL_CACHE }));
    if (anyVersion) return anyVersion;
    throw offline;
  }
}

function parseByteRange(value, size) {
  const match = /^bytes=(\d*)-(\d*)$/i.exec(String(value || '').trim());
  if (!match || !size) return null;
  let start = match[1] ? parseInt(match[1], 10) : null;
  let end = match[2] ? parseInt(match[2], 10) : null;

  if (start === null && end !== null) {
    const suffixLength = Math.min(size, end);
    start = size - suffixLength;
    end = size - 1;
  } else {
    start = start === null ? 0 : start;
    end = end === null ? size - 1 : Math.min(end, size - 1);
  }
  if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || start > end || start >= size) {
    return null;
  }
  return { start, end };
}

/**
 * Audio/video players commonly request byte ranges. We never cache the 206
 * network response, but a complete file warmed earlier can satisfy that range
 * after a TV reboot with no network.
 */
async function handleRangeRequest(request) {
  const cache = await caches.open(RUNTIME_CACHE);
  const cached = await cache.match(request.url);
  if (cached && cached.type !== 'opaque') {
    const buffer = await cached.arrayBuffer();
    const range = parseByteRange(request.headers.get('range'), buffer.byteLength);
    if (range) {
      const headers = new Headers(cached.headers);
      headers.set('Accept-Ranges', 'bytes');
      headers.set('Content-Range', `bytes ${range.start}-${range.end}/${buffer.byteLength}`);
      headers.set('Content-Length', String(range.end - range.start + 1));
      return new Response(buffer.slice(range.start, range.end + 1), {
        status: 206,
        statusText: 'Partial Content',
        headers,
      });
    }
  }
  return fetch(request);
}

/**
 * The display page is data-bearing HTML, so it is always network-first; the
 * cached copy exists purely so a screen that boots without a network still
 * shows its last known board instead of a browser error page.
 */
async function handleNavigation(request) {
  const cache = await caches.open(RUNTIME_CACHE);
  try {
    const response = await fetch(request);
    if (response && response.ok) await cache.put(request, response.clone());
    return response;
  } catch (offline) {
    const cached = await cache.match(request);
    if (cached) return cached;
    throw offline;
  }
}

async function cacheDisplayPage(rawUrl) {
  let url;
  try {
    url = new URL(rawUrl, self.location.origin);
  } catch (_) {
    return;
  }
  if (
    url.origin !== self.location.origin ||
    url.pathname.startsWith('/api/') ||
    url.pathname.startsWith('/dashboard/') ||
    url.pathname.startsWith('/admin/')
  ) return;

  const request = new Request(url.href, { credentials: 'same-origin' });
  const response = await fetch(request);
  if (!response || !response.ok) return;
  const cache = await caches.open(RUNTIME_CACHE);
  await cache.put(request, response.clone());
}

async function cacheDisplayMedia(rawUrls) {
  const urls = Array.isArray(rawUrls) ? rawUrls.slice(0, 48) : [];
  const cache = await caches.open(RUNTIME_CACHE);
  await Promise.all(urls.map(async (rawUrl) => {
    let url;
    try {
      url = new URL(rawUrl, self.location.origin);
    } catch (_) {
      return;
    }
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return;

    const sameOrigin = url.origin === self.location.origin;
    const request = new Request(url.href, {
      mode: sameOrigin ? 'same-origin' : 'no-cors',
      credentials: sameOrigin ? 'same-origin' : 'omit',
      cache: 'no-cache',
    });
    try {
      if (await cache.match(request)) return;
      const response = await fetch(request);
      if (response && (response.ok || response.type === 'opaque')) {
        await cache.put(request, response.clone());
      }
    } catch (_) {
      /* Best effort: one unavailable image must not discard the other media. */
    }
  }));
}

self.addEventListener('message', (event) => {
  const data = event.data || {};
  if (data.type === 'CACHE_DISPLAY_PAGE') {
    event.waitUntil(cacheDisplayPage(data.url).catch(() => undefined));
    return;
  }
  if (data.type === 'CACHE_DISPLAY_MEDIA') {
    event.waitUntil(cacheDisplayMedia(data.urls).catch(() => undefined));
  }
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  /* Never store a 206 response. A pre-warmed complete media file can satisfy
   * the byte range locally; otherwise the request continues to the network. */
  if (request.headers.has('range')) {
    event.respondWith(handleRangeRequest(request));
    return;
  }

  let url;
  try {
    url = new URL(request.url);
  } catch (_) {
    return;
  }

  if (url.origin !== self.location.origin) {
    if (request.destination === 'image' || request.destination === 'audio') {
      event.respondWith(handleAsset(request));
    }
    return;
  }
  if (
    url.pathname.startsWith('/api/') ||
    url.pathname.startsWith('/dashboard/') ||
    url.pathname.startsWith('/admin/')
  ) {
    return;
  }

  const isAsset =
    url.pathname.startsWith('/static/') ||
    url.pathname.startsWith('/media/') ||
    /\.(png|jpe?g|webp|gif|svg|css|js|woff2?)$/i.test(url.pathname);

  if (isAsset) {
    event.respondWith(handleAsset(request));
    return;
  }

  if (request.mode === 'navigate') {
    event.respondWith(handleNavigation(request));
  }
});
