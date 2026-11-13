// content.js — runs in every http(s) page.
//
// Local context: collects visible text blocks, sends new ones to the service worker, and underlines the
// returned claims with the CSS Custom Highlight API (no DOM edits:
// https://developer.mozilla.org/en-US/docs/Web/API/CSS_Custom_Highlight_API). Hovering a claim shows a
// card with the verdict, confidence + reasoning, explanation, correction, sources and computations.
// Global context: talks only to service_worker.js through a runtime Port. All page/model text is written
// with textContent (never innerHTML), and only http(s) links are rendered, so a malicious page or verdict
// cannot inject markup into the card.
(() => {
  // Guard against double injection (manifest + chrome.scripting fallback). A copy left behind by a reloaded or
  // removed extension is "orphaned": its chrome.runtime.id is gone and any extension API call throws
  // "Extension context invalidated", so it is replaced instead of kept.
  if (window.__factcheck?.alive()) return;
  window.__factcheck?.teardown();
  for (const old of document.querySelectorAll("#fc-card-host")) old.remove();
  let dead = false;
  const alive = () => { try { return !dead && !!chrome.runtime?.id; } catch { return false; } };
  window.__factcheck = { alive, teardown: () => teardown() };

  const DOC_ID = Math.random().toString(36).slice(2, 10);   // new source id per page load
  const LABEL_HL = {
    "Supported": "fc-supported", "Refuted": "fc-refuted", "Not Enough Evidence": "fc-nee",
    "Conflicting Evidence/Cherrypicking": "fc-conflicting",
  };
  const LABEL_COLOR = { "Supported": "#2e9d4f", "Refuted": "#d93025", "Not Enough Evidence": "#80868b",
                        "Conflicting Evidence/Cherrypicking": "#f0a400" };
  const SKIP = "script,style,noscript,textarea,input,select,button,code,pre,svg,nav,footer,[aria-hidden='true'],[contenteditable='true'],#fc-card-host";
  // No length or count limits on text (user decision, 2026-11-13: "just remove the limits, only no text"): a block is
  // left out only when it has no text. Earlier minimum (40 characters) and maximum (4,000) dropped short comments and
  // whole long X posts (one element, 5,451 characters measured), which made picks say "No text" over visible text.

  // One Highlight object per colour, registered once (names match ::highlight() rules in content.css).
  const HL = {};
  for (const name of [...Object.values(LABEL_HL), "fc-checking", "fc-pending"]) {
    HL[name] = new Highlight();
    CSS.highlights.set(name, HL[name]);
  }

  const blocks = new Map();      // blockId -> {el, nodes: Text[]}
  const blockIdOf = new WeakMap();
  let nextBlock = 1;
  const sentTexts = new Map();   // blockId -> last text submitted (to send only new/changed blocks)
  const claims = new Map();      // claimId -> {claim, verdict, status, ranges: Range[]}
  let port = null;
  let autoMode = false;          // default: check only when asked (toolbar button, Alt+Shift+F, right-click menu)

  // ---------------------------------------------------------------------------- port to service worker
  // Opened lazily on the first request and reopened on the next one; no reconnect timer, so an orphaned copy
  // never loops on "Extension context invalidated" (found in the live Chrome test, 2026-11-13).
  function connectPort() {
    if (!alive()) return teardown();
    try {
      port = chrome.runtime.connect({ name: "factcheck" });
    } catch { return teardown(); }
    port.onMessage.addListener(onMessage);
    // chrome.runtime.id is still set while the unloading extension disconnects us, so look again a moment later
    port.onDisconnect.addListener(() => { port = null; setTimeout(() => { if (!alive()) teardown(); }, 1000); });
  }
  function post(msg) {
    if (!port) connectPort();
    try { port?.postMessage({ docId: DOC_ID, ...msg }); } catch { port = null; if (!alive()) teardown(); }
  }

  // Remove our UI and listeners for good (the extension was reloaded or removed).
  function teardown() {
    if (dead) return;
    dead = true;
    for (const [name, hl] of Object.entries(HL)) if (CSS.highlights.get(name) === hl) CSS.highlights.delete(name);
    host.remove();
    observer.disconnect();
    window.removeEventListener("scroll", onScroll);
    document.removeEventListener("mousemove", onMove);
    window.removeEventListener("keydown", onPickKey, true);
  }

  // ---------------------------------------------------------------------------- block collection
  // A block = the nearest non-inline ancestor of some text nodes; its text is ONLY those text nodes
  // (not nested blocks), so nested containers never duplicate text and offsets map 1:1 to Range positions.
  // `keepNode(textNode)`, when given, limits the blocks to a part of the page the user picked (selected text or a
  // dragged box): a block is kept if any of its text nodes is in the part.
  function collectBlocks(onlyVisible, within = document.body, keepNode = null) {
    const displayCache = new Map();
    const isBlockish = (el) => {
      if (!displayCache.has(el)) displayCache.set(el, !/^(inline|contents)/.test(getComputedStyle(el).display));
      return displayCache.get(el);
    };
    const blockOf = (node) => {
      let el = node.parentElement;
      while (el && el !== document.body && !isBlockish(el)) el = el.parentElement;
      return el || document.body;
    };
    const groups = new Map();      // element -> Text[]
    const walker = document.createTreeWalker(within, NodeFilter.SHOW_TEXT, {
      acceptNode(n) {
        if (!n.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
        const p = n.parentElement;
        return p && !p.closest(SKIP) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      },
    });
    while (walker.nextNode()) {
      const el = blockOf(walker.currentNode);
      if (!groups.has(el)) groups.set(el, []);
      groups.get(el).push(walker.currentNode);
    }
    const out = [];
    for (const [el, nodes] of groups) {                   // every group has text: the walker skips blank nodes
      if (keepNode && !nodes.some(keepNode)) continue;
      if (onlyVisible) {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.bottom < -200 || r.top > innerHeight + 200) continue;
      }
      let id = blockIdOf.get(el);
      if (!id) { id = `b${nextBlock++}`; blockIdOf.set(el, id); }
      blocks.set(id, { el, nodes });
      out.push({ id, text: nodes.map((n) => n.nodeValue).join("") });
    }
    return out;
  }

  // Auto-mode rate limit (code review: constantly changing pages such as tickers or chats would otherwise trigger an
  // extraction call on every change): at most one automatic submit per 5 s and 40 per page load.
  const AUTO_MIN_GAP_MS = 5000, AUTO_MAX_PER_PAGE = 40;
  let lastAuto = 0, autoCount = 0, autoPaused = false;

  function submit(force, onlyVisible = true, within = document.body, keep = null) {
    if (siteBlocked) { if (force) toast("this site is on your never-check list (extension Options)"); return; }
    if (!force) {
      if (autoPaused || hasPasswordField()) return;     // never auto-send login/vault pages
      const now = Date.now();
      if (now - lastAuto < AUTO_MIN_GAP_MS) return;
      if (++autoCount > AUTO_MAX_PER_PAGE) { autoPaused = true; toast("auto-check paused on this busy page; press Alt+Shift+F to check it"); return; }
      lastAuto = now;
    }
    const all = collectBlocks(onlyVisible, within, keep);
    const fresh = all.filter((b) => force || sentTexts.get(b.id) !== b.text);
    if (!fresh.length) {
      if (force) toast(keep ? "No text to check in that part. Text in frames, images or canvases is not part of the page; "
                              + "Ctrl+Alt+R reads it from the screen." : "No text to check on this page");
      return 0;
    }
    for (const b of fresh) sentTexts.set(b.id, b.text);
    post({ type: "submit", title: document.title, url: location.href, blocks: fresh, auto: autoMode || force, force });
    return fresh.length;
  }

  function hasPasswordField() {
    return [...document.querySelectorAll("input[type=password]")].some((i) => i.offsetParent !== null);
  }

  // Sites never checked (webmail, messaging, password vaults, sign-in pages); editable in Options.
  const DEFAULT_BLOCKED_SITES = ["mail.google.com", "outlook.live.com", "outlook.office.com", "mail.yahoo.com", "mail.proton.me",
    "web.whatsapp.com", "web.telegram.org", "messages.google.com", "vault.bitwarden.com", "my.1password.com", "keepersecurity.com",
    "accounts.google.com", "login.microsoftonline.com", "login.live.com"];
  let siteBlocked = false;
  function matchesSite(host, site) { site = site.trim().toLowerCase(); return !!site && (host === site || host.endsWith("." + site)); }

  // ---------------------------------------------------------------------------- span -> Range
  function rangeFor(blockId, span) {
    const block = blocks.get(blockId);
    if (!block || !span) return null;
    const text = block.nodes.map((n) => n.nodeValue).join("");
    let start = text.indexOf(span), end = start + span.length;
    if (start < 0) {                                  // tolerate whitespace differences
      const map = []; let squashed = "";
      for (let i = 0; i < text.length; i++) {
        if (/\s/.test(text[i]) && /\s$/.test(squashed)) continue;
        squashed += /\s/.test(text[i]) ? " " : text[i]; map.push(i);
      }
      const want = span.replace(/\s+/g, " ").trim();
      const s = squashed.indexOf(want);
      if (s < 0) return null;
      start = map[s]; end = map[s + want.length - 1] + 1;
    }
    const range = document.createRange();
    let offset = 0, setStart = false;
    for (const node of block.nodes) {
      const len = node.nodeValue.length;
      if (!setStart && start < offset + len) { range.setStart(node, start - offset); setStart = true; }
      if (setStart && end <= offset + len) { range.setEnd(node, end - offset); return range; }
      offset += len;
    }
    return null;
  }

  function rangeAlive(r) { return r && r.startContainer.isConnected && r.endContainer.isConnected && !r.collapsed; }

  // ---------------------------------------------------------------------------- painting
  function paint(rec) {
    for (const hl of Object.values(HL)) for (const r of rec.ranges) hl.delete(r);
    const name = rec.verdict ? LABEL_HL[rec.verdict.label] : rec.status === "checking" ? "fc-checking" : "fc-pending";
    for (const r of rec.ranges) HL[name].add(r);
  }

  function reanchor() {
    for (const rec of claims.values()) {
      if (rec.ranges.every(rangeAlive)) continue;
      const block = blocks.get(rec.claim.block_id);
      if (block && block.el.isConnected) collectBlocks(false, block.el.parentElement || document.body);
      const r = rangeFor(rec.claim.block_id, rec.claim.span);
      for (const hl of Object.values(HL)) for (const old of rec.ranges) hl.delete(old);
      rec.ranges = r ? [r] : [];
      paint(rec);
    }
  }

  function onMessage(msg) {
    if (msg.type === "claims") {
      for (const c of msg.claims) {
        const rec = claims.get(c.id) || { claim: c, verdict: null, status: c.status, ranges: [] };
        rec.claim = c; rec.status = c.status;
        const r = rangeFor(c.block_id, c.span);
        rec.ranges = r ? [r] : [];
        claims.set(c.id, rec);
        paint(rec);
      }
    } else if (msg.type === "verdict") {
      const rec = claims.get(msg.claim_id);
      if (rec) { rec.verdict = msg.verdict; rec.status = "done"; paint(rec); if (card.claimId === msg.claim_id) showCard(rec, card.x, card.y); }
    } else if (msg.type === "status") {
      const rec = msg.claim_id && claims.get(msg.claim_id);
      if (rec) { rec.status = msg.status; rec.error = msg.status === "error" ? msg.message : null; paint(rec); }
      if (msg.status === "error" && msg.message) toast(msg.message);
    } else if (msg.type === "disconnected") {         // from the service worker: app not running / connection lost
      for (const rec of claims.values()) {
        if (rec.status === "checking") { rec.status = "error"; rec.error = msg.message; paint(rec); }
      }
      toast(msg.message);
    }
  }

  // ---------------------------------------------------------------------------- hover card (shadow DOM)
  const host = document.createElement("div");
  host.id = "fc-card-host";
  host.style.cssText = "all: initial; position: fixed; z-index: 2147483647; top: 0; left: 0;";
  // Closed shadow root: page scripts cannot read the card or click "Check this claim" to spend budget (code review).
  const shadow = host.attachShadow({ mode: "closed" });
  let debugExpose = false;                       // extension-only storage flag used by the Playwright test
  // Colours follow the browser's light/dark preference (prefers-color-scheme), with the same light and dark sets as
  // the desktop side panel (ui/panel.py: Material light, Windows 11 dark surfaces + Material dark text/link tones).
  shadow.innerHTML = `<style>
    .card,.toast,.pick{--bg:#fff;--fg:#202124;--muted:#5f6368;--line:#dadce0;--link:#1a73e8;--quote:#3c4043;--corr:#fef7e0;
      --code:#f1f3f4;--track:#eee;--btn:#1a73e8;--btn-fg:#fff;--toast:#202124;--toast-fg:#fff}
    @media (prefers-color-scheme: dark){.card,.toast,.pick{--bg:#2b2b2b;--fg:#e8eaed;--muted:#9aa0a6;--line:#454545;
      --link:#8ab4f8;--quote:#bdc1c6;--corr:#3a3222;--code:#202020;--track:#454545;--btn:#8ab4f8;--btn-fg:#202124;
      --toast:#e8eaed;--toast-fg:#202124}}
    .card{position:fixed;max-width:420px;max-height:70vh;overflow:auto;background:var(--bg);color:var(--fg);border:1px solid var(--line);
      border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.18);padding:12px 14px;font:13px/1.45 system-ui,sans-serif;display:none}
    .row{display:flex;align-items:center;gap:8px;margin-bottom:6px}
    .pill{color:#fff;border-radius:999px;padding:2px 9px;font-weight:600;font-size:12px}
    .bar{flex:1;height:6px;background:var(--track);border-radius:3px;overflow:hidden}.bar>i{display:block;height:100%}
    .muted{color:var(--muted);font-size:12px}.h{font-weight:600;margin-top:8px}
    .src{margin:4px 0;padding-left:8px;border-left:3px solid var(--line)}.src a{color:var(--link);text-decoration:none}
    .q{font-style:italic;color:var(--quote)}.badge{font-size:11px;color:var(--muted);margin-left:4px}
    .corr{background:var(--corr);border-radius:6px;padding:6px 8px;margin-top:6px}
    pre{white-space:pre-wrap;background:var(--code);border-radius:6px;padding:6px;font-size:11px;margin:4px 0}
    button{margin-top:8px;background:var(--btn);color:var(--btn-fg);border:0;border-radius:6px;padding:6px 10px;cursor:pointer}
    button.all{margin-left:8px;background:transparent;color:var(--link);border:1px solid var(--line)}
    .toast{position:fixed;right:16px;bottom:16px;background:var(--toast);color:var(--toast-fg);padding:8px 12px;border-radius:8px;font:13px system-ui;display:none}
    .pick{position:fixed;inset:0;cursor:crosshair;background:rgba(0,0,0,.25);display:none}
    .pick .box{position:fixed;border:2px solid var(--btn);background:rgba(255,255,255,.08);display:none}
    .pick .hint{position:fixed;top:12px;left:50%;transform:translateX(-50%);background:var(--toast);color:var(--toast-fg);
      padding:6px 12px;border-radius:8px;font:13px system-ui;pointer-events:none}
  </style><div class="card" part="card"></div><div class="toast"></div>
  <div class="pick"><div class="box"></div><div class="hint">Drag over the part to fact-check · Esc to cancel</div></div>`;
  document.documentElement.appendChild(host);
  const cardEl = shadow.querySelector(".card");
  const toastEl = shadow.querySelector(".toast");
  const pickEl = shadow.querySelector(".pick"), pickBox = shadow.querySelector(".pick .box");
  const card = { claimId: null, x: 0, y: 0, hideTimer: null };

  function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function safeHref(url) { try { const u = new URL(url); return /^https?:$/.test(u.protocol) ? u.href : null; } catch { return null; } }
  const pct = (x) => `${Math.round((x || 0) * 100)}%`;

  function showCard(rec, x, y) {
    card.claimId = rec.claim.id; card.x = x; card.y = y;
    cardEl.replaceChildren();
    const v = rec.verdict;
    const head = el("div", "row");
    if (v) {
      const pill = el("span", "pill", v.label); pill.style.background = LABEL_COLOR[v.label]; head.append(pill);
      head.append(el("span", null, pct(v.confidence)));
      const bar = el("span", "bar"); const fill = el("i"); fill.style.width = pct(v.confidence);
      fill.style.background = LABEL_COLOR[v.label]; bar.append(fill); head.append(bar);
    } else {
      head.append(el("span", "pill", { checking: "Checking…", error: "Couldn't check" }[rec.status] || "Not checked yet"));
      head.lastChild.style.background = "#80868b";
    }
    cardEl.append(head, el("div", "muted", rec.claim.text));
    if (v) {
      cardEl.append(el("div", "h", "Why this confidence"), el("div", null, v.confidence_reasoning));
      cardEl.append(el("div", "h", "Explanation"), el("div", null, v.explanation));
      if (v.corrected_claim) {
        const box = el("div", "corr");
        box.append(el("b", null, "Correction: "), document.createTextNode(v.corrected_claim));
        if (v.correction_confidence != null) box.append(el("div", "muted", `Confidence ${pct(v.correction_confidence)} — ${v.correction_reasoning || ""}`));
        cardEl.append(box);
      }
      if (v.sources?.length) {
        cardEl.append(el("div", "h", "Sources"));
        for (const s of v.sources) {
          const row = el("div", "src");
          const href = safeHref(s.url);
          const link = el(href ? "a" : "span", null, s.title || s.url);
          if (href) { link.href = href; link.target = "_blank"; link.rel = "noopener noreferrer"; }
          row.append(link, el("span", "badge", s.supports === "correction" ? "· correction" : "· verdict"));
          const qb = s.quote_verified === true ? "✓ quote found on page" : s.quote_verified === false ? "✗ quote not found on page" : "quote not checked";
          row.append(el("span", "badge", `· ${qb}`));
          if (s.in_trace === false) row.append(el("span", "badge", "· not in the agent's search results"));
          if (s.quote) row.append(el("div", "q", `“${s.quote}”`));
          cardEl.append(row);
        }
      }
      if (v.computations?.length) {
        const det = el("details"); det.append(el("summary", null, `Computations (${v.computations.length})`));
        for (const c of v.computations) det.append(el("pre", null, `$ ${c.command}\n${c.output}`));
        cardEl.append(det);
      }
      if (v.flags?.length) cardEl.append(el("div", "muted", `flags: ${v.flags.join(", ")}`));
    } else if (rec.status !== "checking") {
      if (rec.error) cardEl.append(el("div", "h", "Why"), el("div", null, rec.error));   // e.g. the daily budget is used up
      const b = el("button", null, rec.error ? "Try again" : "Check this claim");
      b.addEventListener("click", () => { checkClaim(rec); showCard(rec, x, y); });
      cardEl.append(b);
      const todo = unchecked().length;
      if (todo > 1) {                                            // presses every Check / Try again button on the page
        const all = el("button", "all", `Check all ${todo}`);
        all.addEventListener("click", () => { checkAll(); showCard(rec, x, y); });
        cardEl.append(all);
      }
    }
    cardEl.style.display = "block";
    if (debugExpose) {                                           // test-only mirror (closed shadow root)
      const cs = getComputedStyle(cardEl);
      host.dataset.debugCard = cardEl.innerText;
      host.dataset.debugCardColors = `${cs.backgroundColor}|${cs.color}`;
    }
    const w = cardEl.offsetWidth, h = cardEl.offsetHeight;
    cardEl.style.left = `${Math.max(8, Math.min(x + 12, innerWidth - w - 8))}px`;
    cardEl.style.top = `${y + 18 + h > innerHeight ? Math.max(8, y - h - 12) : y + 18}px`;
  }

  function hideCardSoon() {
    clearTimeout(card.hideTimer);
    card.hideTimer = setTimeout(() => { cardEl.style.display = "none"; card.claimId = null; delete host.dataset.debugCard; }, 350);
  }
  cardEl.addEventListener("mouseenter", () => clearTimeout(card.hideTimer));
  cardEl.addEventListener("mouseleave", hideCardSoon);

  function claimAtPoint(x, y) {
    const pos = document.caretPositionFromPoint ? document.caretPositionFromPoint(x, y) : null;
    const node = pos ? pos.offsetNode : document.caretRangeFromPoint?.(x, y)?.startContainer;
    const offset = pos ? pos.offset : document.caretRangeFromPoint?.(x, y)?.startOffset;
    if (!node) return null;
    for (const rec of claims.values()) {
      for (const r of rec.ranges) {
        if (!rangeAlive(r)) continue;
        try { if (!r.isPointInRange(node, offset)) continue; } catch { continue; }
        for (const rect of r.getClientRects()) {        // caret snaps to nearest text: confirm the pointer is on it
          if (x >= rect.left - 2 && x <= rect.right + 2 && y >= rect.top - 2 && y <= rect.bottom + 2) return rec;
        }
      }
    }
    return null;
  }

  let moveQueued = false;
  function onMove(e) {
    if (!alive()) return teardown();               // orphaned copy (extension reloaded/removed): clean up on first use
    if (moveQueued) return;
    moveQueued = true;
    requestAnimationFrame(() => {
      moveQueued = false;
      const rec = claimAtPoint(e.clientX, e.clientY);
      if (rec) { clearTimeout(card.hideTimer); if (card.claimId !== rec.claim.id) showCard(rec, e.clientX, e.clientY); }
      else if (card.claimId) hideCardSoon();
    });
  }
  document.addEventListener("mousemove", onMove, { passive: true });

  function toast(text) {
    toastEl.textContent = `Fact check: ${text}`;
    toastEl.style.display = "block";
    if (debugExpose) host.dataset.debugToast = toastEl.textContent;   // test-only mirror (closed shadow root)
    setTimeout(() => { toastEl.style.display = "none"; }, 6000);
  }

  // ---------------------------------------------------------------------------- check claims on request
  // Claims beyond the first few found are not verified automatically; each shows "Check this claim" (or "Try again"
  // after an error). "Check all" presses every one of those buttons (user request, 2026-11-13).
  const unchecked = () => [...claims.values()].filter((r) => !r.verdict && r.status !== "checking" && r.status !== "done");

  function checkClaim(rec) {
    post({ type: "check", claimId: rec.claim.id });
    rec.status = "checking";
    rec.error = null;
    paint(rec);
  }

  function checkAll() {
    const todo = unchecked();
    if (!todo.length) return toast("No unchecked claims on this page. Press Alt+Shift+F to find claims first.");
    for (const rec of todo) checkClaim(rec);
    toast(`checking ${todo.length} claim${todo.length === 1 ? "" : "s"}`);
  }

  // ---------------------------------------------------------------------------- check part of the page
  // Alt+Shift+S / popup "Check part of page…" / right-click "Fact-check selected text". Whole blocks (paragraphs)
  // are sent, as for a page check, so claims keep their context and underlines map to exact DOM ranges.
  const PICK_MIN_PX = 8;                 // a smaller box is a click, which cancels (same rule as the desktop picker)
  let pickFrom = null;

  // Blocks with a text node inside the selection (https://developer.mozilla.org/en-US/docs/Web/API/Range/intersectsNode),
  // instead of every block under the selection's common ancestor, which for a multi-paragraph selection was the
  // whole article (e2e test, 2026-11-13).
  function checkRange(range) {
    return submit(true, false, document.body, (n) => range.intersectsNode(n));
  }

  const paragraphs = (n) => `${n} paragraph${n === 1 ? "" : "s"}`;

  // The selection is used up once checked: it stayed selected (sometimes only in inactive grey), so the next
  // Alt+Shift+S silently re-checked it instead of opening the box picker (user report + log, 2026-11-13).
  function checkSelection() {
    const sel = getSelection();
    if (!sel || !sel.rangeCount || sel.isCollapsed) return false;
    const n = checkRange(sel.getRangeAt(0));
    sel.removeAllRanges();
    if (n) toast(`checking ${paragraphs(n)} from your selection`);
    return true;
  }

  // True when a line box of a text node overlaps the box AND that text is what is shown there (client coordinates;
  // https://developer.mozilla.org/en-US/docs/Web/API/Range/getClientRects). Line boxes are not clipped: text
  // scrolled out of a scroll container, or covered by a sticky header or pop-up, still has rects under the box
  // (e2e test: 5 hidden lines of a scrolled block lay behind a canvas). So the overlap's centre is hit-tested with
  // document.elementFromPoint (https://developer.mozilla.org/en-US/docs/Web/API/Document/elementFromPoint); the
  // picker layer is already hidden when this runs.
  function textTouches(node, box) {
    const r = document.createRange();
    r.selectNodeContents(node);
    for (const c of r.getClientRects()) {
      const left = Math.max(c.left, box.left), right = Math.min(c.right, box.right);
      const top = Math.max(c.top, box.top), bottom = Math.min(c.bottom, box.bottom);
      if (left >= right || top >= bottom) continue;
      const hit = document.elementFromPoint((left + right) / 2, (top + bottom) / 2);
      if (hit && node.parentElement.contains(hit)) return true;
    }
    return false;
  }

  const boxOf = (a, b) => ({ left: Math.min(a.x, b.x), top: Math.min(a.y, b.y), right: Math.max(a.x, b.x), bottom: Math.max(a.y, b.y) });

  function startPicker() {
    pickFrom = null;
    pickBox.style.display = "none";
    pickEl.style.display = "block";
    if (debugExpose) host.dataset.debugPicking = "1";        // test-only mirror (closed shadow root)
    window.addEventListener("keydown", onPickKey, true);     // capture phase: Esc reaches us before the page
  }

  function stopPicker() {
    pickFrom = null;
    pickEl.style.display = "none";
    delete host.dataset.debugPicking;
    window.removeEventListener("keydown", onPickKey, true);
  }

  function onPickKey(e) {
    if (e.key !== "Escape") return;
    e.preventDefault();
    e.stopPropagation();
    stopPicker();
  }

  pickEl.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();                                       // no text selection while dragging
    pickFrom = { x: e.clientX, y: e.clientY };
  });
  pickEl.addEventListener("mousemove", (e) => {
    if (!pickFrom) return;
    const b = boxOf(pickFrom, { x: e.clientX, y: e.clientY });
    Object.assign(pickBox.style, { display: "block", left: `${b.left}px`, top: `${b.top}px`,
                                   width: `${b.right - b.left}px`, height: `${b.bottom - b.top}px` });
  });
  pickEl.addEventListener("mouseup", (e) => {
    if (!pickFrom) return;
    const b = boxOf(pickFrom, { x: e.clientX, y: e.clientY });
    stopPicker();
    if (b.right - b.left < PICK_MIN_PX || b.bottom - b.top < PICK_MIN_PX) return;
    const n = submit(true, false, document.body, (node) => textTouches(node, b));
    if (n) toast(`checking ${paragraphs(n)} in the box`);
  });
  pickEl.addEventListener("contextmenu", (e) => { e.preventDefault(); stopPicker(); });   // right-click cancels

  function checkPart() {
    if (siteBlocked) return toast("this site is on your never-check list (extension Options)");
    if (pickEl.style.display === "block") return stopPicker();   // pressing the key again closes the picker
    if (!checkSelection()) startPicker();
  }

  // ---------------------------------------------------------------------------- triggers
  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
  const onScroll = debounce(() => { if (autoMode) submit(false); }, 1200);
  const onMutate = debounce(() => { reanchor(); if (autoMode) submit(false); }, 1500);
  window.addEventListener("scroll", onScroll, { passive: true });
  const observer = new MutationObserver((records) => {
    if (records.every((r) => host.contains(r.target))) return;   // ignore our own card updates
    onMutate();
  });
  observer.observe(document.body, { childList: true, subtree: true, characterData: true });

  // Settings first: a check must never run before the never-check site list is known.
  const settingsReady = chrome.storage.local.get({ autoMode: false, blockedSites: DEFAULT_BLOCKED_SITES, debugExposeCard: false })
    .then((s) => {
      autoMode = s.autoMode;
      debugExpose = !!s.debugExposeCard;
      siteBlocked = s.blockedSites.some((site) => matchesSite(location.hostname.toLowerCase(), site));
      if (siteBlocked || !autoMode) return;       // manual mode: nothing is sent until the user asks
      connectPort();
      setTimeout(() => submit(false), 1200);
    });

  chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
    settingsReady.then(() => {
      if (msg.type === "checkPage") submit(true);
      if (msg.type === "checkPart") checkPart();
      if (msg.type === "checkAll") checkAll();                // popup: "Check all claims on this page"
      if (msg.type === "checkSelection") checkSelection();    // right-click menu: only shown when text is selected
      reply({ ok: true });                        // lets the sender know a live content script handled it
    });
    return true;                                  // reply asynchronously
  });
  chrome.storage.onChanged.addListener((c) => { if (c.autoMode) autoMode = c.autoMode.newValue; });
})();
