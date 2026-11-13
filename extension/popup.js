// popup.js — connection status, "Check this page now", "Check part of page…", "Check all claims on this page", Options.
const $ = (id) => document.getElementById(id);

chrome.runtime.sendMessage({ type: "status" }).then(({ status }) => {
  $("status").textContent = { connected: "Connected to the local app", disconnected: "App not running / not connected",
                              "no-token": "Set the token in Options", "bad-token": "Token rejected — check Options" }[status] || status;
});
async function askActiveTab(type) {        // the service worker injects the content script into old tabs if needed
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.id != null) await chrome.runtime.sendMessage({ type, tabId: tab.id });
  window.close();                            // the page gets the focus back (the box picker needs the mouse there)
}
$("check").addEventListener("click", () => askActiveTab("checkTab"));
$("part").addEventListener("click", () => askActiveTab("checkPartTab"));
$("all").addEventListener("click", () => askActiveTab("checkAllTab"));
$("options").addEventListener("click", () => chrome.runtime.openOptionsPage());
