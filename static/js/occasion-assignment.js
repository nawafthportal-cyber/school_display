(function () {
  const form = document.querySelector("#occasion-assignment form");
  if (!form) return;
  const current = document.querySelector(".occasion-assigned");
  const preview = current.querySelector("a");
  let keepOccasion = form.dataset.preset === "1";
  form.elements.occasion.addEventListener("change", function () {
    keepOccasion = Boolean(this.value);
  });
  form.elements.screen.addEventListener("change", function () {
    const option = this.selectedOptions[0];
    current.hidden = !this.value;
    current.querySelector("[data-assigned-screen]").textContent = option.dataset.name || "";
    current.querySelector("strong").textContent = option.dataset.label || "";
    preview.hidden = !option.dataset.preview;
    if (option.dataset.preview) preview.href = option.dataset.preview;
    else preview.removeAttribute("href");
    preview.dataset.previewName = option.dataset.name || "";
    if (!keepOccasion) form.elements.occasion.value = option.dataset.key || "";
    document.querySelectorAll("[data-screen-link]").forEach(link => {
      const url = new URL(link.href);
      if (option.value) url.searchParams.set("screen", option.value);
      else url.searchParams.delete("screen");
      link.href = url.href;
    });
  });
}());
