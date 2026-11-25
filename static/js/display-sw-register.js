/**
 * Service worker registration for the display page.
 *
 * Lives in its own file rather than inline so the page satisfies a
 * `script-src 'self'` Content-Security-Policy with no nonce or hash needed.
 */
(function () {
    "use strict";

    if (!("serviceWorker" in navigator)) {
        return;
    }

    document.addEventListener("DOMContentLoaded", function () {
        if (document.body.getAttribute("data-preview-mode") === "1") return;
        navigator.serviceWorker.register("/sw.js", { scope: "/" }).then(function () {
            // The first navigation happens before a newly-installed worker can
            // control it. Cache the exact display URL now so one successful
            // online visit is enough for a later offline reboot.
            return navigator.serviceWorker.ready.then(function (registration) {
                var worker = navigator.serviceWorker.controller || registration.active;
                if (!worker) return;
                worker.postMessage({ type: "CACHE_DISPLAY_PAGE", url: window.location.href });
            });
        }).catch(function () {
            // A screen that cannot register the worker still displays normally.
        });
    });
})();
