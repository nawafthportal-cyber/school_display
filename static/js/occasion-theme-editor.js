(function () {
  "use strict";
  var renderer = window.SchoolOccasionThemes;
  var form = document.getElementById("occasionThemeForm");
  if (!renderer || !form) return;
  var original = renderer.read("occasionEditorData");
  var preview = document.getElementById("occasionLivePreview");
  var status = document.getElementById("occasionContrastStatus");
  var save = document.getElementById("saveOccasionTheme");
  function light(hex) {
    return [1, 3, 5].map(function (i) { var x = parseInt(hex.slice(i, i + 2), 16) / 255; return x <= 0.04045 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); })
      .reduce(function (sum, x, i) { return sum + x * [0.2126, 0.7152, 0.0722][i]; }, 0);
  }
  function contrast(a, b) { var x = light(a), y = light(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); }
  function update() {
    var theme = {}, values = {};
    ["name", "tagline", "deep", "accent", "highlight", "soft", "pattern"].forEach(function (key) { theme[key] = form.elements[key].value; });
    values[original.key] = theme;
    renderer.apply(preview, original.key, values);
    preview.querySelector("[data-occasion-name]").textContent = theme.name || "اسم الثيم";
    preview.querySelector("[data-occasion-tagline]").textContent = theme.tagline;
    var valid = contrast(theme.soft, theme.deep) >= 4.5 && contrast(theme.highlight, theme.deep) >= 3;
    status.dataset.valid = valid ? "1" : "0";
    status.textContent = valid ? "✓ تباين جيد — النصوص والعناوين واضحة على الخلفية" : "اختر لون نصوص وعناوين أوضح على الخلفية حتى يسهل قراءتها.";
    save.disabled = !valid;
    return valid;
  }
  form.addEventListener("input", update);
  form.addEventListener("change", update);
  form.addEventListener("submit", function (event) { if ((!event.submitter || event.submitter.value !== "reset") && !update()) event.preventDefault(); });
  update();
}());
