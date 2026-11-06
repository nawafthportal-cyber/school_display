(function () {
  "use strict";
  var colors = ["deep", "accent", "highlight", "soft"];
  var patterns = {
    solid: "none",
    dots: "radial-gradient(var(--occasion-highlight) 1.5px, transparent 1.5px)",
    lines: "repeating-linear-gradient(45deg, transparent 0 22px, var(--occasion-highlight) 23px 25px)"
  };
  function read(id) {
    try { return JSON.parse(document.getElementById(id).textContent || "{}"); } catch (_) { return {}; }
  }
  function valid(value) {
    return value && colors.every(function (key) { return /^#[0-9a-f]{6}$/i.test(value[key] || ""); });
  }
  function selected(key, overrides) {
    var value = overrides && Object.prototype.hasOwnProperty.call(overrides, key) ? overrides[key] : null;
    return valid(value) ? value : null;
  }
  function meta(key, overrides, original) {
    var value = selected(key, overrides);
    return value ? Object.assign({}, original, {label: value.name, tagline: value.tagline}) : original;
  }
  function apply(target, key, overrides) {
    if (!target) return;
    var value = selected(key, overrides);
    colors.forEach(function (color) {
      if (value) target.style.setProperty("--occasion-" + color, value[color]);
      else target.style.removeProperty("--occasion-" + color);
    });
    if (value) {
      var rgb = [1, 3, 5].map(function (start) { return parseInt(value.accent.slice(start, start + 2), 16); });
      target.style.setProperty("--occasion-glow", "rgba(" + rgb.join(",") + ",0.34)");
    } else target.style.removeProperty("--occasion-glow");
    if (value && Object.prototype.hasOwnProperty.call(patterns, value.pattern)) {
      target.style.setProperty("--custom-occasion-pattern", patterns[value.pattern]);
    } else target.style.removeProperty("--custom-occasion-pattern");
  }
  window.SchoolOccasionThemes = {read: read, selected: selected, meta: meta, apply: apply};
}());
