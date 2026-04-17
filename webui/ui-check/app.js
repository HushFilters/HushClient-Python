function toHex(buffer) {
  const bytes = new Uint8Array(buffer);
  return Array.from(bytes).map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function sha256Hex(input) {
  const encoded = new TextEncoder().encode(input);
  const digest = await crypto.subtle.digest("SHA-256", encoded);
  return toHex(digest);
}

function setStatus(message, variant = "") {
  const el = document.getElementById("status");
  el.textContent = message;
  el.className = `status ${variant}`.trim();
}

function prettyJson(value) {
  return JSON.stringify(value, null, 2);
}

function syncPasswordToggleState(isVisible) {
  const passwordInput = document.getElementById("password");
  const toggle = document.getElementById("password-toggle");

  passwordInput.type = isVisible ? "text" : "password";
  toggle.setAttribute("aria-label", isVisible ? "Hide password" : "Show password");
  toggle.setAttribute("aria-pressed", String(isVisible));
  toggle.dataset.visible = String(isVisible);
}

function togglePasswordVisibility() {
  const passwordInput = document.getElementById("password");
  syncPasswordToggleState(passwordInput.type === "password");
}

async function submitCheck(event) {
  event.preventDefault();

  const form = document.getElementById("check-form");
  const submitBtn = document.getElementById("submit-btn");
  const hashOutput = document.getElementById("hash-output");
  const apiOutput = document.getElementById("api-output");

  const username = form.username.value || "";
  const password = form.password.value || "";

  submitBtn.disabled = true;
  setStatus("Computing SHA-256...");

  try {
    const digest = await sha256Hex(`${username}nWebbed${password}`);
    hashOutput.textContent = digest;

    setStatus("Submitting to /checkhash...");
    const response = await fetch("/checkhash", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hash: digest }),
    });

    const payload = await response.json().catch(() => ({ detail: "Invalid JSON response" }));

    if (!response.ok) {
      setStatus(`Error (${response.status})`, "bad");
      apiOutput.textContent = prettyJson(payload);
      return;
    }

    const state = payload.found ? "FOUND" : "NOT FOUND";
    setStatus(`Lookup complete: ${state}`, payload.found ? "ok" : "bad");
    apiOutput.textContent = prettyJson(payload);
  } catch (err) {
    setStatus("Client error", "bad");
    apiOutput.textContent = prettyJson({ detail: err instanceof Error ? err.message : String(err) });
  } finally {
    submitBtn.disabled = false;
  }
}

window.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("check-form");
  const passwordToggle = document.getElementById("password-toggle");

  syncPasswordToggleState(false);
  form.addEventListener("submit", submitCheck);
  passwordToggle.addEventListener("click", togglePasswordVisibility);
  passwordToggle.addEventListener("mousedown", (event) => event.preventDefault());
});
