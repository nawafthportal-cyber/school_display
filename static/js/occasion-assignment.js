document.addEventListener("change", (event) => {
  const field = event.target;
  if (!(field instanceof HTMLSelectElement)) return;

  const form = field.form;
  const assignment = form?.closest("#occasion-assignment");
  if (!assignment) return;
  if (field.name === "occasion") {
    form.dataset.preset = field.value ? "1" : "0";
    return;
  }
  if (field.name !== "screen") return;

  const option = field.selectedOptions[0];
  const current = assignment.querySelector(".occasion-assigned");
  const preview = current.querySelector("a");
  current.hidden = !field.value;
  current.querySelector("[data-assigned-screen]").textContent = option.dataset.name || "";
  current.querySelector("strong").textContent = option.dataset.label || "";
  preview.hidden = !option.dataset.preview;
  if (option.dataset.preview) preview.href = option.dataset.preview;
  else preview.removeAttribute("href");
  preview.dataset.previewName = option.dataset.name || "";
  if (form.dataset.preset !== "1") form.elements.occasion.value = option.dataset.key || "";

  document.querySelectorAll("[data-screen-link]").forEach((link) => {
    const url = new URL(link.href);
    if (field.value) url.searchParams.set("screen", field.value);
    else url.searchParams.delete("screen");
    link.href = url.href;
  });
});
