export const $ = (id) => document.getElementById(id);

export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function setMetricTone(id, tone = "") {
  const el = $(id);
  const card = el ? el.closest(".metric-card") : null;
  if (!card) return;
  card.classList.remove("good", "warn", "risk");
  if (tone) card.classList.add(tone);
}
