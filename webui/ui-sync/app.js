function setText(id, value) {
  const element = document.getElementById(id);
  const text = String(value);
  if (element.textContent !== text) element.textContent = text;
}

function setStatus(message, variant = "") {
  const el = document.getElementById("sync-status");
  setText("sync-status", message);
  const className = `status ${variant}`.trim();
  if (el.className !== className) el.className = className;
}

function prettyJson(value) {
  return JSON.stringify(value, null, 2);
}

function setMetric(id, value) {
  setText(id, value);
}

let loadedCountVersion = 0;

function updateLoadedCount(count) {
  if (!Number.isInteger(count) || count < 0) return false;
  // An activation result supersedes any stats request already in flight.
  loadedCountVersion++;
  setMetric("loaded-count", count);
  return true;
}

async function refreshLoadedCount() {
  const requestVersion = ++loadedCountVersion;
  try {
    const response = await fetch("/stats", { cache: "no-store" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      return;
    }
    if (requestVersion === loadedCountVersion) updateLoadedCount(payload.filter_count);
  } catch (_err) {
    // Leave the existing metric value in place if stats cannot be fetched.
  }
}

function renderOutput(payload) {
  renderProgress(payload);
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

  setText("sync-output", lines.length > 0 ? lines.join("\n") : prettyJson(payload));
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

let lastHistorySignature = null;
function renderAutoUpdateHistory(history, activeRun = null) {
  const signature = JSON.stringify([history, activeRun]);
  if (signature === lastHistorySignature) return;
  lastHistorySignature = signature;
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

function renderAutoUpdateStatus(payload, applyFormValues = true, renderLiveLogs = true) {
  updateScheduleClock(payload.current_time);
  setText("machine-id", payload.machine_id || "Unavailable (no hardware MAC detected)");
  setText("retry-status", payload.retry_at
    ? `Next retry: ${formatScheduledTime(payload.retry_at)}. Retry window ends: ${formatScheduledTime(payload.retry_until)}.` : "");
  const badge = document.getElementById("auto-update-badge");
  setText("auto-update-badge", payload.active ? "Running" : (payload.enabled ? "Enabled" : "Disabled"));
  const badgeClass = `schedule-badge ${(payload.enabled || payload.active) ? "schedule-badge--enabled" : ""}`.trim();
  if (badge.className !== badgeClass) badge.className = badgeClass;

  if (applyFormValues) {
    document.getElementById("auto-update-enabled").checked = payload.enabled === true;
    if (Number.isInteger(payload.hour)) {
      document.getElementById("auto-update-hour").value = String(payload.hour);
    }
    syncAutoUpdateHourAvailability();
  }

  setText("auto-update-next", formatNextScheduledTime(payload.next_update_at));
  setText("auto-update-timezone", payload.timezone || "—");
  setText("auto-update-current-time", formatScheduledTime(payload.current_time));
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
  scheduleHistory = payload.history;
  scheduleActiveRun = activeRun;
  renderAutoUpdateHistory(payload.history, activeRun);

  if (payload.active) {
    if (renderLiveLogs) renderStatusLogs({ operation: "auto_sync_apply", logs: payload.live_logs || [] });
    setStatus("Scheduled filter update running…");
    autoUpdateWasActive = true;
  } else if (autoUpdateWasActive) {
    const completedRun = Array.isArray(payload.history)
      ? payload.history.find((run) => run.trigger !== "manual")
      : null;
    if (completedRun) {
      if (renderLiveLogs) renderStatusLogs({ operation: "auto_sync_apply", logs: payload.live_logs || [] });
      setStatus(
        completedRun.status === "success" ? "Scheduled filter update complete" : "Scheduled filter update failed",
        completedRun.status === "success" ? "ok" : "bad",
      );
      void refreshLoadedCount();
      void refreshCleanupInventory();
    }
    autoUpdateWasActive = false;
  }
}

let schedulePollBusy = false;
async function refreshAutoUpdateStatus(force = false) {
  if (schedulePollBusy || document.hidden || (syncStreamAvailable && !force)) return;
  schedulePollBusy = true;
  try {
    const response = await fetch("/sync/auto-update", { cache: "no-store" });
    const payload = await readResponsePayload(response);
    if (!response.ok) {
      setAutoUpdateMessage(payload.detail || `Could not load schedule (${response.status})`, "bad");
      return;
    }
    if (!syncStreamAvailable || force) renderAutoUpdateStatus(payload, !autoUpdateFormDirty);
  } catch (err) {
    setAutoUpdateMessage(err instanceof Error ? err.message : String(err), "bad");
  } finally { schedulePollBusy = false; }
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
let syncEventSource = null;
let syncStreamAvailable = false;
let syncConnectTimer = null;
let latestSyncStatus = null;
let syncStatusRequest = null;
const syncStatusWaiters = new Set();
let scheduleClock = null;
let scheduleHistory = [];
let scheduleActiveRun = null;

function updateScheduleClock(value) {
  const match = /([+-])(\d{2}):(\d{2})$/.exec(value || "");
  const offsetMinutes = match ? (Number(match[2]) * 60 + Number(match[3])) * (match[1] === "-" ? -1 : 1) : 0;
  const time = Date.parse(value);
  if (Number.isFinite(time)) scheduleClock = { time, received: Date.now(), offsetMinutes, suffix: match ? match[0] : "Z" };
}

function tickScheduleClock() {
  if (!scheduleClock || document.hidden) return;
  const time = scheduleClock.time + Date.now() - scheduleClock.received + scheduleClock.offsetMinutes * 60000;
  const currentTime = new Date(time).toISOString().slice(0, 19) + scheduleClock.suffix;
  setText("auto-update-current-time", formatScheduledTime(currentTime));
  if (scheduleActiveRun) {
    scheduleActiveRun = { ...scheduleActiveRun, completed_at: currentTime };
    renderAutoUpdateHistory(scheduleHistory, scheduleActiveRun);
  }
}

function wakeSyncStatusWaiters() {
  for (const wake of syncStatusWaiters) wake();
  syncStatusWaiters.clear();
}

function receiveSyncStatus(payload) {
  latestSyncStatus = payload;
  wakeSyncStatusWaiters();
  renderLiveStatus(payload);
}

function closeSyncEventStream() {
  if (syncEventSource) syncEventSource.close();
  syncEventSource = null;
  syncStreamAvailable = false;
  latestSyncStatus = null;
  latestCleanupStatus = null;
  window.clearTimeout(syncConnectTimer);
  wakeSyncStatusWaiters();
}

function startSyncEventStream() {
  if (syncEventSource || (document.hidden && !localOperationRunning && !cleanupRefreshRequestsScan) || typeof window.EventSource !== "function") return;
  let source;
  try {
    source = new window.EventSource("/sync/events");
  } catch (_err) {
    return; // Existing polling remains available when streaming is unsupported.
  }
  syncEventSource = source;
  syncStreamAvailable = true;
  // A proxy that never completes the handshake must not leave the UI stale.
  let receivedStatus = false;
  let receivedSchedule = false;
  let receivedCleanup = false;
  const awaitSnapshots = () => {
    receivedStatus = false;
    receivedSchedule = false;
    receivedCleanup = false;
    window.clearTimeout(syncConnectTimer);
    syncConnectTimer = window.setTimeout(() => {
      if (source !== syncEventSource) return;
      syncStreamAvailable = false;
      void pollLiveStatus();
      void refreshAutoUpdateStatus();
      wakeSyncStatusWaiters();
      void loadCleanupInventory();
    }, 5000);
  };
  const receivedSnapshot = () => {
    if (!receivedStatus || !receivedSchedule || !receivedCleanup) return;
    syncStreamAvailable = true;
    window.clearTimeout(syncConnectTimer);
  };
  awaitSnapshots();
  source.onopen = () => {
    if (source !== syncEventSource) return;
    awaitSnapshots();
    latestSyncStatus = null;
    latestCleanupStatus = null;
    syncStreamAvailable = true;
  };
  source.addEventListener("sync-status", event => {
    if (source !== syncEventSource) return;
    try {
      receiveSyncStatus(JSON.parse(event.data));
      receivedStatus = true;
      receivedSnapshot();
    } catch (_err) { source.onerror(); }
  });
  source.addEventListener("auto-update", event => {
    if (source !== syncEventSource) return;
    try {
      renderAutoUpdateStatus(JSON.parse(event.data), !autoUpdateFormDirty, false);
      receivedSchedule = true;
      receivedSnapshot();
    } catch (_err) { source.onerror(); }
  });
  source.addEventListener("cleanup-status", event => {
    if (source !== syncEventSource) return;
    try {
      latestCleanupStatus = JSON.parse(event.data);
      renderCleanupInventoryStatus(latestCleanupStatus);
      receivedCleanup = true;
      receivedSnapshot();
      wakeSyncStatusWaiters();
    } catch (_err) { source.onerror(); }
  });
  source.onerror = () => {
    if (source !== syncEventSource) return;
    syncStreamAvailable = false;
    window.clearTimeout(syncConnectTimer);
    wakeSyncStatusWaiters();
    void pollLiveStatus();
    void refreshAutoUpdateStatus();
    void loadCleanupInventory();
    // EventSource reconnects automatically; fresh snapshots restore the UI.
  };
}

function renderStatusLogs(payload) {
  renderProgress(payload);
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
    setText("sync-output", lines.join("\n"));
  }
}

async function pollLiveStatus() {
  if (statusPollBusy || document.hidden || syncStreamAvailable) return;
  statusPollBusy = true;
  try {
    const payload = await fetchSyncStatus();
    if (!payload) return;
    receiveSyncStatus(payload);
  } finally { statusPollBusy = false; }
}

function renderLiveStatus(payload) {
  // Download and manifest preparation counts describe staged files, not the
  // active manager. Only successful activation results can replace this metric.
  if (payload.active === false && payload.success === true
      && ["sync_apply", "auto_sync_apply", "sync_reload"].includes(payload.operation)) {
    updateLoadedCount(payload.filter_count);
  }
  renderProgress(payload);
  setButtonsDisabled(payload.active || localOperationRunning);
  if (payload.operation) {
    renderStatusLogs(payload);
    if (!localOperationRunning) {
      setStatus(payload.active ? "Filter operation running…" : (payload.success ? "Filter operation complete" : "Filter operation failed"),
        payload.active ? "" : (payload.success ? "ok" : "bad"));
    }
  }
}

async function fetchSyncStatus() {
  if (syncStreamAvailable) return latestSyncStatus;
  if (syncStatusRequest) return syncStatusRequest;
  syncStatusRequest = (async () => {
    try {
      const response = await fetch("/sync/status", { cache: "no-store" });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) return null;
      // Ignore an older HTTP response if the SSE connection has recovered.
      return syncStreamAvailable ? latestSyncStatus : payload;
    } catch (_err) {
      return null;
    }
  })();
  try { return await syncStatusRequest; } finally { syncStatusRequest = null; }
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
    await waitForStreamChange();
  }
}

function waitForStreamChange() {
  return new Promise(resolve => {
    const wake = () => {
      window.clearTimeout(timer);
      syncStatusWaiters.delete(wake);
      resolve();
    };
    const timer = window.setTimeout(wake, 1000);
    syncStatusWaiters.add(wake);
  });
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
    await loadCleanupInventory();
    if (document.hidden && !cleanupRefreshRequestsScan) closeSyncEventStream();
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

    // A fresh subscription guarantees its initial snapshot follows this accepted
    // command, rather than reusing the cached completion of an earlier run.
    closeSyncEventStream();
    startSyncEventStream();
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
    await loadCleanupInventory();
    if (document.hidden && !cleanupRefreshRequestsScan) closeSyncEventStream();
  }
}

