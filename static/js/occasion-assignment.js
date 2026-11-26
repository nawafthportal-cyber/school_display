document.addEventListener("change", (event) => {
  const field = event.target;
  if (!(field instanceof HTMLSelectElement)) return;
  if (field.id === "occasionApplyScreen") {
    updateOccasionApplyScreen(field);
    return;
  }

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
  current.querySelector("[data-assigned-connection]").textContent = option.dataset.connectionMessage || "";
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

function updateOccasionApplyScreen(screen) {
  const form = screen.form;
  const status = form.querySelector("[data-occasion-connection]");
  status.textContent = screen.selectedOptions[0]?.dataset.connectionMessage || "";
  form.querySelector('button[type="submit"]').disabled = !screen.value;
}

document.addEventListener("click", (event) => {
  const dialog = document.getElementById("occasionApplyDialog");
  const close = event.target.closest?.("[data-occasion-apply-close]");
  if (close && dialog?.contains(close)) {
    dialog.close();
    return;
  }

  const link = event.target.closest?.("[data-occasion-apply]");
  if (!link || typeof dialog?.showModal !== "function" || event.defaultPrevented ||
      event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  dialog.querySelector("[data-occasion-selected-name]").textContent = link.dataset.occasionName;

  const form = dialog.querySelector("form");
  if (form) {
    const screen = form.elements.namedItem("screen");
    const current = document.querySelector('#occasion-assignment select[name="screen"]');
    form.elements.namedItem("occasion").value = link.dataset.occasionApply;
    screen.value = current.value || (screen.options.length === 2 ? screen.options[1].value : "");
    updateOccasionApplyScreen(screen);
  }

  dialog.showModal();
  dialog.querySelector("select")?.focus();
});
