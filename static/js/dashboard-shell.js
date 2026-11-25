(function () {
  "use strict";

  function dismissToast(toast) {
    if (!toast || toast.dataset.dismissed === "1") return;
    toast.dataset.dismissed = "1";
    toast.classList.replace("toast-in", "toast-out");
    window.setTimeout(function () { toast.remove(); }, 220);
  }

  function initializeToasts() {
    var stack = document.getElementById("toastStack");
    if (!stack) return;
    stack.addEventListener("click", function (event) {
      var button = event.target.closest("[data-toast-close]");
      if (button) dismissToast(button.closest("[data-toast]"));
    });
    stack.querySelectorAll("[data-toast]").forEach(function (toast) {
      var tags = (toast.getAttribute("data-tags") || "").toLowerCase();
      if (!tags.includes("error") && !tags.includes("danger")) {
        window.setTimeout(function () { dismissToast(toast); }, 5000);
      }
    });
  }

  function initializeSidebar() {
    var sidebar = document.getElementById("dashboardSidebar");
    var overlay = document.getElementById("dashboardMobileOverlay");
    var toggle = document.getElementById("dashboardSidebarToggleBtn");
    var openIcon = document.getElementById("dashboardSidebarOpenIcon");
    var closeIcon = document.getElementById("dashboardSidebarCloseIcon");
    if (!sidebar || !overlay || !toggle) return;
    var mobileOpen = false;
    var lockedElements = [];
    var bodyOverflow = "";

    function isDesktop() {
      return window.matchMedia("(min-width: 1024px)").matches;
    }

    function setOpen(open) {
      var desktop = isDesktop();
      var wasOpen = mobileOpen;
      mobileOpen = !desktop && Boolean(open);
      var effectiveOpen = desktop || mobileOpen;
      if (!effectiveOpen && sidebar.contains(document.activeElement)) toggle.focus();
      sidebar.inert = !effectiveOpen;
      if (effectiveOpen) sidebar.removeAttribute("aria-hidden");
      else sidebar.setAttribute("aria-hidden", "true");
      sidebar.classList.toggle("translate-x-full", !effectiveOpen);
      sidebar.classList.toggle("translate-x-0", effectiveOpen);
      overlay.classList.toggle("show", mobileOpen);
      toggle.setAttribute("aria-expanded", String(mobileOpen));
      toggle.setAttribute("aria-label", mobileOpen ? "إغلاق قائمة التنقل" : "فتح قائمة التنقل");
      if (openIcon) openIcon.classList.toggle("hidden", mobileOpen);
      if (closeIcon) closeIcon.classList.toggle("hidden", !mobileOpen);
      document.body.classList.toggle("dashboard-sidebar-open", mobileOpen);

      if (mobileOpen && !wasOpen) {
        bodyOverflow = document.body.style.overflow;
        document.body.style.overflow = "hidden";
        lockedElements = Array.prototype.map.call(document.querySelectorAll(
          ".dashboard-main, .dashboard-topbar-actions, .dashboard-whatsapp-float, .dashboard-skip-link, .dashboard-brand-link"
        ), function (element) {
          var previous = element.inert;
          element.inert = true;
          return { element: element, inert: previous };
        });
        var currentLink = sidebar.querySelector("[aria-current]") || sidebar.querySelector("a[href]");
        if (currentLink) {
          currentLink.scrollIntoView({ block: "nearest" });
          currentLink.focus({ preventScroll: true });
        }
      } else if (!mobileOpen && wasOpen) {
        document.body.style.overflow = bodyOverflow;
        lockedElements.forEach(function (entry) { entry.element.inert = entry.inert; });
        lockedElements = [];
        if (!desktop) toggle.focus({ preventScroll: true });
      }
    }

    toggle.addEventListener("click", function () {
      setOpen(!sidebar.classList.contains("translate-x-0"));
    });
    overlay.addEventListener("click", function () { setOpen(false); });
    document.addEventListener("keydown", function (event) {
      if (!mobileOpen) return;
      if (event.key === "Escape") {
        event.preventDefault();
        setOpen(false);
      } else if (event.key === "Tab") {
        var links = Array.prototype.filter.call(sidebar.querySelectorAll(
          "a[href], button:not([disabled]), [tabindex]:not([tabindex='-1'])"
        ), function (element) { return element.getClientRects().length > 0; });
        var last = links[links.length - 1] || toggle;
        if (event.shiftKey && document.activeElement === toggle) {
          event.preventDefault(); last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault(); toggle.focus();
        }
      }
    });
    var desktopLayout = isDesktop();
    window.addEventListener("resize", function () {
      if (isDesktop() === desktopLayout) return;
      desktopLayout = isDesktop();
      setOpen(false);
    });
    setOpen(false);
  }

  function initializeResponsiveTables() {
    document.querySelectorAll(".dashboard-main table").forEach(function (table) {
      if (!table.parentElement) return;
      var wrapper = table.closest(".dashboard-table-scroll");
      if (!wrapper) {
        wrapper = document.createElement("div");
        wrapper.className = "dashboard-table-scroll";
        table.parentElement.insertBefore(wrapper, table);
        wrapper.appendChild(table);
      }
      if (!wrapper.hasAttribute("role")) wrapper.setAttribute("role", "region");
      if (!wrapper.hasAttribute("aria-label") && !wrapper.hasAttribute("aria-labelledby")) {
        wrapper.setAttribute("aria-label", "جدول قابل للتمرير أفقيًا");
      }
      if (!wrapper.hasAttribute("tabindex")) wrapper.setAttribute("tabindex", "0");
    });
  }

  function initializeSchoolSwitcher() {
    var form = document.getElementById("schoolSwitcherForm");
    var select = document.getElementById("schoolSwitcherSelect");
    if (!form || !select) return;
    var initialValue = select.value;
    select.addEventListener("change", function () {
      if (select.value && select.value !== initialValue) form.submit();
    });
  }

  window.showDashboardToast = function (title, message, type) {
    var stack = document.getElementById("dashboardRuntimeToastStack");
    if (!stack) {
      stack = document.createElement("div");
      stack.id = "dashboardRuntimeToastStack";
      stack.className = "dashboard-toast-stack space-y-3";
      stack.setAttribute("aria-live", "polite");
      document.body.appendChild(stack);
    }

    var isError = type === "error";
    var toast = document.createElement("div");
    toast.className = "toast-in pointer-events-auto rounded-2xl shadow-xl border overflow-hidden glass";
    toast.innerHTML =
      '<div class="flex items-start gap-3 p-4 border-r-4 ' +
      (isError ? "border-rose-500 bg-rose-50/70" : "border-emerald-500 bg-emerald-50/70") +
      '"><div class="flex-1 min-w-0"><div class="text-[13px] font-bold text-slate-800" data-toast-title></div>' +
      '<div class="mt-0.5 text-sm text-slate-600 leading-relaxed" data-toast-message></div></div>' +
      '<button type="button" class="p-1.5 rounded-lg text-slate-400 hover:text-slate-700" aria-label="إغلاق التنبيه">×</button></div>';
    toast.querySelector("[data-toast-title]").textContent = title || (isError ? "تنبيه" : "تم بنجاح");
    toast.querySelector("[data-toast-message]").textContent = message || "";
    toast.querySelector("button").addEventListener("click", function () { dismissToast(toast); });
    stack.appendChild(toast);
    if (!isError) window.setTimeout(function () { dismissToast(toast); }, 3500);
  };

  document.addEventListener("DOMContentLoaded", function () {
    initializeToasts();
    initializeSidebar();
    initializeResponsiveTables();
    initializeSchoolSwitcher();
  });
}());