window.addEventListener("DOMContentLoaded", () => {
  startSyncEventStream();
  void loadSyncSettings();
  document.getElementById("sync-settings-form").addEventListener("submit", saveSyncSettings);
  document.getElementById("sync-settings-refresh").addEventListener("click", loadSyncSettings);
  void loadCleanupSettings();
  void loadCleanupInventory();
  window.setInterval(() => { if (!document.hidden) void loadCleanupInventory(); }, 5000);
  document.getElementById("cleanup-delete").addEventListener("click", deleteSupersededFilters);
  document.getElementById("cleanup-files-refresh").addEventListener("click", () => { void refreshCleanupInventory(); });
  document.getElementById("cleanup-mode").addEventListener("change", syncCleanupDaysAvailability);
  document.getElementById("cleanup-form").addEventListener("submit", saveCleanupSettings);
  document.getElementById("cleanup-refresh").addEventListener("click", loadCleanupSettings);
  startLiveLogPolling();
  refreshLoadedCount();
  void refreshAutoUpdateStatus();
  window.setInterval(tickScheduleClock, 1000);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && !localOperationRunning && !cleanupRefreshRequestsScan) closeSyncEventStream();
    else if (!document.hidden) startSyncEventStream();
  });
  window.addEventListener("pagehide", closeSyncEventStream);
  window.addEventListener("pageshow", startSyncEventStream);

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
    void refreshAutoUpdateStatus(true);
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
        if (!updateLoadedCount(payload.filter_count)) await refreshLoadedCount();
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
      async onSuccess(payload) {
        if (!updateLoadedCount(payload.filter_count)) await refreshLoadedCount();
      },
    });
  });
});

