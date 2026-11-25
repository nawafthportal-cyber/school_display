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
  // Each starting palette follows the same background, card, text, heading and accent order.
  var presets = {
    indigo:  ["#101728", "#202C46", "#DFE7F7", "#F4F6FF", "#A5B4FC"],
    emerald: ["#082821", "#163D32", "#D9F3E8", "#FAFFF9", "#78DDB5"],
    rose:    ["#301827", "#482737", "#FAE5EF", "#FFF6FA", "#F9A8D4"],
    cyan:    ["#092633", "#163B49", "#DBF3F7", "#F2FDFF", "#75DAE6"],
    amber:   ["#FCF5E8", "#F3E5CC", "#45331F", "#533819", "#805516"],
    orange:  ["#321E16", "#4C3024", "#FBE9DA", "#FFF6EF", "#FFBA85"],
    violet:  ["#251D35", "#382C4D", "#F1E8FB", "#FCF7FF", "#D4B7FA"]
  };
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
    var palette = presets[this.value] || presets.indigo;
    colorFields.forEach(function (field, index) { form.elements[field].value = palette[index]; });
    update();
  });
  form.addEventListener("submit", function (event) { if (!update()) { event.preventDefault(); status.scrollIntoView({block: "center", behavior: "smooth"}); } });
  update();
}());
