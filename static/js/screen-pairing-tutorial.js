(function () {
  "use strict";
  var tutorial = document.querySelector("[data-screen-tutorial]");
  if (!tutorial) return;
  var video = tutorial.querySelector("video");
  var status = tutorial.querySelector("[data-tutorial-status]");
  var pendingSeek = null;

  document.querySelectorAll("[data-pairing-video-open]").forEach(function (link) {
    link.addEventListener("click", function (event) {
      event.preventDefault();
      var guide = document.querySelector("#pairingGuideDialog[open]");
      if (guide) {
        var close = guide.querySelector("[data-pairing-guide-close]");
        if (close) close.click();
      }
      tutorial.open = true;
      var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      tutorial.scrollIntoView({ behavior: reduceMotion ? "instant" : "smooth", block: "start" });
    });
  });

  function showStatus(message) {
    status.textContent = message;
    status.hidden = !message;
  }

  video.addEventListener("loadedmetadata", function () {
    if (pendingSeek !== null) {
      video.currentTime = Math.min(pendingSeek, video.duration || pendingSeek);
      pendingSeek = null;
    }
  });
  video.addEventListener("error", function () {
    showStatus("تعذر تحميل الفيديو. يمكنك فتحه من الرابط أدناه أو متابعة الخطوات النصية.");
  });
  video.addEventListener("playing", function () { showStatus(""); });

  tutorial.querySelectorAll("[data-tutorial-seek]").forEach(function (button) {
    button.addEventListener("click", function () {
      var seconds = Number(button.getAttribute("data-tutorial-seek"));
      if (!Number.isFinite(seconds) || seconds < 0) return;
      if (video.readyState >= 1) {
        video.currentTime = Math.min(seconds, video.duration || seconds);
      } else {
        pendingSeek = seconds;
      }
      // Playback follows the user's chapter click; never autoplay on page load.
      var playing = video.play();
      if (playing && typeof playing.catch === "function") {
        playing.catch(function () {
          if (!tutorial.open) return;
          showStatus(video.error
            ? "تعذر تحميل الفيديو. يمكنك فتحه من الرابط أدناه أو متابعة الخطوات النصية."
            : "اضغط زر التشغيل داخل الفيديو للمتابعة.");
        });
      }
      video.focus({ preventScroll: true });
    });
  });
  tutorial.addEventListener("toggle", function () {
    if (!tutorial.open) video.pause();
  });
})();