let cleanupInventory = null;
let cleanupDeleteRunning = false;

function formatFilterBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let value = bytes / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index++; }
  return `${value.toFixed(2)} ${units[index]}`;
}

function showCleanupInventory(inventory) {
  cleanupInventory = inventory;
  document.getElementById("cleanup-count").textContent = inventory.filter_count.toLocaleString();
  document.getElementById("cleanup-size").textContent = `${formatFilterBytes(inventory.total_bytes)} (${inventory.total_bytes.toLocaleString()} bytes)`;
  document.getElementById("cleanup-delete").disabled = cleanupDeleteRunning || inventory.filter_count === 0;
}

let cleanupRefreshPromise = null;
let cleanupRefreshRequestsScan = false;
let cleanupStatusSignature = null;
let latestCleanupStatus = null;

function loadCleanupInventory() {
  if (syncStreamAvailable) return Promise.resolve(cleanupInventory);
  return refreshCleanupInventory(false);
}

function refreshCleanupInventory(requestScan = true) {
  if (cleanupRefreshPromise && requestScan && !cleanupRefreshRequestsScan) {
    // A cached status read cannot satisfy an explicit request for a new scan.
    return cleanupRefreshPromise.then(() => refreshCleanupInventory(true));
  }
  if (!cleanupRefreshPromise) {
    cleanupRefreshRequestsScan = requestScan;
    cleanupRefreshPromise = runCleanupInventoryRefresh(requestScan).finally(() => {
      cleanupRefreshPromise = null;
      cleanupRefreshRequestsScan = false;
    });
  }
  return cleanupRefreshPromise;
}

