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
  renderProgress(payload);
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

function formatHistoryTime(value) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return formatScheduledTime(value);
  }
  return parsed.toLocaleString();
}

function historyDescription(run) {
  const trigger = run.trigger === "manual" ? "manual" : "scheduled";
  const subject = trigger === "manual"
    ? (run.operation === "sync_filters" ? "Filter sync manually" : "Sync manually")
    : "Scheduled sync automatically";
  const when = formatHistoryTime(run.triggered_at);

  if (run.status === "running") {
    return `${subject} started on ${when} — in progress currently`;
  }

  const outcome = run.status === "success"
    ? "completed successfully"
    : (run.status === "skipped" ? "was skipped" : "failed");
  return `${subject} performed on ${when} — ${outcome}`;
}

function historyOutcome(run) {
  if (run.status === "running") {
    return `Running for ${formatDuration(run.triggered_at, run.completed_at)}. Live output is shown in Operational Log below.`;
  }

  const outcomes = [];
  if (run.filter_count > 0) {
    outcomes.push(`${run.filter_count} filters loaded`);
  }
  if ((run.downloaded?.length || 0) > 0) {
    outcomes.push(`${run.downloaded.length} new archive${run.downloaded.length === 1 ? "" : "s"} downloaded`);
  }
  if ((run.redownloaded?.length || 0) > 0) {
    outcomes.push(`${run.redownloaded.length} archive${run.redownloaded.length === 1 ? "" : "s"} refreshed`);
  }
  if ((run.verified_existing?.length || 0) > 0) {
    outcomes.push(`${run.verified_existing.length} archive${run.verified_existing.length === 1 ? "" : "s"} already current`);
  }
  if (outcomes.length === 0 && run.status === "success") {
    outcomes.push("No archive downloads were required");
  }
  outcomes.push(`duration ${formatDuration(run.triggered_at, run.completed_at)}`);
  return outcomes.join(" · ");
}

function renderAutoUpdateHistory(history, activeRun = null) {
  const container = document.getElementById("auto-update-history");
  container.replaceChildren();

  const runs = Array.isArray(history) ? [...history] : [];
  if (activeRun) {
    runs.unshift(activeRun);
  }

  const toggle = document.getElementById("history-toggle");
  toggle.hidden = runs.length <= 1;

  if (runs.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-history";
    empty.textContent = "No manual or automatic syncs have been recorded yet.";
    container.appendChild(empty);
    return;
  }

  runs.forEach((run, index) => {
    const item = document.createElement("article");
    item.className = "history-item";
    item.hidden = index > 0 && !historyExpanded;

    const header = document.createElement("div");
    header.className = "history-item__header";
    const title = document.createElement("strong");
    title.textContent = historyDescription(run);

    const badge = document.createElement("span");
    badge.className = `run-badge run-badge--${run.status || "failed"}`;
    badge.textContent = run.status || "unknown";
    header.append(title, badge);
    item.appendChild(header);

    const counts = document.createElement("p");
    counts.className = "history-item__counts";
    counts.textContent = historyOutcome(run);
    item.appendChild(counts);

    if (run.detail) {
      const detail = document.createElement("p");
      detail.className = "history-item__detail";
      detail.textContent = run.detail;
      item.appendChild(detail);
    }

    container.appendChild(item);
  });
}

let historyExpanded = false;
document.getElementById("history-toggle").addEventListener("click", () => {
  historyExpanded = !historyExpanded;
  const toggle = document.getElementById("history-toggle");
  toggle.setAttribute("aria-expanded", String(historyExpanded));
  toggle.textContent = historyExpanded ? "Show latest only" : "Show all syncs";
  document.querySelectorAll("#auto-update-history .history-item").forEach((item, index) => {
    item.hidden = index > 0 && !historyExpanded;
  });
});

let autoUpdateFormDirty = false;
let autoUpdateWasActive = false;

function setAutoUpdateMessage(message, variant = "") {
  const element = document.getElementById("auto-update-message");
  element.textContent = message;
  element.className = `status ${variant}`.trim();
}

function syncAutoUpdateHourAvailability() {
  document.getElementById("auto-update-hour").disabled = !document.getElementById("auto-update-enabled").checked;
}

function renderAutoUpdateStatus(payload, applyFormValues = true) {
  document.getElementById("machine-id").textContent = payload.machine_id || "Unavailable (no hardware MAC detected)";
  document.getElementById("retry-status").textContent = payload.retry_at
    ? `Next retry: ${formatScheduledTime(payload.retry_at)}. Retry window ends: ${formatScheduledTime(payload.retry_until)}.` : "";
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
    trigger: "scheduled",
    operation: "sync_apply",
    filter_count: 0,
    downloaded: [],
    redownloaded: [],
    verified_existing: [],
  } : null;
  renderAutoUpdateHistory(payload.history, activeRun);

  if (payload.active) {
    renderStatusLogs({ operation: "auto_sync_apply", logs: payload.live_logs || [] });
    setStatus("Scheduled filter update running…");
    autoUpdateWasActive = true;
  } else if (autoUpdateWasActive) {
    const completedRun = Array.isArray(payload.history)
      ? payload.history.find((run) => run.trigger !== "manual")
      : null;
    if (completedRun) {
      renderStatusLogs({ operation: "auto_sync_apply", logs: payload.live_logs || [] });
      setStatus(
        completedRun.status === "success" ? "Scheduled filter update complete" : "Scheduled filter update failed",
        completedRun.status === "success" ? "ok" : "bad",
      );
      void refreshLoadedCount();
    }
    autoUpdateWasActive = false;
  }
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
let statusPollBusy = false;
let localOperationRunning = false;

