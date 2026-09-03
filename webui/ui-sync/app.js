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

function formatScheduledTime(value) {
  if (!value) {
    return "Not scheduled";
  }
  return value.replace("T", " ");
}

function formatNextScheduledTime(value) {
  if (!value) {
    return "Not scheduled";
  }

  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return formatScheduledTime(value);
  }
  return `${formatScheduledTime(value)} · your time: ${parsed.toLocaleString()}`;
}

function formatDuration(startValue, endValue) {
  const start = Date.parse(startValue);
  const end = Date.parse(endValue);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) {
    return "—";
  }

  const seconds = Math.round((end - start) / 1000);
  if (seconds < 60) {
    return `${seconds}s`;
  }
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${seconds % 60}s`;
}

function appendHistoryList(parent, label, values) {
  if (!Array.isArray(values) || values.length === 0) {
    return;
  }

  const heading = document.createElement("strong");
  heading.textContent = `${label} (${values.length})`;
  parent.appendChild(heading);

  const list = document.createElement("ul");
  list.className = "history-file-list";
  values.forEach((value) => {
    const item = document.createElement("li");
    item.textContent = value;
    list.appendChild(item);
  });
  parent.appendChild(list);
}

function renderAutoUpdateHistory(history, activeRun = null) {
  const container = document.getElementById("auto-update-history");
  container.replaceChildren();

  const runs = Array.isArray(history) ? [...history] : [];
  if (activeRun) {
    runs.unshift(activeRun);
  }

  if (runs.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-history";
    empty.textContent = "No automatic update attempts have been recorded yet.";
    container.appendChild(empty);
    return;
  }

  runs.forEach((run) => {
    const item = document.createElement("article");
    item.className = "history-item";

    const header = document.createElement("div");
    header.className = "history-item__header";
    const title = document.createElement("div");
    const triggered = document.createElement("strong");
    triggered.textContent = formatScheduledTime(run.triggered_at);
    const timing = document.createElement("span");
    timing.textContent = run.status === "running"
      ? `In progress · ${formatDuration(run.triggered_at, run.completed_at)}`
      : `Completed ${formatScheduledTime(run.completed_at)} · ${formatDuration(run.triggered_at, run.completed_at)}`;
    title.append(triggered, timing);

    const badge = document.createElement("span");
    badge.className = `run-badge run-badge--${run.status || "failed"}`;
    badge.textContent = run.status || "unknown";
    header.append(title, badge);
    item.appendChild(header);

    const counts = document.createElement("p");
    counts.className = "history-item__counts";
    counts.textContent = `${run.downloaded?.length || 0} downloaded · ${run.redownloaded?.length || 0} refreshed · ${run.verified_existing?.length || 0} already current`;
    item.appendChild(counts);

    if (run.detail) {
      const detail = document.createElement("p");
      detail.className = "history-item__detail";
      detail.textContent = run.detail;
      item.appendChild(detail);
    }

    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = `View files and logs (${run.logs?.length || 0} lines)`;
    details.appendChild(summary);

    const detailBody = document.createElement("div");
    detailBody.className = "history-item__body";
    appendHistoryList(detailBody, "Downloaded", run.downloaded);
    appendHistoryList(detailBody, "Re-downloaded", run.redownloaded);
    appendHistoryList(detailBody, "Verified existing", run.verified_existing);
    if (Array.isArray(run.logs) && run.logs.length > 0) {
      const logs = document.createElement("pre");
      logs.className = "mono-surface history-logs";
      logs.textContent = run.logs.join("\n");
      detailBody.appendChild(logs);
    }
    details.appendChild(detailBody);
    item.appendChild(details);
    container.appendChild(item);
  });
}

let autoUpdateFormDirty = false;

function setAutoUpdateMessage(message, variant = "") {
  const element = document.getElementById("auto-update-message");
  element.textContent = message;
  element.className = `status ${variant}`.trim();
}

function syncAutoUpdateHourAvailability() {
  document.getElementById("auto-update-hour").disabled = !document.getElementById("auto-update-enabled").checked;
}

function renderAutoUpdateStatus(payload, applyFormValues = true) {
  const badge = document.getElementById("auto-update-badge");
  badge.textContent = payload.active ? "Running" : (payload.enabled ? "Enabled" : "Disabled");
  badge.className = `schedule-badge ${(payload.enabled || payload.active) ? "schedule-badge--enabled" : ""}`.trim();

  if (applyFormValues) {
    document.getElementById("auto-update-enabled").checked = payload.enabled === true;
    if (Number.isInteger(payload.hour)) {
      document.getElementById("auto-update-hour").value = String(payload.hour);
    }
    syncAutoUpdateHourAvailability();
  }

  document.getElementById("auto-update-next").textContent = formatNextScheduledTime(payload.next_update_at);
  document.getElementById("auto-update-timezone").textContent = payload.timezone || "—";
  document.getElementById("auto-update-current-time").textContent = formatScheduledTime(payload.current_time);
  const activeRun = payload.active ? {
    triggered_at: payload.active_since,
    completed_at: payload.current_time,
    status: "running",
    downloaded: [],
    redownloaded: [],
    verified_existing: [],
    logs: payload.live_logs || [],
  } : null;
  renderAutoUpdateHistory(payload.history, activeRun);
}

async function refreshAutoUpdateStatus() {
  try {
    const response = await fetch("/sync/auto-update", { cache: "no-store" });
    const payload = await readResponsePayload(response);
    if (!response.ok) {
      setAutoUpdateMessage(payload.detail || `Could not load schedule (${response.status})`, "bad");
      return;
    }
    renderAutoUpdateStatus(payload, !autoUpdateFormDirty);
  } catch (err) {
    setAutoUpdateMessage(err instanceof Error ? err.message : String(err), "bad");
  }
}

async function saveAutoUpdateSchedule(event) {
  event.preventDefault();
  const enabled = document.getElementById("auto-update-enabled").checked;
  const hourInput = document.getElementById("auto-update-hour");
  const hour = Number.parseInt(hourInput.value, 10);
  if (enabled && (!Number.isInteger(hour) || hour < 0 || hour > 23)) {
    setAutoUpdateMessage("Enter an hour from 0 through 23.", "bad");
    return;
  }

  const saveButton = document.getElementById("auto-update-save");
  saveButton.disabled = true;
  setAutoUpdateMessage("Saving schedule…");
  try {
    const response = await fetch("/sync/auto-update", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled, hour: Number.isInteger(hour) ? hour : null }),
    });
    const payload = await readResponsePayload(response);
    if (!response.ok) {
      setAutoUpdateMessage(typeof payload.detail === "string" ? payload.detail : `Could not save schedule (${response.status})`, "bad");
      return;
    }
    autoUpdateFormDirty = false;
    renderAutoUpdateStatus(payload);
    setAutoUpdateMessage("Schedule saved", "ok");
  } catch (err) {
    setAutoUpdateMessage(err instanceof Error ? err.message : String(err), "bad");
  } finally {
    saveButton.disabled = false;
  }
}

let liveLogPollTimer = null;

function renderStatusLogs(payload) {
  const output = document.getElementById("sync-output");
  const lines = [];

  if (payload.operation) {
    lines.push(`operation: ${payload.operation}`);
  }
  if (Array.isArray(payload.logs) && payload.logs.length > 0) {
    lines.push("");
    lines.push(...payload.logs);
  }

  if (lines.length > 0) {
    output.textContent = lines.join("\n");
  }
}

async function pollLiveStatus() {
  const payload = await fetchSyncStatus();
  if (payload && (payload.active || (Array.isArray(payload.logs) && payload.logs.length > 0))) {
    renderStatusLogs(payload);
  }
}

async function fetchSyncStatus() {
  try {
    const response = await fetch("/sync/status", { cache: "no-store" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      return null;
    }
    return payload;
  } catch (_err) {
    return null;
  }
}

function startLiveLogPolling() {
  stopLiveLogPolling();
  void pollLiveStatus();
  liveLogPollTimer = window.setInterval(() => {
    void pollLiveStatus();
  }, 1000);
}

function stopLiveLogPolling() {
  if (liveLogPollTimer !== null) {
    window.clearInterval(liveLogPollTimer);
    liveLogPollTimer = null;
  }
}

function sleep(ms) {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

async function readResponsePayload(response) {
  const rawText = await response.text();
  if (!rawText) {
    return {};
  }

  try {
    return JSON.parse(rawText);
  } catch (_err) {
    return { detail: "Invalid JSON response" };
  }
}

async function waitForOperationCompletion(expectedOperation) {
  for (;;) {
    const payload = await fetchSyncStatus();
    if (payload && payload.operation === expectedOperation) {
      renderStatusLogs(payload);
      if (!payload.active) {
        return payload;
      }
    }
    await sleep(1000);
  }
}

async function runOperation({ endpoint, inProgress, success, failure, onSuccess }) {
  setButtonsDisabled(true);
  setStatus(inProgress);
  document.getElementById("sync-output").textContent = `Starting ${inProgress.toLowerCase()}`;
  startLiveLogPolling();

  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const payload = await readResponsePayload(response);

    if (!response.ok || payload.success === false) {
      setStatus(`${failure}${response.ok ? "" : ` (${response.status})`}`, "bad");
      setMetric("downloaded-count", payload.downloaded?.length || 0);
      renderOutput(payload);
      return;
    }

    setStatus(success, "ok");
    if (typeof onSuccess === "function") {
      await onSuccess(payload);
    }
    renderOutput(payload);
  } catch (err) {
    setStatus("Client error", "bad");
    renderOutput({ detail: err instanceof Error ? err.message : String(err) });
  } finally {
    stopLiveLogPolling();
    setButtonsDisabled(false);
  }
}

async function runBackgroundApplyOperation({ endpoint, operation, inProgress, success, failure, onSuccess }) {
  setButtonsDisabled(true);
  setStatus(inProgress);
  document.getElementById("sync-output").textContent = `Starting ${inProgress.toLowerCase()}`;

  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const payload = await readResponsePayload(response);

    if (!response.ok || payload.started !== true || payload.operation !== operation) {
      setStatus(`${failure}${response.ok ? "" : ` (${response.status})`}`, "bad");
      renderOutput(payload);
      return;
    }

    const finalPayload = await waitForOperationCompletion(operation);
    if (finalPayload.success !== true) {
      setStatus(failure, "bad");
      setMetric("downloaded-count", finalPayload.downloaded?.length || 0);
      renderOutput(finalPayload);
      return;
    }

    setStatus(success, "ok");
    if (typeof onSuccess === "function") {
      await onSuccess(finalPayload);
    }
    renderOutput(finalPayload);
  } catch (err) {
    setStatus("Client error", "bad");
    renderOutput({ detail: err instanceof Error ? err.message : String(err) });
  } finally {
    setButtonsDisabled(false);
  }
}

window.addEventListener("DOMContentLoaded", () => {
  refreshLoadedCount();
  void refreshAutoUpdateStatus();

  document.getElementById("auto-update-form").addEventListener("submit", saveAutoUpdateSchedule);
  document.getElementById("auto-update-enabled").addEventListener("change", () => {
    autoUpdateFormDirty = true;
    syncAutoUpdateHourAvailability();
  });
  document.getElementById("auto-update-hour").addEventListener("input", () => {
    autoUpdateFormDirty = true;
  });
  document.getElementById("auto-update-refresh").addEventListener("click", () => {
    autoUpdateFormDirty = false;
    setAutoUpdateMessage("");
    void refreshAutoUpdateStatus();
  });
  window.setInterval(() => {
    void refreshAutoUpdateStatus();
  }, 5000);

  document.getElementById("apply-button").addEventListener("click", () => {
    setMetric("downloaded-count", 0);
    runBackgroundApplyOperation({
      endpoint: "/sync/apply",
      operation: "sync_apply",
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