async function runCleanupInventoryRefresh(requestScan) {
  if (requestScan) showCleanupScanning();
  try {
    let response = await fetch(requestScan ? "/sync/cleanup/files/refresh" : "/sync/cleanup/files/status",
      requestScan ? { method: "POST" } : { cache: "no-store" });
    let status = await readResponsePayload(response);
    if (!response.ok) throw new Error(status.detail || "Could not read file totals scan status");
    if (requestScan && syncStreamAvailable) {
      // Initial snapshots from this subscription follow the accepted refresh,
      // so an old cached completion cannot satisfy a newly requested scan.
      closeSyncEventStream();
      startSyncEventStream();
    } else if (syncStreamAvailable) {
      // An older fallback response must not overwrite a recovered stream.
      if (!latestCleanupStatus) return cleanupInventory;
      status = latestCleanupStatus;
    }
    renderCleanupInventoryStatus(status, requestScan);
    while (status.active) {
      if (syncStreamAvailable) {
        if (latestCleanupStatus && latestCleanupStatus !== status) status = latestCleanupStatus;
        else {
          await waitForStreamChange();
          continue;
        }
      } else {
        await new Promise(resolve => setTimeout(resolve, 1000));
        // Streaming may have recovered during the fallback delay.
        if (syncStreamAvailable) continue;
        response = await fetch("/sync/cleanup/files/status", { cache: "no-store" });
        const fallbackStatus = await readResponsePayload(response);
        if (!response.ok) throw new Error(fallbackStatus.detail || "Could not read file totals scan status");
        if (syncStreamAvailable) {
          if (!latestCleanupStatus) continue;
          status = latestCleanupStatus;
        } else status = fallbackStatus;
      }
      renderCleanupInventoryStatus(status, requestScan);
    }
    return cleanupInventory;
  } catch (error) {
    showCleanupError(error.message || String(error));
    return null;
  } finally {
    if (document.hidden && !localOperationRunning) closeSyncEventStream();
  }
}