function renderStatusLogs(payload) {
  renderProgress(payload);
  const output = document.getElementById("sync-output");
  const lines = [];

  if (payload.operation) {
    lines.push(`operation: ${payload.operation}`);
  }
  if (payload.detail) lines.push(`detail: ${payload.detail}`);
  if (Array.isArray(payload.logs) && payload.logs.length > 0) {
    lines.push("");
    lines.push(...payload.logs);
  }

  if (lines.length > 0) {
    output.textContent = lines.join("\n");
  }
}

async function pollLiveStatus() {
  if (statusPollBusy || document.hidden) return;
  statusPollBusy = true;
  try {
    const payload = await fetchSyncStatus();
    if (!payload) return;
    renderProgress(payload);
    setButtonsDisabled(payload.active || localOperationRunning);
    if (payload.operation) {
      renderStatusLogs(payload);
      if (!localOperationRunning) {
        setStatus(payload.active ? "Filter operation running…" : (payload.success ? "Filter operation complete" : "Filter operation failed"),
          payload.active ? "" : (payload.success ? "ok" : "bad"));
      }
    }
  } finally { statusPollBusy = false; }
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

function scrollToProgress() {
  const progress = document.getElementById("sync-progress-section");
  window.scrollTo({
    top: window.scrollY + progress.getBoundingClientRect().top - 16,
    behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth",
  });
}

async function runOperation({ endpoint, inProgress, success, failure, onSuccess }) {
  localOperationRunning = true;
  setButtonsDisabled(true);
  setStatus(inProgress);
  document.getElementById("sync-output").textContent = `Starting ${inProgress.toLowerCase()}`;
  scrollToProgress();
  startLiveLogPolling();

  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const payload = await readResponsePayload(response);

    if (!response.ok || payload.success === false) {
      setStatus(`${failure}${response.ok ? "" : ` (${response.status})`}`, "bad");
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
    localOperationRunning = false;
    setButtonsDisabled(false);
    await pollLiveStatus();
  }
}

async function runBackgroundApplyOperation({ endpoint, operation, inProgress, success, failure, onSuccess }) {
  localOperationRunning = true;
  setButtonsDisabled(true);
  setStatus(inProgress);
  document.getElementById("sync-output").textContent = `Starting ${inProgress.toLowerCase()}`;
  scrollToProgress();

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
    localOperationRunning = false;
    setButtonsDisabled(false);
    await pollLiveStatus();
  }
}

window.addEventListener("DOMContentLoaded", () => {
  startLiveLogPolling();
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
    runBackgroundApplyOperation({
      endpoint: "/sync/apply",
      operation: "sync_apply",
      inProgress: "Running filter sync, manifest update, and reload...",
      success: "Filter sync, manifest update, and reload complete",
      failure: "Combined filter update failed",
      async onSuccess(payload) {
        await refreshLoadedCount();
      },
    });
  });

  document.getElementById("sync-button").addEventListener("click", () => {
    runOperation({
      endpoint: "/sync/filters",
      inProgress: "Running filter sync...",
      success: "Filter sync complete",
      failure: "Filter sync failed",
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

let previousProgress = '';
function renderProgress(payload) {
  if (!Array.isArray(payload.progress)) return;
  const signature = JSON.stringify([payload.progress, payload.active, payload.success]);
  if (signature === previousProgress) return;
  previousProgress = signature;
  const summary = document.getElementById('progress-summary');
  summary.textContent = payload.active ? 'Operation in progress' : (payload.success === true ? 'Operation complete' : (payload.success === false ? 'Operation failed' : 'No operation started yet.'));
  const container = document.getElementById('sync-progress');
  container.replaceChildren();
  const labels = { prepare: 'Fetch remote manifest', download: 'Check and download archives', extract: 'Extract filters', verify: 'Verify checksums', manifest: 'Update local manifest', reload: 'Reload filters' };
  const statuses = { pending: 'Waiting', running: 'In progress', complete: 'Complete', failed: 'Failed', skipped: 'Not needed' };
  payload.progress.forEach(phase => {
    const row = document.createElement('div');
    row.className = `progress-phase progress-phase--${phase.status}`;
    const label = document.createElement('label');
    label.htmlFor = `progress-${phase.phase}`;
    label.textContent = labels[phase.phase] || phase.phase;
    const state = document.createElement('span');
    const percent = phase.total > 0 ? Math.min(100, Math.max(0, phase.completed / phase.total * 100)) : null;
    state.textContent = `${statuses[phase.status] || phase.status}${percent !== null ? ` · ${Math.floor(percent)}%` : ''}`;
    const bar = document.createElement('progress');
    bar.id = label.htmlFor;
    bar.max = 100;
    if (!(phase.status === 'running' && percent === null)) bar.value = percent || 0;
    const detail = document.createElement('small');
    detail.textContent = phase.detail;
    row.append(label, state, bar, detail);
    container.appendChild(row);
  });
}
