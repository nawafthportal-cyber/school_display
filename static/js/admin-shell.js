(function () {
  "use strict";

  // Plain "ar-SA" resolves to the Umm al-Qura calendar with Arabic-Indic
  // digits, which contradicted every other date in the console (Gregorian,
  // Y-m-d, Latin digits). Pin the calendar and numbering system explicitly.
  var LOCALE = "ar-SA-u-ca-gregory-nu-latn";

  function initializeClock() {
    var timeElement = document.getElementById("current-time");
    var dateElement = document.getElementById("current-date");
    if (!timeElement || !dateElement) return;
    function update() {
      var now = new Date();
      timeElement.textContent = now.toLocaleTimeString(LOCALE, {
        hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: true
      });
      dateElement.textContent = now.toLocaleDateString(LOCALE, {
        weekday: "long", year: "numeric", month: "long", day: "numeric"
      });
    }
    update();
    window.setInterval(update, 1000);
  }

  // The active item can sit below the fold in a long sidebar; bring it into
  // view so the operator always sees where they are.
  function revealActiveNavItem() {
    var active = document.querySelector(".admin-nav-link.is-active");
    if (!active || typeof active.scrollIntoView !== "function") return;
    var scroller = active.closest(".overflow-y-auto");
    if (!scroller || scroller.scrollHeight <= scroller.clientHeight) return;
    active.scrollIntoView({ block: "nearest" });
  }

  function initializeSidebar() {
    var sidebar = document.getElementById("adminSidebar");
    var overlay = document.getElementById("adminSidebarOverlay");
    var openButton = document.getElementById("adminSidebarOpenBtn");
    var closeButton = document.getElementById("adminSidebarCloseBtn");
    if (!sidebar || !overlay) return;

    function isDesktop() { return window.matchMedia("(min-width: 1024px)").matches; }
    function setOpen(open) {
      var effectiveOpen = isDesktop() || Boolean(open);
      sidebar.classList.toggle("translate-x-full", !effectiveOpen);
      sidebar.classList.toggle("translate-x-0", effectiveOpen);
      sidebar.inert = !effectiveOpen;
      sidebar.setAttribute("aria-hidden", String(!effectiveOpen));
      overlay.classList.toggle("hidden", isDesktop() || !effectiveOpen);
      if (openButton) openButton.setAttribute("aria-expanded", String(!isDesktop() && effectiveOpen));
    }
    if (openButton) openButton.addEventListener("click", function () {
      setOpen(true);
      if (closeButton) closeButton.focus();
    });
    if (closeButton) closeButton.addEventListener("click", function () {
      setOpen(false);
      if (openButton) openButton.focus();
    });
    overlay.addEventListener("click", function () { setOpen(false); });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Tab" && !isDesktop() && openButton && openButton.getAttribute("aria-expanded") === "true") {
        var focusable = Array.prototype.filter.call(sidebar.querySelectorAll('a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex="0"]'), function (node) { return node.getClientRects().length > 0; });
        var first = focusable[0], last = focusable[focusable.length - 1];
        if (event.shiftKey && (document.activeElement === first || !sidebar.contains(document.activeElement))) { event.preventDefault(); if (last) last.focus(); }
        else if (!event.shiftKey && (document.activeElement === last || !sidebar.contains(document.activeElement))) { event.preventDefault(); if (first) first.focus(); }
      }
      if (event.key !== "Escape" || isDesktop()) return;
      setOpen(false);
      if (openButton) openButton.focus();
    });
    // Only react to a real breakpoint change; a mobile keyboard opening fires
    // "resize" too and used to slam the drawer shut mid-interaction.
    var wasDesktop = isDesktop();
    window.addEventListener("resize", function () {
      var nowDesktop = isDesktop();
      if (nowDesktop === wasDesktop) return;
      wasDesktop = nowDesktop;
      setOpen(false);
    });
    setOpen(false);
  }

  function initializeGlobalSearch() {
    var searchInput = document.getElementById("adminGlobalSearch");
    if (!searchInput) return;
    document.addEventListener("keydown", function (event) {
      if ((event.ctrlKey || event.metaKey) && String(event.key).toLowerCase() === "k") {
        event.preventDefault();
        searchInput.focus();
        searchInput.select();
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initializeClock();
    initializeSidebar();
    initializeGlobalSearch();
    revealActiveNavItem();
    var animated = document.querySelector(".animate-fade-in");
    if (animated) animated.style.animation = "fadeIn 0.5s ease-out";
    var printButton = document.getElementById("adminPrintBtn");
    var reloadButton = document.getElementById("adminReloadBtn");
    if (printButton) printButton.addEventListener("click", function () { window.print(); });
    if (reloadButton) reloadButton.addEventListener("click", function () { window.location.reload(); });
  });
}());