function showCleanupScanning() {
  document.getElementById("cleanup-files-refresh").disabled = true;
  document.getElementById("cleanup-files-refresh").textContent = "Scanning…";
  document.getElementById("cleanup-delete").disabled = true;
  setText("cleanup-files-message", "Scanning superseded filters (waiting for any running filter operation). Previous totals remain visible.");
  document.getElementById("cleanup-files-message").className = "status";
}

function showCleanupError(error) {
  cleanupStatusSignature = null;
  cleanupInventory = null;
  // Keep previous totals visible, but never permit deletion using a stale token.
  setText("cleanup-files-message", `${error} Previous totals may be out of date.`);
  document.getElementById("cleanup-files-message").className = "status bad";
  document.getElementById("cleanup-files-refresh").disabled = cleanupDeleteRunning;
  document.getElementById("cleanup-files-refresh").textContent = "Refresh file totals";
  document.getElementById("cleanup-delete").disabled = true;
}

function renderCleanupInventoryStatus(status, requestScan = false) {
  const signature = JSON.stringify(status);
  if (!requestScan && signature === cleanupStatusSignature) return;
  cleanupStatusSignature = signature;
  if (status.inventory) showCleanupInventory(status.inventory);
  setText("cleanup-checked-at", status.checked_at ? formatHistoryTime(status.checked_at) : "Never checked");
  if (status.active) {
    showCleanupScanning();
    return;
  }
  if (status.error) {
    showCleanupError(status.error);
    return;
  }
  if (!status.inventory) cleanupInventory = null;
  document.getElementById("cleanup-files-refresh").disabled = cleanupDeleteRunning;
  document.getElementById("cleanup-files-refresh").textContent = "Refresh file totals";
  document.getElementById("cleanup-delete").disabled = cleanupDeleteRunning || !cleanupInventory || cleanupInventory.filter_count === 0;
  if (document.getElementById("cleanup-mode").disabled) void loadCleanupSettings();
  setText("cleanup-files-message", !status.inventory ? "No cached totals. Use Refresh file totals to check."
    : (requestScan ? "File totals refreshed." : "Showing the last checked file totals."));
  document.getElementById("cleanup-files-message").className = status.inventory ? "status ok" : "status";
}

async function deleteSupersededFilters() {
  if (cleanupDeleteRunning) return;
  cleanupDeleteRunning = true;
  const button = document.getElementById("cleanup-delete");
  const message = document.getElementById("cleanup-files-message");
  button.disabled = true;
  try {
    const inventory = await refreshCleanupInventory();
    if (!inventory) return;
    if (!inventory.filter_count) {
      message.textContent = "No superseded filters to delete.";
      message.className = "status";
      return;
    }
    if (!window.confirm(`Are you sure you want to permanently delete ${inventory.filter_count.toLocaleString()} superseded filter files (${formatFilterBytes(inventory.total_bytes)})? This cannot be undone.`)) return;
    const response = await fetch("/sync/cleanup/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirmation_token: inventory.confirmation_token }),
    });
    const result = await readResponsePayload(response);
    if (!response.ok) throw new Error(result.detail || "Could not delete superseded filters");
    await loadCleanupInventory();
    message.textContent = `Deleted ${result.deleted_count.toLocaleString()} superseded filters (${formatFilterBytes(result.deleted_bytes)}).`
      + (result.filter_count ? ` ${result.filter_count.toLocaleString()} remain; check diagnostic logs and retry.` : "");
    message.className = result.filter_count ? "status bad" : "status ok";
  } catch (error) {
    cleanupInventory = null;
    message.textContent = error.message || String(error);
    message.className = "status bad";
  } finally {
    cleanupDeleteRunning = false;
    document.getElementById("cleanup-files-refresh").disabled = false;
    button.disabled = !cleanupInventory || cleanupInventory.filter_count === 0;
  }
}

