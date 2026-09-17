document.addEventListener("DOMContentLoaded", function () {
  const toggle = document.querySelector(".theme-toggle");
  if (!toggle) return;
  toggle.addEventListener("click", async function () {
    const next = (document.documentElement.dataset.theme || "light") === "dark" ? "light" : "dark";
    const form = new FormData();
    form.append("theme", next);
    const response = await fetch("/theme", { method: "POST", body: form });
    if (!response.ok) return;
    document.documentElement.dataset.theme = next;
    toggle.textContent = next === "dark" ? "☀️ Светлая" : "🌙 Тёмная";
  });
});