(function () {
  "use strict";
  var renderer = window.SchoolDisplayThemes;
  if (!renderer) return;
  var library = renderer.read("themeLibraryData") || [];
  document.querySelectorAll(".theme-board[data-theme-id]").forEach(function (node) {
    var theme = library.find(function (item) { return item && String(item.id) === node.dataset.themeId; });
    renderer.apply(node, theme);
  });
  var form = document.getElementById("themeEditorForm");
  if (!form) return;
  var preview = document.getElementById("themeLivePreview");
  var status = document.getElementById("themeContrastStatus");
  var save = document.getElementById("saveThemeButton");
  var colorFields = ["background_color", "card_color", "text_color", "heading_color", "accent_color"];
  var presets = {indigo: "#A5B4FC", emerald: "#6EE7B7", rose: "#F9A8D4", cyan: "#67E8F9", amber: "#FDE68A", orange: "#FDBA74", violet: "#C4B5FD"};
  function ratio(a, b) {
    function light(hex) {
      var values = [1, 3, 5].map(function (i) { var x = parseInt(hex.slice(i, i + 2), 16) / 255; return x <= 0.04045 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); });
      return values[0] * 0.2126 + values[1] * 0.7152 + values[2] * 0.0722;
    }
    var first = light(a), second = light(b);
    return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05);
  }
  function update() {
    var theme = {};
    colorFields.concat(["font", "font_scale", "card_radius", "background_style"]).forEach(function (name) { theme[name] = form.elements[name].value; });
    renderer.apply(preview, theme);
    var readable = renderer.valid(theme);
    if (readable) {
      ["background_color", "card_color"].forEach(function (background) {
        if (ratio(theme.text_color, theme[background]) < 4.5 || ratio(theme.heading_color, theme[background]) < 3 || ratio(theme.accent_color, theme[background]) < 3) readable = false;
      });
    }
    colorFields.forEach(function (name) { var label = document.querySelector('[data-color-value="' + name + '"]'); if (label) label.textContent = theme[name].toUpperCase(); });
    status.textContent = readable ? "✓ تباين جيد — الألوان واضحة على الخلفية والبطاقات" : "اختر ألوانًا أكثر تباينًا للنصوص والعناوين واللون الرئيسي حتى تصبح القراءة واضحة.";
    status.dataset.valid = readable ? "1" : "0";
    save.disabled = !readable;
    return readable;
  }
  form.addEventListener("input", update);
  form.addEventListener("change", update);
  form.elements.base_theme.addEventListener("change", function () {
    form.elements.background_color.value = "#07111f";
    form.elements.card_color.value = "#142033";
    form.elements.text_color.value = "#e2e8f0";
    form.elements.heading_color.value = "#ffffff";
    form.elements.accent_color.value = presets[this.value] || presets.indigo;
    update();
  });
  form.addEventListener("submit", function (event) { if (!update()) { event.preventDefault(); status.scrollIntoView({block: "center", behavior: "smooth"}); } });
  update();
}());