function syncCleanupDaysAvailability() {
  const retained = document.getElementById("cleanup-mode").value === "retained";
  document.getElementById("cleanup-days").disabled = !retained;
  document.getElementById("cleanup-days-field").hidden = !retained;
}

function showSyncSettings(settings) {
  document.getElementById("reuse-verified-md5").checked = settings.reuse_verified_md5 === true;
  document.getElementById("reuse-verified-md5").disabled = false;
  document.getElementById("sync-settings-save").disabled = false;
}

async function loadSyncSettings() {
  const message = document.getElementById("sync-settings-message");
  try {
    const response = await fetch("/sync/settings", { cache: "no-store" });
    const settings = await readResponsePayload(response);
    if (!response.ok) throw new Error(settings.detail || "Could not load verification setting");
    showSyncSettings(settings);
    message.textContent = "";
    message.className = "status";
  } catch (error) {
    message.textContent = error.message || String(error);
    message.className = "status bad";
  }
}

async function saveSyncSettings(event) {
  event.preventDefault();
  const button = document.getElementById("sync-settings-save");
  const message = document.getElementById("sync-settings-message");
  button.disabled = true;
  try {
    const response = await fetch("/sync/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reuse_verified_md5: document.getElementById("reuse-verified-md5").checked }),
    });
    const settings = await readResponsePayload(response);
    if (!response.ok) throw new Error(settings.detail || "Could not save verification setting");
    showSyncSettings(settings);
    message.textContent = "Verification setting saved. Applies to the next sync and survives restarts.";
    message.className = "status ok";
  } catch (error) {
    message.textContent = error.message || String(error);
    message.className = "status bad";
  } finally {
    button.disabled = false;
  }
}

function showCleanupSettings(settings) {
  document.getElementById("cleanup-mode").value = !settings.enabled
    ? "off" : (settings.retention_days === 0 ? "immediate" : "retained");
  document.getElementById("cleanup-days").value = settings.retention_days || 31;
  document.getElementById("cleanup-mode").disabled = false;
  document.getElementById("cleanup-save").disabled = false;
  syncCleanupDaysAvailability();
}

async function loadCleanupSettings() {
  const message = document.getElementById("cleanup-message");
  try {
    const response = await fetch("/sync/cleanup");
    const settings = await readResponsePayload(response);
    if (!response.ok) throw new Error(settings.detail || "Could not load cleanup settings");
    showCleanupSettings(settings);
    message.textContent = "";
    message.className = "status";
  } catch (error) {
    message.textContent = error.message || String(error);
    message.className = "status bad";
  }
}

async function saveCleanupSettings(event) {
  event.preventDefault();
  const mode = document.getElementById("cleanup-mode").value;
  const days = Number(document.getElementById("cleanup-days").value);
  const message = document.getElementById("cleanup-message");
  if (mode === "retained" && (!Number.isInteger(days) || days < 1 || days > 31)) {
    message.textContent = "Enter a whole number of days from 1 to 31.";
    message.className = "status bad";
    return;
  }
  const button = document.getElementById("cleanup-save");
  button.disabled = true;
  try {
    const response = await fetch("/sync/cleanup", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: mode !== "off", retention_days: mode === "immediate" ? 0 : days }),
    });
    const settings = await readResponsePayload(response);
    if (!response.ok) throw new Error(settings.detail || "Could not save cleanup settings");
    showCleanupSettings(settings);
    message.textContent = "Cleanup settings saved. Automatic deletion runs after the next successful sync and reload.";
    message.className = "status ok";
  } catch (error) {
    message.textContent = error.message || String(error);
    message.className = "status bad";
  } finally {
    button.disabled = false;
  }
}

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
  const labels = { prepare: 'Fetch remote manifest', download: 'Check and download archives', extract: 'Stage incoming filters', verify: 'Verify checksums', manifest: 'Prepare candidate manifest', validate: 'Validate all shards and required test credentials', reload: 'Activate validated filters' };
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
