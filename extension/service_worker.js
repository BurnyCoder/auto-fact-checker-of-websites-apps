// service_worker.js — the extension's background script.
//
// Local context: keeps ONE authenticated WebSocket to the local fact-check app (ws://127.0.0.1:8765/ws),
// relays requests from content scripts (one runtime Port per tab/frame) and routes the app's events back
// to the Port that owns each source.
// Global context: the content script never talks to localhost itself (Chrome's Local Network Access
// prompts would hit pages, https://developer.chrome.com/blog/local-network-access). MV3 workers stay
// alive while a WebSocket is active (Chrome 116+), and a 20 s ping keeps traffic flowing
// (https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle).
// The socket is opened only when there is work (a check, or the popup/Options asking for the status) and only
// after GET /health answers: a WebSocket to a closed port logs "net::ERR_CONNECTION_REFUSED" in
// chrome://extensions on every attempt, while a failed fetch() logs nothing (both measured over CDP, 2026-11-13).
// There is no reconnect loop: the app cancels a connection's work when it drops, so pages are told instead.

const DEFAULTS = { serverUrl: "ws://127.0.0.1:8765/ws", token: "" };
const KEEPALIVE_MS = 20000;          // ping interval
const MAX_PENDING = 50;              // messages buffered while connecting
const NOT_RUNNING = "the fact-check app is not running; start it with: uv run factcheck";

let ws = null;                       // the WebSocket (or null)
let ready = false;                   // true after the server's "hello" (auth accepted)
let connecting = null;               // promise of the connection attempt in progress
let keepaliveTimer = null;
const pending = [];                  // {msg, port} queued until connected
const ports = new Map();             // sourceId -> Port of the content script that owns it
const portSources = new Map();       // Port -> Set(sourceId) (to cancel on disconnect)
let lastStatus = "disconnected";

async function getSettings() {
  return { ...DEFAULTS, ...(await chrome.storage.local.get(Object.keys(DEFAULTS))) };
}

function setStatus(status) {
  // Badge text: "" when connected, short hint otherwise.
  lastStatus = status;
  const badge = { connected: "", disconnected: "off", "no-token": "key", "bad-token": "key" }[status] ?? "";
  chrome.action.setBadgeText({ text: badge });
  chrome.action.setBadgeBackgroundColor({ color: "#80868b" });
}

function tell(port, message) {        // explain to a waiting page why nothing will come back
  try { port?.postMessage({ type: "disconnected", message }); } catch { /* tab went away */ }
}

function failPending(message) {
  for (const { port } of pending.splice(0)) tell(port, message);
}

// Resolves once the socket is ready, or the attempt failed (status says which).
function connect() {
  if (ready || connecting) return connecting || Promise.resolve();
  connecting = (async () => {
    const s = await getSettings();
    if (!s.token) { setStatus("no-token"); return failPending("paste the extension token in the extension's Options"); }
    const health = s.serverUrl.replace(/^ws/, "http").replace(/\/ws$/, "/health");
    const up = await fetch(health, { cache: "no-store" }).then((r) => r.ok, () => false);
    if (!up) { setStatus("disconnected"); return failPending(NOT_RUNNING); }
    await new Promise((resolve) => {
      ws = new WebSocket(s.serverUrl);
      ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token: s.token }));
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.type === "hello") {                          // auth accepted
          ready = true; setStatus("connected");
          clearInterval(keepaliveTimer);
          keepaliveTimer = setInterval(() => send({ type: "ping" }), KEEPALIVE_MS);
          for (const { msg: m } of pending.splice(0)) ws.send(JSON.stringify(m));
          return resolve();
        }
        if (msg.type === "pong") return;
        const port = ports.get(msg.source_id);               // route to the owning tab
        if (port) { try { port.postMessage(msg); } catch { /* tab went away */ } }
      };
      ws.onclose = (ev) => {
        const wasReady = ready;
        ready = false; ws = null; clearInterval(keepaliveTimer);
        setStatus(ev.code === 4401 ? "bad-token" : "disconnected");
        const why = ev.code === 4401 ? "the app rejected the extension token; copy it again from the tray menu into Options"
                  : wasReady ? "lost the connection to the fact-check app; check again once it runs" : NOT_RUNNING;
        if (wasReady) for (const port of portSources.keys()) tell(port, why);   // their claims were cancelled
        failPending(why);
        resolve();
      };
      ws.onerror = () => { /* onclose follows */ };
    });
  })().finally(() => { connecting = null; });
  return connecting;
}

