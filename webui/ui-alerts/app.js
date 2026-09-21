const form = document.getElementById('alert-form');
const fields = document.getElementById('alert-fields');
const status = document.getElementById('alert-status');
const eventInputs = [...document.querySelectorAll('input[name="event"]')];
let loaded = false;
let dirty = false;
let busy = false;

function message(text, failed = false) {
  status.textContent = text;
  status.className = failed ? 'status bad' : 'status';
}

async function request(url, init = {}) {
  const response = await fetch(url, { cache: 'no-store', ...init });
  const payload = await response.json();
  if (!response.ok) {
    const detail = Array.isArray(payload.detail)
      ? payload.detail.map(item => `${item.loc.slice(1).join('.')}: ${item.msg}`).join('; ')
      : payload.detail;
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return payload;
}

const labels = { sync_failure: 'Sync failure', filter_failure: 'Filter failure', no_filters: 'No filters loaded', service_error: 'Service error', certificate_expiry: 'TLS certificate issue', test: 'Test email' };
function renderHistory(history) {
  const container = document.getElementById('alert-history');
  container.replaceChildren();
  if (!history?.length) container.textContent = 'No delivery attempts yet.';
  (history || []).forEach(attempt => {
    const item = document.createElement('article');
    item.className = 'delivery-attempt';
    const heading = document.createElement('strong');
    heading.textContent = `${labels[attempt.event] || 'Alert'} · ${attempt.success ? 'Accepted' : 'Failed'} · ${new Date(attempt.time).toLocaleString()}`;
    const detail = document.createElement('p');
    detail.textContent = attempt.detail;
    item.append(heading, detail);
    container.appendChild(item);
  });
}

function renderSettings(payload) {
  const settings = payload.settings;
  document.getElementById('enabled').checked = settings.enabled;
  for (const name of ['smtp_host', 'smtp_port', 'security', 'username', 'sender', 'cooldown_minutes', 'certificate_warning_days']) {
    document.getElementById(name).value = settings[name];
  }
  document.getElementById('recipients').value = settings.recipients.join('\n');
  document.getElementById('password').value = '';
  document.getElementById('password').disabled = false;
  document.getElementById('clear-password').checked = false;
  document.getElementById('password-help').textContent = payload.password_configured
    ? 'A password is saved. Leave blank to keep it, or enter a replacement.'
    : 'No password is saved. Enter one if your SMTP server requires authentication.';
  eventInputs.forEach(input => { input.checked = settings.events.includes(input.value); });
  renderHistory(payload.history);
  loaded = true;
  dirty = false;
  fields.disabled = false;
  message(payload.settings_error || (settings.enabled ? 'Automatic alerts are enabled.' : 'Automatic alerts are disabled.'), Boolean(payload.settings_error));
}

async function loadSettings() {
  if (busy) return;
  busy = true;
  fields.disabled = true;
  try { renderSettings(await request('/alerts/settings')); }
  catch (error) { message(`Could not load settings: ${error.message}`, true); }
  finally { fields.disabled = !loaded; busy = false; }
}

form.addEventListener('input', () => { dirty = true; });
document.getElementById('clear-password').addEventListener('change', event => {
  const password = document.getElementById('password');
  password.disabled = event.target.checked;
  if (event.target.checked) password.value = '';
});
document.getElementById('security').addEventListener('change', event => {
  document.getElementById('smtp_port').value = { starttls: 587, ssl: 465, none: 25 }[event.target.value];
});
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (busy || !loaded) return;
  const settings = {
    enabled: document.getElementById('enabled').checked,
    smtp_host: document.getElementById('smtp_host').value.trim(),
    smtp_port: Number(document.getElementById('smtp_port').value),
    security: document.getElementById('security').value,
    username: document.getElementById('username').value.trim(),
    password: document.getElementById('clear-password').checked ? '' : (document.getElementById('password').value || null),
    sender: document.getElementById('sender').value.trim(),
    recipients: document.getElementById('recipients').value.split(/[,;\n]+/).map(value => value.trim()).filter(Boolean),
    events: eventInputs.filter(input => input.checked).map(input => input.value),
    cooldown_minutes: Number(document.getElementById('cooldown_minutes').value),
    certificate_warning_days: Number(document.getElementById('certificate_warning_days').value),
  };
  busy = true;
  fields.disabled = true;
  message('Saving settings…');
  try {
    const payload = await request('/alerts/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(settings) });
    renderSettings(payload);
    void refreshCertificates();
    message(`Settings saved. Automatic alerts are ${payload.settings.enabled ? 'enabled' : 'disabled'}.`);
  } catch (error) { message(error.message, true); }
  finally { fields.disabled = false; busy = false; }
});
document.getElementById('test-alert').addEventListener('click', async () => {
  if (busy || !loaded) return;
  if (dirty) { message('Save your changes before sending a test email.', true); return; }
  busy = true;
  fields.disabled = true;
  message('Sending test email…');
  try { message((await request('/alerts/test', { method: 'POST' })).detail); }
  catch (error) { message(error.message, true); }
  finally {
    fields.disabled = false;
    busy = false;
    void refreshHistory();
  }
});
async function refreshHistory() {
  if (busy || !loaded || document.hidden) return;
  try { renderHistory((await request('/alerts/settings')).history); }
  catch (_) { /* Keep the form and last delivery status while disconnected. */ }
}
document.getElementById('reload-alerts').addEventListener('click', loadSettings);
void loadSettings();
window.setInterval(refreshHistory, 5000);

async function refreshCertificates() {
  const button = document.getElementById('refresh-certificates');
  const status = document.getElementById('certificate-status');
  const container = document.getElementById('certificate-list');
  button.disabled = true;
  try {
    const payload = await request('/alerts/certificates');
    container.replaceChildren();
    status.textContent = payload.enabled
      ? `Checked ${new Date(payload.checked_at).toLocaleString()}. Warning window: ${payload.warning_days} days.`
      : 'Certificate monitoring is disabled for this deployment.';
    for (const cert of payload.certificates) {
      const item = document.createElement('article');
      item.className = 'delivery-attempt';
      const heading = document.createElement('strong');
      heading.textContent = `${cert.name}: ${cert.status.replaceAll('_', ' ')}`;
      const detail = document.createElement('p');
      detail.textContent = cert.expires_at
        ? `${cert.file} · Expires ${new Date(cert.expires_at).toLocaleString()} · ${cert.days_remaining} days remaining`
        : `${cert.file} · File is missing, unreadable, or invalid.`;
      item.append(heading, detail);
      container.appendChild(item);
    }
  } catch (error) { status.textContent = `Could not check certificates: ${error.message}`; }
  finally { button.disabled = false; }
}
document.getElementById('refresh-certificates').addEventListener('click', refreshCertificates);
void refreshCertificates();
