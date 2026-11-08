// popup.js — connection status, "check this page now", and a link to Options.
const $ = (id) => document.getElementById(id);

chrome.runtime.sendMessage({ type: "status" }).then(({ status }) => {
  $("status").textContent = { connected: "Connected to the local app", disconnected: "App not running / not connected",
                              "no-token": "Set the token in Options", "bad-token": "Token rejected — check Options" }[status] || status;
});
$("check").addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.id != null) chrome.tabs.sendMessage(tab.id, { type: "checkPage" });
  window.close();
});
$("options").addEventListener("click", () => chrome.runtime.openOptionsPage());