function send(obj, port = null) {
  if (ready && ws && ws.readyState === WebSocket.OPEN) { ws.send(JSON.stringify(obj)); return; }
  if (obj.type === "ping" || obj.type === "cancel") return;  // nothing to keep alive / nothing running
  pending.push({ msg: obj, port });
  if (pending.length > MAX_PENDING) pending.shift();
  connect();
}

// ---- content-script Ports: one per tab/frame ---------------------------------------------------------
chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "factcheck" || !port.sender?.tab) return;
  const tabId = port.sender.tab.id;
  portSources.set(port, new Set());
  port.onMessage.addListener((msg) => {
    const sourceId = `tab${tabId}:${msg.docId || "doc"}`;    // unique per page load
    ports.set(sourceId, port);                               // route replies (also after a worker restart)
    portSources.get(port).add(sourceId);
    if (msg.type === "submit") {
      send({ type: "submit", source: { source_id: sourceId, title: msg.title, url: msg.url },
             blocks: msg.blocks, auto: msg.auto, force: msg.force }, port);
    } else if (msg.type === "check") {
      send({ type: "check", claim_id: msg.claimId, source_id: sourceId }, port);
    }
  });
  port.onDisconnect.addListener(() => {                      // tab closed or navigated: stop its work
    for (const sid of portSources.get(port) || []) { send({ type: "cancel", source_id: sid }); ports.delete(sid); }
    portSources.delete(port);
  });
});

// Ask the tab's content script to check; tabs opened before the extension was (re)loaded have none, so inject it
// (otherwise sendMessage rejects with "Receiving end does not exist", an error in chrome://extensions).
async function askTab(tabId, msg) {
  try { return await chrome.tabs.sendMessage(tabId, msg); } catch { /* no live content script */ }
  try {
    await chrome.scripting.insertCSS({ target: { tabId }, files: ["content.css"] });
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
    return await chrome.tabs.sendMessage(tabId, msg);
  } catch { /* chrome://, Web Store and PDF pages cannot be scripted */ }
}

// ---- toolbar button (popup), keyboard command, context menu, status ---------------------------------
chrome.runtime.onInstalled.addListener(async () => {
  chrome.contextMenus.removeAll(() => {                      // reload keeps old items: avoid a duplicate-id error
    chrome.contextMenus.create({ id: "fc-selection", title: "Fact-check selected text", contexts: ["selection"] });
  });
  // Settings v2 (2026-11-13): checking became on-request by default. autoMode=true stored by the old Options page was
  // that page's default checkbox state, not a choice, so it is reset once; ticking the box later is kept.
  const { settingsVersion = 1 } = await chrome.storage.local.get("settingsVersion");
  if (settingsVersion < 2) await chrome.storage.local.set({ autoMode: false, settingsVersion: 2 });
});
chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "fc-selection" && tab?.id != null) askTab(tab.id, { type: "checkSelection" });
});
// Alt+Shift+F checks the visible page; Alt+Shift+S checks only a part (the selected text, or a box dragged over the
// page). Chrome allows at most 4 suggested shortcuts per extension
// (https://developer.chrome.com/docs/extensions/reference/api/commands); the user can change both in
// chrome://extensions/shortcuts.
const COMMANDS = { "check-page": "checkPage", "check-part": "checkPart" };
chrome.commands.onCommand.addListener(async (command) => {
  if (!COMMANDS[command]) return;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.id != null) askTab(tab.id, { type: COMMANDS[command] });
});
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg?.type === "status") { connect().then(() => reply({ status: lastStatus })); return true; }
  const ask = { checkTab: "checkPage", checkPartTab: "checkPart" }[msg?.type];    // popup buttons
  if (ask) { askTab(msg.tabId, { type: ask }).then(() => reply({ ok: true })); return true; }
  return false;
});
chrome.storage.onChanged.addListener((changes) => {
  if ((changes.token || changes.serverUrl) && ws) ws.close();   // the next request reconnects with the new values
});
