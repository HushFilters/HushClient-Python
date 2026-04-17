function setStatus(message, variant = "") {
  const el = document.getElementById("sync-status");
  el.textContent = message;
  el.className = `status ${variant}`.trim();
}

function prettyJson(value) {
  return JSON.stringify(value, null, 2);
}

function setMetric(id, value) {
  document.getElementById(id).textContent = String(value);
}

async function refreshLoadedCount() {
  try {
    const response = await fetch("/stats");
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      return;
    }
    setMetric("loaded-count", payload.filter_count || 0);
  } catch (_err) {
    // Leave the existing metric value in place if stats cannot be fetched.
  }
}

function renderOutput(payload) {
  const output = document.getElementById("sync-output");
  const lines = [];

  if (payload.detail) {
    lines.push(`detail: ${payload.detail}`);
  }
  if (payload.manifest_path) {
    lines.push(`manifest_path: ${payload.manifest_path}`);
  }
  if (Array.isArray(payload.logs) && payload.logs.length > 0) {
    lines.push("");
    lines.push(...payload.logs);
  }

  output.textContent = lines.length > 0 ? lines.join("\n") : prettyJson(payload);
}

function setButtonsDisabled(disabled) {
  document.getElementById("apply-button").disabled = disabled;
  document.getElementById("sync-button").disabled = disabled;
  document.getElementById("manifest-button").disabled = disabled;
  document.getElementById("reload-button").disabled = disabled;
}

async function runOperation({ endpoint, inProgress, success, failure, onSuccess }) {
  setButtonsDisabled(true);
  setStatus(inProgress);
  document.getElementById("sync-output").textContent = `Starting ${inProgress.toLowerCase()}`;

  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const payload = await response.json().catch(() => ({ detail: "Invalid JSON response" }));

    if (!response.ok || payload.success === false) {
      setStatus(`${failure}${response.ok ? "" : ` (${response.status})`}`, "bad");
      setMetric("downloaded-count", payload.downloaded?.length || 0);
      renderOutput(payload);
      return;
    }

    setStatus(success, "ok");
    if (typeof onSuccess === "function") {
      onSuccess(payload);
    }
    renderOutput(payload);
  } catch (err) {
    setStatus("Client error", "bad");
    renderOutput({ detail: err instanceof Error ? err.message : String(err) });
  } finally {
    setButtonsDisabled(false);
  }
}

window.addEventListener("DOMContentLoaded", () => {
  refreshLoadedCount();

  document.getElementById("apply-button").addEventListener("click", () => {
    setMetric("downloaded-count", 0);
    runOperation({
      endpoint: "/sync/apply",
      inProgress: "Running filter sync, manifest update, and reload...",
      success: "Filter sync, manifest update, and reload complete",
      failure: "Combined filter update failed",
      async onSuccess(payload) {
        setMetric("downloaded-count", payload.downloaded?.length || 0);
        await refreshLoadedCount();
      },
    });
  });

  document.getElementById("sync-button").addEventListener("click", () => {
    setMetric("downloaded-count", 0);
    runOperation({
      endpoint: "/sync/filters",
      inProgress: "Running filter sync...",
      success: "Filter sync complete",
      failure: "Filter sync failed",
      onSuccess(payload) {
        setMetric("downloaded-count", payload.downloaded.length);
      },
    });
  });

  document.getElementById("manifest-button").addEventListener("click", () => {
    runOperation({
      endpoint: "/sync/manifest",
      inProgress: "Updating manifest...",
      success: "Manifest update complete",
      failure: "Manifest update failed",
    });
  });

  document.getElementById("reload-button").addEventListener("click", () => {
    runOperation({
      endpoint: "/sync/reload",
      inProgress: "Reloading filters...",
      success: "Reload complete",
      failure: "Reload failed",
      async onSuccess(_payload) {
        await refreshLoadedCount();
      },
    });
  });
});
