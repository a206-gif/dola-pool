(function () {
  "use strict";
  const INSTALL = "__doubaoDolaMediaCaptureV1620";
  const APIS_ATTRIBUTE = "data-dola-media-fallback-apis";
  const APIS_EVENT = "dola-media-fallback-apis";
  const FORCE_ATTRIBUTE = "data-watermark-free-media-force";
  const STATUS_ATTRIBUTE = "data-watermark-free-media-capture";
  const STATUS_EVENT = "watermark-free-media-capture-status";
  const RETRY_EVENT = "watermark-free-media-retry";
  const LIMIT = 8;
  const TAIL_LIMIT = 65536;
  const supported = (host) => /(^|\.)(doubao|dola)\.com$/i.test(host);
  if (!supported(location.hostname) || globalThis[INSTALL]) return;
  globalThis[INSTALL] = true;

  const state = { version: "1.6.21", installed: true, observedResponses: 0,
    capturedCandidates: 0, captureEvents: 0, bytesRead: 0, transport: "", lastStatus: 0, error: "" };
  const recent = new Set();
  const pending = new Set();
  const seenAt = new Map();
  const xhrStates = new WeakMap();
  let page = location.href;

  function publish() {
    document.documentElement?.setAttribute(STATUS_ATTRIBUTE, JSON.stringify(state));
    document.dispatchEvent(new Event(STATUS_EVENT));
  }

  function syncPage() {
    if (page === location.href) return;
    page = location.href;
    recent.clear(); pending.clear(); seenAt.clear();
    state.observedResponses = 0; state.capturedCandidates = 0; state.captureEvents = 0;
    state.bytesRead = 0; state.error = "";
    document.documentElement?.removeAttribute(APIS_ATTRIBUTE);
    document.documentElement?.removeAttribute(FORCE_ATTRIBUTE);
  }

  function endpoint(input) {
    try {
      const raw = typeof input === "string" ? input : input?.url || input?.href || "";
      const url = new URL(raw, location.href);
      return supported(url.hostname) && /^https?:$/.test(url.protocol) &&
        /(?:^|\/)(?:im\/|chat\/completion(?:\/|$))/.test(url.pathname);
    } catch { return false; }
  }

  function flush(force = false) {
    const root = document.documentElement;
    if (!root || !pending.size) return;
    let existing = [];
    try { existing = JSON.parse(root.getAttribute(APIS_ATTRIBUTE) || "[]"); } catch { /* Start a new batch. */ }
    const values = [...new Set([...(Array.isArray(existing) ? existing : []), ...pending])].slice(-LIMIT);
    pending.clear();
    root.setAttribute(APIS_ATTRIBUTE, JSON.stringify(values));
    if (force) root.setAttribute(FORCE_ATTRIBUTE, "1");
    state.captureEvents += 1;
    publish();
    document.dispatchEvent(new Event(APIS_EVENT));
  }

  function decoded(value) {
    let text = String(value || "");
    for (let count = 0; count < 5; count += 1) {
      try {
        const next = JSON.parse(`"${text.replace(/"/g, '\\"')}"`);
        if (next === text) break;
        text = next;
      } catch { break; }
    }
    return text.replace(/\\u0026/gi, "&").replace(/\\u003d/gi, "=").replace(/\\\//g, "/");
  }

  function capture(text) {
    const patterns = [/fallback_api\\*"\s*:\s*\\*"(.+?)\\*"/g, /"fallback_api"\s*:\s*"([^"]+)"/g];
    for (const pattern of patterns) {
      for (const match of String(text || "").matchAll(pattern)) {
        try {
          const url = new URL(decoded(match[1]));
          if (!/^https?:$/.test(url.protocol)) continue;
          const value = url.toString();
          const now = Date.now();
          if (now - (seenAt.get(value) || 0) < 1500) continue;
          seenAt.set(value, now);
          while (seenAt.size > 200) seenAt.delete(seenAt.keys().next().value);
          recent.delete(value); recent.add(value);
          while (recent.size > LIMIT) recent.delete(recent.values().next().value);
          pending.add(value);
        } catch { /* Ignore malformed candidates; never guess a URL. */ }
      }
    }
    state.capturedCandidates = recent.size;
    flush();
  }

  function scanner() {
    let tail = "";
    return (text) => {
      for (let offset = 0; offset < text.length; offset += TAIL_LIMIT) {
        const part = text.slice(offset, offset + TAIL_LIMIT);
        capture(tail + part);
        tail = (tail + part).slice(-TAIL_LIMIT);
      }
    };
  }

  async function observeResponse(response, capturedPage) {
    if (capturedPage !== location.href) return;
    state.observedResponses += 1; state.transport = "fetch"; state.lastStatus = response?.status || 0;
    publish();
    if (!response?.ok) { state.error = `HTTP_${response?.status || 0}`; publish(); return; }
    const type = response.headers?.get("content-type") || "";
    if (type && !/json|event-stream|text\/plain/i.test(type)) return;
    let reader;
    try {
      const copy = response.clone();
      const scan = scanner();
      if (!copy.body?.getReader) { scan(await copy.text()); return; }
      reader = copy.body.getReader();
      const decoder = new TextDecoder();
      while (true) {
        const result = await reader.read();
        if (capturedPage !== location.href) { void reader.cancel().catch(() => {}); break; }
        if (result.done) { scan(decoder.decode()); break; }
        state.bytesRead += result.value.byteLength;
        scan(decoder.decode(result.value, { stream: true }));
      }
      state.error = "";
    } catch {
      state.error = "RESPONSE_READ_FAILED";
    } finally {
      try { reader?.releaseLock(); } catch { /* Reader may have been canceled. */ }
      publish();
    }
  }

  const nativeFetch = window.fetch;
  if (typeof nativeFetch === "function") {
    window.fetch = function passiveMediaFetch(input) {
      syncPage();
      const watched = endpoint(input);
      const capturedPage = location.href;
      const result = nativeFetch.apply(this, arguments);
      if (watched) {
        // Register on the returned promise itself. Assimilating a foreign promise
        // first can let the page consume the body before our clone is created.
        if (typeof result?.then === "function") {
          result.then((response) => { void observeResponse(response, capturedPage).catch(() => {}); }, () => {});
        } else void observeResponse(result, capturedPage).catch(() => {});
      }
      return result;
    };
  }

  if (typeof XMLHttpRequest !== "undefined") {
    const proto = XMLHttpRequest.prototype;
    const nativeOpen = proto.open;
    proto.open = function passiveMediaOpen(method, url) {
      const previous = xhrStates.get(this);
      if (previous) for (const event of ["readystatechange", "progress", "load"]) this.removeEventListener(event, previous.observe);
      xhrStates.delete(this);
      const result = nativeOpen.apply(this, arguments);
      syncPage();
      if (!endpoint(url)) return result;
      const local = { page: location.href, offset: 0, jsonRead: false, started: false, scan: scanner(), observe: null };
      local.observe = () => {
        if (local.page !== location.href || this.readyState < 3) return;
        if (!local.started) {
          local.started = true; state.observedResponses += 1; state.transport = "xhr";
        }
        state.lastStatus = this.status;
        if (this.status < 200 || this.status >= 300) { state.error = `HTTP_${this.status}`; publish(); return; }
        try {
          if (this.responseType === "json") {
            if (this.readyState === 4 && !local.jsonRead) {
              local.jsonRead = true;
              local.scan(JSON.stringify(this.response || {}));
            }
          } else if (!this.responseType || this.responseType === "text") {
            const raw = this.responseText;
            const next = raw.slice(local.offset);
            local.offset = raw.length; state.bytesRead += next.length;
            local.scan(next);
          }
          state.error = "";
        } catch { state.error = "RESPONSE_READ_FAILED"; }
        publish();
      };
      xhrStates.set(this, local);
      for (const event of ["readystatechange", "progress", "load"]) this.addEventListener(event, local.observe);
      return result;
    };
  }

  function bootstrapJson() {
    // Existing conversations may be hydrated from server-delivered JSON rather
    // than making a new history request. Read JSON scripts, never rendered text.
    if (typeof document.querySelectorAll !== "function") return;
    for (const node of document.querySelectorAll('script[type="application/json"]')) {
      const text = String(node.textContent || "");
      if (!text.includes("fallback_api") || text.length > 2_000_000) continue;
      try {
        JSON.parse(text);
        state.transport = "bootstrap-json";
        scanner()(text);
      } catch { /* Not JSON metadata; do not interpret arbitrary script source. */ }
    }
  }

  document.addEventListener("DOMContentLoaded", () => { bootstrapJson(); flush(); publish(); }, { once: true });
  if (document.readyState && document.readyState !== "loading") bootstrapJson();
  document.addEventListener(RETRY_EVENT, () => {
    syncPage();
    if (!recent.size) bootstrapJson();
    if (!recent.size) { state.error = "REOPEN_VIDEO_CONVERSATION"; publish(); return; }
    for (const url of recent) pending.add(url);
    flush(true);
  });
  Object.defineProperty(window, "__DOUBAO_DOLA_MEDIA_CAPTURE__", {
    configurable: true, get: () => JSON.parse(JSON.stringify(state))
  });
  publish();
})();
