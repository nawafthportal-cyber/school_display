/* The worker runs on the same pre-async JavaScript engines as the display.
 * Exact versioned asset URLs are used online. Older cached resources are
 * considered only after a network failure.
 */
const RELEASE = 'v17';
const SHELL_CACHE = 'school-display-shell-' + RELEASE;
// Retain the last working board and its media across worker updates.
const RUNTIME_CACHE = 'school-display-runtime-v11';
const EXPECTED_CACHES = [SHELL_CACHE, RUNTIME_CACHE];

const SHELL_ASSETS = [
  '/static/css/tailwind.generated.css',
  '/static/css/app.css',
  '/static/css/display-board.css',
  '/static/css/display-legacy.css',
  '/static/css/custom-display-theme.css',
  '/static/css/display-controls.css',
  '/static/css/fonts.css',
  '/static/fonts/ibm-plex-sans-arabic-400-arabic.woff2',
  '/static/fonts/ibm-plex-sans-arabic-700-arabic.woff2',
  '/static/js/display-controls.js',
  '/static/js/display-theme.js',
  '/static/js/occasion-theme.js',
  '/static/css/custom-occasion-theme.css',
  '/static/js/display.min.js',
  '/static/js/display-sw-register.js',
];

function isAssetUrl(url) {
  if (/^\/(?:api|dashboard|admin|cpanel-123)\//.test(url.pathname)) return false;
  return url.pathname.startsWith('/static/') || url.pathname.startsWith('/media/') ||
    /\.(png|jpe?g|webp|gif|svg|css|js|woff2?)$/i.test(url.pathname);
}

function displayCacheKey(url) {
  if (url.searchParams.has('preview')) return '';
  const shortLink = /^\/s\/([a-z\d]{6,32})\/?$/i.exec(url.pathname);
  if (shortLink) return url.origin + '/s/' + shortLink[1] + '/' + url.search;
  return url.pathname === '/' && url.searchParams.get('token') ? url.origin + '/' + url.search : '';
}

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(SHELL_CACHE).then((cache) => Promise.all(
    SHELL_ASSETS.map((asset) => cache.add(asset).catch(() => undefined))
  )).catch(() => undefined).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys
    .filter((key) => key.startsWith('school-display-') && EXPECTED_CACHES.indexOf(key) === -1)
    .map((key) => caches.delete(key))
  )).then(() => caches.open(RUNTIME_CACHE)).then((cache) => cache.keys().then((keys) => {
    // Older workers cached arbitrary navigations, including previews. Keep only
    // real display URLs and assets in the runtime cache.
    return Promise.all(keys.map((key) => {
      const url = new URL(key.url);
      if (url.origin === self.location.origin && !isAssetUrl(url) && !displayCacheKey(url)) {
        return cache.delete(key);
      }
    }));
  })).catch(() => undefined).then(() => self.clients.claim()));
});

function dropSupersededVersions(cache, request) {
  const keepPath = new URL(request.url).pathname;
  return cache.keys().then((keys) => Promise.all(keys.map((key) => {
    if (key.url !== request.url && new URL(key.url).pathname === keepPath) return cache.delete(key);
  })));
}

function handleAsset(request) {
  return caches.open(RUNTIME_CACHE).catch(() => null).then((cache) => {
    const exact = cache && cache.match(request).catch(() => undefined);
    return Promise.resolve(exact).then((cached) => {
      if (cached) return cached;
      return fetch(request).then((response) => {
        if (!cache || (!response.ok && response.type !== 'opaque')) return response;
        // A cache quota or storage error must not discard a good response.
        return cache.put(request, response.clone())
          .then(() => dropSupersededVersions(cache, request))
          .catch(() => undefined)
          .then(() => response);
      }).catch((offline) => {
        const older = cache && cache.match(request, { ignoreSearch: true }).catch(() => undefined);
        return Promise.resolve(older)
          .then((version) => version || caches.match(request, { ignoreSearch: true, cacheName: SHELL_CACHE }))
          .then((version) => {
            if (version) return version;
            throw offline;
          });
      });
    });
  });
}

function parseByteRange(value, size) {
  const match = /^bytes=(\d*)-(\d*)$/i.exec(String(value || '').trim());
  if (!match || !size || (!match[1] && !match[2])) return null;
  let start = match[1] ? parseInt(match[1], 10) : null;
  let end = match[2] ? parseInt(match[2], 10) : null;
  if (start === null && end !== null) {
    start = size - Math.min(size, end);
    end = size - 1;
  } else {
    start = start === null ? 0 : start;
    end = end === null ? size - 1 : Math.min(end, size - 1);
  }
  if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || start > end || start >= size) return null;
  return { start, end };
}

