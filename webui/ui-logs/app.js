const options = [...document.querySelectorAll('#log-options input[name]')];
const off = document.getElementById('logs-off');
const settingsStatus = document.getElementById('settings-status');
const logStatus = document.getElementById('log-status');

off.addEventListener('change', () => {
  if (off.checked) options.forEach(input => { input.checked = false; });
  else off.checked = !options.some(input => input.checked);
});
options.forEach(input => input.addEventListener('change', () => {
  off.checked = !options.some(option => option.checked);
}));

async function request(url, init = {}) {
  const response = await fetch(url, { cache: 'no-store', ...init });
  const payload = await response.json();
  if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : `Request failed (${response.status})`);
  return payload;
}

let loadedSettings = false;
let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  const button = document.getElementById('refresh-logs');
  button.disabled = true;
  try {
    const payload = await request('/logs');
    document.getElementById('log-size').textContent =
      `Total retained size: ${payload.size_bytes.toLocaleString()} bytes (${(payload.size_bytes / 1048576).toFixed(2)} MiB). Retention: up to ${payload.max_size_bytes / 1073741824} GiB across one active log and three rotated backups.`;
    document.getElementById('log-output').textContent = payload.text || 'No log entries.';
    logStatus.textContent = payload.truncated ? 'Showing the latest 256 KiB. Download to view all retained logs.' : 'Showing all retained logs.';
    if (!loadedSettings) {
      options.forEach(input => { input.checked = payload.settings[input.name] === true; });
      off.checked = !options.some(input => input.checked);
      loadedSettings = true;
      document.getElementById('log-options').disabled = false;
      document.getElementById('save-settings').disabled = false;
    }
  } catch (error) {
    logStatus.textContent = `Could not load logs: ${error.message}`;
  } finally {
    button.disabled = false;
    refreshing = false;
  }
}

document.getElementById('log-settings').addEventListener('submit', async event => {
  event.preventDefault();
  const button = document.getElementById('save-settings');
  button.disabled = true;
  try {
    await request('/logs/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.fromEntries(options.map(input => [input.name, input.checked]))),
    });
    settingsStatus.textContent = off.checked ? 'Logging is off. Existing logs are retained until cleared.' : 'Log settings saved.';
  } catch (error) {
    settingsStatus.textContent = `Could not save settings: ${error.message}`;
  } finally {
    button.disabled = false;
  }
});

document.getElementById('refresh-logs').addEventListener('click', refresh);
document.getElementById('clear-logs').addEventListener('click', async () => {
  if (!window.confirm('Permanently clear all retained client logs? Log settings will be kept.')) return;
  const button = document.getElementById('clear-logs');
  button.disabled = true;
  try {
    await request('/logs', { method: 'DELETE' });
    await refresh();
  } catch (error) {
    logStatus.textContent = `Could not clear logs: ${error.message}`;
  } finally {
    button.disabled = false;
  }
});
void refresh();

window.setInterval(() => {
  if (document.getElementById('log-window').open && !document.hidden) void refresh();
}, 2000);
document.getElementById('log-window').addEventListener('toggle', event => {
  if (event.target.open) void refresh();
});
