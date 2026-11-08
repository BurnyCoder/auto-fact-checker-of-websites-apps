// service_worker.js — the extension's background script.
//
// Local context: keeps ONE authenticated WebSocket to the local fact-check app (ws://127.0.0.1:8765/ws),
// relays requests from content scripts (one runtime Port per tab/frame) and routes the app's events back
// to the Port that owns each source.
// Global context: the content script never talks to localhost itself (Chrome's Local Network Access
// prompts would hit pages, https://developer.chrome.com/blog/local-network-access). MV3 workers stay
// alive while a WebSocket is active (Chrome 116+), and a 20 s ping keeps traffic flowing
// (https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle).

const DEFAULTS = { serverUrl: "ws://127.0.0.1:8765/ws", token: "", autoMode: true };
const KEEPALIVE_MS = 20000;          // ping interval
const MAX_PENDING = 50;              // messages buffered while disconnected

let ws = null;                       // the WebSocket (or null)
let ready = false;                   // true after the server's "hello" (auth accepted)
let backoffMs = 1000;                // reconnect delay, doubles up to 30 s
let keepaliveTimer = null;
const pending = [];                  // outgoing messages queued until connected
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

async function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  const s = await getSettings();
  if (!s.token) { setStatus("no-token"); return; }          // user must paste the token in Options
  ws = new WebSocket(s.serverUrl);
  ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token: s.token }));
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "hello") {                              // auth accepted
      ready = true; backoffMs = 1000; setStatus("connected");
      clearInterval(keepaliveTimer);
      keepaliveTimer = setInterval(() => send({ type: "ping" }), KEEPALIVE_MS);
      while (pending.length) ws.send(JSON.stringify(pending.shift()));
      return;
    }
    if (msg.type === "pong") return;
    const port = ports.get(msg.source_id);                   // route to the owning tab
    if (port) { try { port.postMessage(msg); } catch (e) { /* tab went away */ } }
  };
  ws.onclose = (ev) => {
    ready = false; ws = null; clearInterval(keepaliveTimer);
    setStatus(ev.code === 4401 ? "bad-token" : "disconnected");
    if (ev.code !== 4401) setTimeout(connect, backoffMs);    // don't hammer with a bad token
    backoffMs = Math.min(backoffMs * 2, 30000);
  };
  ws.onerror = () => { /* onclose follows and handles reconnects */ };
}

function send(obj) {
  if (ready && ws && ws.readyState === WebSocket.OPEN) { ws.send(JSON.stringify(obj)); return; }
  if (obj.type !== "ping") { pending.push(obj); if (pending.length > MAX_PENDING) pending.shift(); }
  connect();
}

// ---- content-script Ports: one per tab/frame ---------------------------------------------------------
chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "factcheck" || !port.sender?.tab) return;
  const tabId = port.sender.tab.id;
  portSources.set(port, new Set());
  port.onMessage.addListener((msg) => {
    const sourceId = `tab${tabId}:${msg.docId || "doc"}`;    // unique per page load
    if (msg.type === "submit") {
      ports.set(sourceId, port);
      portSources.get(port).add(sourceId);
      send({ type: "submit", source: { source_id: sourceId, title: msg.title, url: msg.url },
             blocks: msg.blocks, auto: msg.auto, force: msg.force });
    } else if (msg.type === "check") {
      send({ type: "check", claim_id: msg.claimId });
    }
  });
  port.onDisconnect.addListener(() => {                      // tab closed or navigated: stop its work
    for (const sid of portSources.get(port) || []) { send({ type: "cancel", source_id: sid }); ports.delete(sid); }
    portSources.delete(port);
  });
  connect();
});

// ---- context menu, keyboard command, popup status ----------------------------------------------------
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({ id: "fc-selection", title: "Fact-check selected text", contexts: ["selection"] });
});
chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "fc-selection" && tab?.id != null) chrome.tabs.sendMessage(tab.id, { type: "checkSelection" });
});
chrome.commands.onCommand.addListener(async (command) => {
  if (command !== "check-page") return;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.id != null) chrome.tabs.sendMessage(tab.id, { type: "checkPage" });
});
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg?.type === "status") { reply({ status: lastStatus }); return true; }
  if (msg?.type === "reconnect") { if (ws) ws.close(); else connect(); reply({ ok: true }); return true; }
  return false;
});
chrome.storage.onChanged.addListener((changes) => {
  if (changes.token || changes.serverUrl) { if (ws) ws.close(); setTimeout(connect, 100); }
});

connect();
