(function () {
  "use strict";

  const INSTALL_MARK = "__dolaNative30SkillPackInstalledV150";
  const DURATION_KEY = "intl_doubao_enable_30s_v1";
  const TARGET_DURATION_KEY = "intl_doubao_target_duration_v1";
  const READY_ATTRIBUTE = "data-dola-native-30-ready";
  const STATUS_EVENT = "dola-native-30-status";
  const TARGET_MODEL = "seedance_v2.5";
  const MEDIA_APIS_ATTRIBUTE = "data-dola-media-fallback-apis";
  const MEDIA_APIS_EVENT = "dola-media-fallback-apis";

  if (globalThis[INSTALL_MARK]) return;
  globalThis[INSTALL_MARK] = true;

  // 扩展由 manifest matches 限定仅 dola.com；软件（WebView2）对所有页面注入本脚本，
  // 故这里补 host 守卫，两种宿主下行为一致：仅在 Dola / 豆包域内 hook。
  const host = location.hostname.replace(/^www\./i, "").toLowerCase();
  if (!(host === "dola.com" || host.endsWith(".dola.com") ||
        host === "doubao.com" || host.endsWith(".doubao.com"))) {
    return;
  }

  const originalFetch = window.fetch;
  if (typeof originalFetch !== "function") return;

  function requestUrl(input) {
    return typeof input === "string" ? input : (input && input.url) || "";
  }

  function isSkillPackUrl(rawUrl) {
    try {
      return new URL(rawUrl, location.href).pathname.replace(/\/$/, "") === "/samantha/skill/pack";
    } catch {
      return false;
    }
  }

  function isActionBarConfigUrl(rawUrl) {
    try {
      return new URL(rawUrl, location.href).pathname.replace(/\/$/, "") ===
        "/alice/slot/action_bar_v3/get_item_conf";
    } catch {
      return false;
    }
  }

  function isChainSingleUrl(rawUrl) {
    try {
      return new URL(rawUrl, location.href).pathname.replace(/\/$/, "") === "/im/chain/single";
    } catch {
      return false;
    }
  }

  function decodeEscapedFragment(value) {
    let text = String(value || "");
    for (let index = 0; index < 3; index += 1) {
      try {
        const decoded = JSON.parse(`"${text.replace(/"/g, '\\"')}"`);
        if (decoded === text) break;
        text = decoded;
      } catch {
        break;
      }
    }
    return text.replace(/\\u0026/gi, "&").replace(/\\u003d/gi, "=").replace(/\\\//g, "/");
  }

  function publishFallbackApis(rawText) {
    const urls = new Set();
    const patterns = [
      /fallback_api\\*"\s*:\s*\\*"(.+?)\\*"/g,
      /"fallback_api"\s*:\s*"([^"]+)"/g
    ];
    for (const pattern of patterns) {
      let match = pattern.exec(String(rawText || ""));
      while (match && urls.size < 8) {
        const value = decodeEscapedFragment(match[1]);
        try {
          const parsed = new URL(value);
          if (parsed.protocol === "https:" || parsed.protocol === "http:") urls.add(parsed.toString());
        } catch {
          // Ignore malformed candidates from unrelated response fields.
        }
        match = pattern.exec(String(rawText || ""));
      }
    }
    if (!urls.size || !document.documentElement) return;
    const list = Array.from(urls);
    document.documentElement.setAttribute(MEDIA_APIS_ATTRIBUTE, JSON.stringify(list));
    document.dispatchEvent(new Event(MEDIA_APIS_EVENT));
    // 宿主软件资源栏（WebView2）：与 C# Network 被动听互补，覆盖 SPA 漏听。
    // 扩展环境不存在 chrome.webview，自动跳过。
    try {
      if (globalThis.chrome && chrome.webview && typeof chrome.webview.postMessage === "function") {
        chrome.webview.postMessage({ type: "dola-fallback-apis", urls: list });
      }
    } catch {
      // 非 WebView2 或宿主未启用 WebMessage 时忽略。
    }
  }

  async function observeChainResponse(response) {
    try {
      publishFallbackApis(await response.clone().text());
    } catch {
      // Passive capture must never affect the page response.
    }
  }

  function modeEnabled() {
    try {
      return localStorage.getItem(DURATION_KEY) === "1";
    } catch {
      return false;
    }
  }

  function targetDuration() {
    try {
      const value = Number(localStorage.getItem(TARGET_DURATION_KEY));
      if (Number.isInteger(value) && value >= 4 && value <= 30) return value;
    } catch {
      // Fall through to the legacy 30-second switch.
    }
    return modeEnabled() ? 30 : 30;
  }

  function publishReady(ready, reason = "") {
    document.documentElement?.setAttribute(READY_ATTRIBUTE, ready ? "1" : "0");
    document.dispatchEvent(new CustomEvent(STATUS_EVENT, {
      detail: { ready: Boolean(ready), reason: String(reason || "") }
    }));
  }

  function patchDurationOptions(node) {
    let patched = false;
    const seen = new Set();

    function walk(value) {
      if (!value || typeof value !== "object" || seen.has(value)) return;
      seen.add(value);

      if (value.value === "duration" && Array.isArray(value.options)) {
        const template = value.options.find((option) => String(option?.value) === "10") || value.options[0] || {};
        const selected = targetDuration();
        const extras = [15, 30];
        if (!extras.includes(selected)) extras.push(selected);
        for (const seconds of extras) {
          if (!value.options.some((option) => String(option?.value) === String(seconds))) {
            value.options.push({
              ...template,
              show_name: `${seconds}s`,
              value: String(seconds),
              is_default: false,
              sub_display: template.sub_display || ""
            });
          }
        }
        for (const option of value.options) option.is_default = Number(option?.value) === selected;
        patched = true;
      }

      if (value.model_capability && typeof value.model_capability === "object") {
        const capability = value.model_capability[TARGET_MODEL];
        if (capability && typeof capability === "object") {
          const durations = Array.isArray(capability.supported_durations)
            ? capability.supported_durations.map(String)
            : [];
          const supported = ["15", "30", String(targetDuration())];
          for (const seconds of supported) {
            if (!durations.includes(seconds)) durations.push(seconds);
          }
          capability.supported_durations = durations;
          patched = true;
        }
      }

      for (const child of Object.values(value)) walk(child);
    }

    walk(node);
    return patched;
  }

  async function patchSkillPackResponse(response) {
    try {
      const json = await response.clone().json();
      const patched = patchDurationOptions(json);
      publishReady(patched, patched ? "" : "能力响应中未找到视频时长配置");
      if (!patched) return response;
      const headers = new Headers(response.headers);
      headers.delete("content-length");
      headers.delete("content-encoding");
      headers.set("content-type", "application/json; charset=utf-8");
      return new Response(JSON.stringify(json), {
        status: response.status,
        statusText: response.statusText,
        headers
      });
    } catch (error) {
      publishReady(false, error?.message || String(error));
      return response;
    }
  }

  function patchSkillPackText(rawText) {
    try {
      const json = JSON.parse(rawText);
      const patched = patchDurationOptions(json);
      publishReady(patched, patched ? "" : "能力响应中未找到视频时长配置");
      return patched ? JSON.stringify(json) : rawText;
    } catch (error) {
      publishReady(false, error?.message || String(error));
      return rawText;
    }
  }

  function patchActionBarConfig(node) {
    let patched = false;
    const seen = new Set();

    function walk(value, parent = null, parentKey = "") {
      if (typeof value === "string") {
        const trimmed = value.trim();
        if ((trimmed.startsWith("{") || trimmed.startsWith("[")) &&
            (trimmed.includes("video-duration") || trimmed.includes("video-model"))) {
          try {
            const parsed = JSON.parse(value);
            const changed = walk(parsed);
            if (changed && parent) parent[parentKey] = JSON.stringify(parsed);
            return changed;
          } catch {
            return false;
          }
        }
        return false;
      }
      if (!value || typeof value !== "object" || seen.has(value)) return false;
      seen.add(value);
      let changed = false;

      if (value.key === "video-duration" && Array.isArray(value.option_list)) {
        const template = value.option_list.find((option) => String(option?.option_key) === "10") || value.option_list[0] || {};
        const selected = targetDuration();
        const extras = [15, 30];
        if (!extras.includes(selected)) extras.push(selected);
        for (const seconds of extras) {
          if (!value.option_list.some((option) => Number(option?.option_key) === seconds)) {
            value.option_list.push({
              ...template,
              id: Math.max(0, ...value.option_list.map((option) => Number(option?.id) || 0)) + 1,
              display_text: `${seconds}s`,
              message_text: "",
              option_key: String(seconds)
            });
          }
        }
        const selectedIndex = value.option_list.findIndex((option) => Number(option?.option_key) === selected);
        if (selectedIndex >= 0) {
          value.default_selection_index = selectedIndex;
          if (value.option_list[selectedIndex]?.id != null) value.default_selection_id = value.option_list[selectedIndex].id;
        }
        changed = true;
      }

      // 新版创作页的时长是 4~10 秒滑杆，没有 15/30 选项。扩展上限并写入地址栏里的秒数，
      // 让页面自己签名提交，不在生成请求发出后改 body。
      if (value.key === "video-duration" && !Array.isArray(value.option_list)) {
        const hasRangeShape = [
          "lower_bound", "upper_bound", "step_length", "default_selected_duration",
          "lowerBound", "upperBound", "stepLength", "defaultSelectedDuration"
        ].some((key) => Object.hasOwn(value, key));
        if (hasRangeShape) {
          const selected = targetDuration();
          if (Object.hasOwn(value, "upper_bound")) value.upper_bound = Math.max(Number(value.upper_bound) || 0, 30);
          if (Object.hasOwn(value, "upperBound")) value.upperBound = Math.max(Number(value.upperBound) || 0, 30);
          if (!Object.hasOwn(value, "upper_bound") && !Object.hasOwn(value, "upperBound")) value.upper_bound = 30;
          if (Object.hasOwn(value, "default_selected_duration")) value.default_selected_duration = selected;
          if (Object.hasOwn(value, "defaultSelectedDuration")) value.defaultSelectedDuration = selected;
          if (!Object.hasOwn(value, "default_selected_duration") && !Object.hasOwn(value, "defaultSelectedDuration")) {
            value.default_selected_duration = selected;
          }
          changed = true;
        }
      }

      if (value.key === "video-model" && Array.isArray(value.option_list)) {
        const modelIndex = value.option_list.findIndex((option) => option?.option_key === TARGET_MODEL);
        if (modelIndex >= 0) {
          value.default_selection_index = modelIndex;
          value.default_selection_id = value.option_list[modelIndex].id;
          changed = true;
        }
      }

      for (const [key, child] of Object.entries(value)) {
        changed = walk(child, value, key) || changed;
      }
      return changed;
    }

    patched = walk(node);
    return patched;
  }

  async function patchActionBarResponse(response) {
    try {
      const json = await response.clone().json();
      patchActionBarConfig(json);
      const headers = new Headers(response.headers);
      headers.delete("content-length");
      headers.delete("content-encoding");
      headers.set("content-type", "application/json; charset=utf-8");
      return new Response(JSON.stringify(json), {
        status: response.status,
        statusText: response.statusText,
        headers
      });
    } catch {
      return response;
    }
  }

  function patchActionBarText(rawText) {
    try {
      const json = JSON.parse(rawText);
      patchActionBarConfig(json);
      return JSON.stringify(json);
    } catch {
      return rawText;
    }
  }

  window.fetch = function dolaNative30Fetch(input, init = {}) {
    const url = requestUrl(input);
    const result = originalFetch.call(this, input, init);
    if (isSkillPackUrl(url)) return Promise.resolve(result).then(patchSkillPackResponse);
    if (isActionBarConfigUrl(url)) return Promise.resolve(result).then(patchActionBarResponse);
    if (isChainSingleUrl(url)) Promise.resolve(result).then(observeChainResponse).catch(() => {});
    return result;
  };

  // Dola 的不同构建版本可能用 XMLHttpRequest 获取能力配置。与 fetch 一样，
  // 只改未签名的能力配置响应；绝不改生成提交请求。
  const xhrOpen = XMLHttpRequest.prototype.open;
  const responseTextGetter = Object.getOwnPropertyDescriptor(XMLHttpRequest.prototype, "responseText")?.get;
  const responseGetter = Object.getOwnPropertyDescriptor(XMLHttpRequest.prototype, "response")?.get;
  XMLHttpRequest.prototype.open = function dolaNative30XhrOpen(method, url) {
    const requestUrl = String(url || "");
    const skillRequest = isSkillPackUrl(requestUrl);
    const actionBarRequest = isActionBarConfigUrl(requestUrl);
    const chainRequest = isChainSingleUrl(requestUrl);
    if (chainRequest) {
      this.addEventListener("load", () => {
        try {
          const raw = typeof this.responseText === "string"
            ? this.responseText
            : JSON.stringify(this.response || "");
          publishFallbackApis(raw);
        } catch {
          // Read-only fallback; never block or replace the XHR response.
        }
      }, { once: true });
    }
    if (skillRequest || actionBarRequest) {
      if (responseTextGetter) {
        try {
          Object.defineProperty(this, "responseText", {
            configurable: true,
            get: () => {
              const raw = responseTextGetter.call(this);
              return actionBarRequest ? patchActionBarText(raw) : patchSkillPackText(raw);
            }
          });
        } catch {
          // 当前 Chromium 不允许覆盖实例 getter 时，fetch 路径仍然有效。
        }
      }
      if (responseGetter) {
        try {
          Object.defineProperty(this, "response", {
            configurable: true,
            get: () => {
              const raw = responseGetter.call(this);
              if (typeof raw === "string") return actionBarRequest ? patchActionBarText(raw) : patchSkillPackText(raw);
              if (raw && typeof raw === "object") {
                const patched = actionBarRequest ? patchActionBarConfig(raw) : patchDurationOptions(raw);
                publishReady(patched, patched ? "" : "能力响应中未找到视频时长配置");
              }
              return raw;
            }
          });
        } catch {
          // See responseText fallback above.
        }
      }
    }
    return xhrOpen.apply(this, arguments);
  };
})();