function handleRangeRequest(request) {
  return caches.open(RUNTIME_CACHE).then((cache) => cache.match(request.url)).then((cached) => {
    if (!cached || cached.type === 'opaque') return;
    return cached.arrayBuffer().then((buffer) => {
      const range = parseByteRange(request.headers.get('range'), buffer.byteLength);
      if (!range) return;
      const headers = new Headers(cached.headers);
      headers.delete('Content-Encoding');
      headers.set('Accept-Ranges', 'bytes');
      headers.set('Content-Range', 'bytes ' + range.start + '-' + range.end + '/' + buffer.byteLength);
      headers.set('Content-Length', String(range.end - range.start + 1));
      return new Response(buffer.slice(range.start, range.end + 1), {
        status: 206, statusText: 'Partial Content', headers,
      });
    });
  }).catch(() => undefined).then((partial) => partial || fetch(request));
}

function offlineBoardUnavailable() {
  return new Response('<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    '<title>شاشة العرض دون اتصال</title>' +
    '<body style="background:#0b1726;color:white;font:1.2rem sans-serif;margin:12vh 8vw">' +
    '<h1>بانتظار اتصال شاشة العرض</h1>' +
    '<p>لا توجد نسخة محفوظة لهذه الشاشة. بعد عودة الاتصال، افتح رابط الشاشة واتركها حتى يظهر جدول المدرسة.</p>' +
    '<p><a href="" style="color:#9ceadf">إعادة المحاولة</a></p></body></html>', {
    headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' },
  });
}

function saveDisplayPage(cache, key, response) {
  if (!cache || !response.ok || response.headers.get('X-School-Display') !== 'board') return Promise.resolve();
  return cache.put(key, response.clone()).catch(() => undefined);
}

function handleNavigation(request, key) {
  return caches.open(RUNTIME_CACHE).catch(() => null).then((cache) => {
    const readSaved = () => {
      if (!cache) return Promise.resolve();
      // The exact request is a fallback for boards saved by earlier workers.
      return cache.match(key).then((saved) => saved || cache.match(request)).catch(() => undefined);
    };
    return fetch(request).then((response) => {
      if (response.status >= 500) return readSaved().then((saved) => saved || response);
      const gone = response.status === 402 || response.status === 403 || response.status === 404;
      if (cache && gone) {
        return Promise.all([cache.delete(key), cache.delete(request)]).catch(() => undefined).then(() => response);
      }
      return saveDisplayPage(cache, key, response).then(() => response);
    }).catch(() => readSaved().then((saved) => saved || offlineBoardUnavailable()));
  });
}

function cacheDisplayPage(rawUrl) {
  let url;
  try {
    url = new URL(rawUrl, self.location.origin);
  } catch (_) {
    return;
  }
  const key = url.origin === self.location.origin && displayCacheKey(url);
  if (!key) return;
  const request = new Request(url.href, { credentials: 'omit' });
  return fetch(request).then((response) => caches.open(RUNTIME_CACHE)
    .then((cache) => saveDisplayPage(cache, key, response)));
}

function cacheDisplayMedia(rawUrls) {
  const urls = Array.isArray(rawUrls) ? rawUrls.slice(0, 48) : [];
  return caches.open(RUNTIME_CACHE).then((cache) => Promise.all(urls.map((rawUrl) => {
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
    return cache.match(request).then((saved) => {
      if (saved) return;
      return fetch(request).then((response) => {
        if (response.ok || response.type === 'opaque') return cache.put(request, response.clone());
      });
    }).catch(() => undefined);
  })));
}

self.addEventListener('message', (event) => {
  const data = event.data || {};
  if (data.type === 'CACHE_DISPLAY_PAGE') {
    event.waitUntil(Promise.resolve(cacheDisplayPage(data.url)).catch(() => undefined));
  } else if (data.type === 'CACHE_DISPLAY_MEDIA') {
    event.waitUntil(cacheDisplayMedia(data.urls).catch(() => undefined));
  }
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  let url;
  try {
    url = new URL(request.url);
  } catch (_) {
    return;
  }

  if (url.origin !== self.location.origin) {
    if (request.destination === 'image' || request.destination === 'audio') {
      event.respondWith(request.headers.has('range') ? handleRangeRequest(request) : handleAsset(request));
    }
    return;
  }
  if (isAssetUrl(url)) {
    if (request.headers.has('range')) {
      event.respondWith(handleRangeRequest(request));
    } else {
      event.respondWith(handleAsset(request));
    }
    return;
  }

  const key = displayCacheKey(url);
  if (key && request.mode === 'navigate') event.respondWith(handleNavigation(request, key));
});
