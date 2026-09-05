"use strict";

const refreshForm = document.getElementById("run-refresh-form");
const refreshSeconds = Number(refreshForm.dataset.refreshSeconds);
if (Number.isInteger(refreshSeconds) && refreshSeconds > 0 && refreshSeconds <= 900) {
  const refreshTimer = window.setTimeout(() => window.location.reload(), refreshSeconds * 1000);
  // Let operators edit even a one-second interval without losing their input.
  refreshForm.addEventListener("focusin", () => {
    window.clearTimeout(refreshTimer);
    document.getElementById("refresh-status").textContent =
      "Auto-refresh is paused while editing. Save to apply the refresh rate.";
  }, { once: true });
}
