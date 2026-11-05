(function () {
  "use strict";
  var colors = ["background_color", "card_color", "text_color", "heading_color", "accent_color"];
  var fonts = {ibm: '"IBM Plex Sans Arabic", sans-serif', system: 'system-ui, "Segoe UI", sans-serif', arial: 'Arial, sans-serif'};
  var properties = ["--bg-deep", "--glass-surface", "--board-surface", "--board-surface-strong", "--accent-main", "--accent-sub", "--mesh1", "--mesh2", "--custom-text", "--custom-heading", "--custom-font", "--custom-radius", "--custom-scale"];
  function read(id) {
    var node = document.getElementById(id);
    try { return node ? JSON.parse(node.textContent) : null; } catch (e) { return null; }
  }
  function valid(theme) {
    if (!theme || typeof theme !== "object") return false;
    return colors.every(function (key) { return /^#[0-9a-f]{6}$/i.test(theme[key] || ""); });
  }
  function apply(target, theme) {
    if (!target) return;
    properties.forEach(function (property) { target.style.removeProperty(property); });
    target.removeAttribute("data-custom-display-theme");
    target.removeAttribute("data-custom-background");
    if (!valid(theme)) return;
    var values = {
      "--bg-deep": theme.background_color, "--glass-surface": theme.card_color,
      "--board-surface": theme.card_color, "--board-surface-strong": theme.card_color,
      "--custom-text": theme.text_color, "--custom-heading": theme.heading_color,
      "--accent-main": theme.accent_color, "--accent-sub": theme.accent_color,
      "--custom-font": fonts[theme.font] || fonts.ibm,
      "--custom-radius": Math.max(0, Math.min(32, parseInt(theme.card_radius, 10) || 0)) + "px",
      "--custom-scale": String(Math.max(90, Math.min(115, parseInt(theme.font_scale, 10) || 100)) / 100),
      "--mesh1": "transparent", "--mesh2": "transparent"
    };
    Object.keys(values).forEach(function (key) { target.style.setProperty(key, values[key]); });
    target.setAttribute("data-custom-display-theme", String(theme.id || "preview"));
    target.setAttribute("data-custom-background", ["solid", "dots", "gradient"].indexOf(theme.background_style) >= 0 ? theme.background_style : "gradient");
  }
  window.SchoolDisplayThemes = {read: read, apply: apply, valid: valid};
}());
