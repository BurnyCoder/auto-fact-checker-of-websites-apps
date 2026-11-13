// options.js — load/save extension settings in chrome.storage.local and show the connection status.
const DEFAULT_BLOCKED_SITES = ["mail.google.com", "outlook.live.com", "outlook.office.com", "mail.yahoo.com", "mail.proton.me",
  "web.whatsapp.com", "web.telegram.org", "messages.google.com", "vault.bitwarden.com", "my.1password.com", "keepersecurity.com",
  "accounts.google.com", "login.microsoftonline.com", "login.live.com"];   // keep in sync with content.js
const DEFAULTS = { serverUrl: "ws://127.0.0.1:8765/ws", token: "", autoMode: false, blockedSites: DEFAULT_BLOCKED_SITES };
const $ = (id) => document.getElementById(id);

async function load() {
  const s = { ...DEFAULTS, ...(await chrome.storage.local.get(Object.keys(DEFAULTS))) };
  $("serverUrl").value = s.serverUrl;
  $("token").value = s.token;
  $("autoMode").checked = s.autoMode;
  $("blockedSites").value = s.blockedSites.join(String.fromCharCode(10));   // one site per line
  refreshStatus();
}

async function refreshStatus() {
  const { status } = await chrome.runtime.sendMessage({ type: "status" });
  const text = { connected: "Connected to the local app ✓", disconnected: "Not connected — is the desktop app running?",
                 "no-token": "Paste the extension token to connect.", "bad-token": "The app rejected this token." }[status];
  $("status").textContent = text || status;
}

$("save").addEventListener("click", async () => {
  await chrome.storage.local.set({ serverUrl: $("serverUrl").value.trim() || DEFAULTS.serverUrl,
                                   token: $("token").value.trim(), autoMode: $("autoMode").checked,
                                   blockedSites: $("blockedSites").value.split(/\r?\n/).map((x) => x.trim()).filter(Boolean) });
  $("status").textContent = "Saved. Connecting…";
  setTimeout(refreshStatus, 1500);
});

load();
