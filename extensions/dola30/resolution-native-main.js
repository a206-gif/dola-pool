(function () {
  "use strict";

  const INSTALL = "__doubaoDolaResolutionNativeV1620";
  const PREF_KEY = "intl_doubao_target_resolution_v1";
  const PREF_ATTRIBUTE = "data-watermark-free-resolution-preference";
  const PREF_EVENT = "watermark-free-resolution-preference-change";
  const STATUS_ATTRIBUTE = "data-watermark-free-resolution-bridge";
  const STATUS_EVENT = "watermark-free-resolution-bridge-status";
  const MENU_MARK = "data-watermark-free-resolution-ui";
  const VALUES = ["720p", "1080p"];
  const IS_DOLA = /(^|\.)dola\.com$/i.test(location.hostname);
  if (!/(^|\.)(doubao|dola)\.com$/i.test(location.hostname) || globalThis[INSTALL]) return;
  globalThis[INSTALL] = true;

  const state = {
    version: "1.6.21", runtimeFound: false, submitReady: false,
    menuVisible: false, menuInjected: false, nativeControl: false,
    selected: "auto", lastDraft: null, reason: "等待网页提交服务",
    inputReady: false, inputResolution: "", pipelineReady: false, legacyReady: false,
    draftCount: 0, pipelineCount: 0, lastPipeline: null, serviceRebinds: 0
  };
  const installedServices = new WeakMap();
  const installedAccessors = new WeakMap();
  const installedLegacyServices = new WeakMap();
  const installedNodes = new WeakMap();
  let nativeInput = null;
  let scannedModuleCount = -1;
  const inputModuleIds = new Set(["Kyj"]);
  const pipelineModuleIds = new Set(["sAh"]);
  let searching = false;
  let renderQueued = false;
  let lastPageUrl = location.href;
  let pendingPrompt = null;
  const EDITOR_SELECTOR = 'textarea, input[type="text"], [contenteditable="true"], [role="textbox"]';

  function selected() {
    try {
      const value = localStorage.getItem(PREF_KEY);
      return VALUES.includes(value) ? value : "auto";
    } catch { return "auto"; }
  }

  function publish() {
    state.selected = selected();
    document.documentElement?.setAttribute(STATUS_ATTRIBUTE, JSON.stringify(state));
    document.dispatchEvent(new Event(STATUS_EVENT));
  }

  function save(value) {
    if (!VALUES.includes(value) && value !== "auto") return false;
    try { localStorage.setItem(PREF_KEY, value); } catch { return false; }
    document.documentElement?.setAttribute(PREF_ATTRIBUTE, value);
    document.dispatchEvent(new Event(PREF_EVENT));
    syncDolaInput();
    renderMenu();
    publish();
    return true;
  }

  // Current Dola uses Kyj.qM to keep both the raw action value and its
  // submission record. Setting only localStorage does not update either.
  function selectedDurationPref() {
    try {
      const value = Number(localStorage.getItem("intl_doubao_target_duration_v1"));
      if (Number.isInteger(value) && value >= 4 && value <= 30) return value;
    } catch { /* Keep the page duration. */ }
    return 0;
  }

  function syncDolaDuration() {
    if (!IS_DOLA || !nativeInput) return;
    const seconds = selectedDurationPref();
    if (!seconds) return;
    try {
      const previous = nativeInput.N5?.("video-duration");
      const value = String(seconds);
      if (String(nativeInput.gl("video-duration") || "") !== value || String(previous?.value || "") !== value ||
          previous?.messageText) {
        nativeInput.qM("video-duration", { ...previous, value, submitKey: "duration", messageText: "" });
      }
    } catch { /* The duration control is not hydrated yet. */ }
  }

  function syncDolaInput() {
    if (!IS_DOLA || !nativeInput) return;
    const value = selected();
    try {
      if (value !== "auto") {
        const previous = nativeInput.N5?.("video-resolution");
        if (nativeInput.gl("video-resolution") !== value || previous?.value !== value ||
            previous?.messageText !== "" || previous?.submitKey !== "resolution") {
          nativeInput.qM("video-resolution", { ...previous, value, submitKey: "resolution", messageText: "" });
        }
      }
      state.inputResolution = String(nativeInput.gl("video-resolution") || "").toLowerCase();
      state.inputReady = true;
    } catch {
      state.inputReady = false;
      state.inputResolution = "";
    }
  }

  function stripAddedDuration(text, snapshot, duration) {
    if (!snapshot || snapshot.page !== location.href || Date.now() - snapshot.at > 60_000 ||
        typeof text !== "string" || !text.startsWith(snapshot.text) || !Number.isFinite(duration) || duration <= 0) return text;
    const number = String(duration).replace(/\./g, "\\.");
    const suffix = new RegExp(`^(?:[ \\t]*[,，][ \\t]*|[ \\t]*\\n[ \\t]*)(?:(?:时长|duration)[ \\t]*[:：]?[ \\t]*)?${number}[ \\t]*(?:s|sec(?:onds?)?|秒(?:钟)?)[ \\t]*$`, "i");
    return suffix.test(text.slice(snapshot.text.length)) ? snapshot.text : text;
  }

  function prepareDolaContext(context, resolution) {
    const content = context?.content;
    if (!content || typeof content !== "object" ||
        (content.abilityType != null && Number(content.abilityType) !== 17)) return context;
    const params = { ...content.actionBarControlParams };
    const values = { ...content.actionBarControlValues };
    if (VALUES.includes(resolution)) {
      params["video-resolution"] = resolution;
      values["video-resolution"] = { ...values["video-resolution"], value: resolution,
        submitKey: "resolution", messageText: "" };
      // A native record may have a different valueKey but the same submitKey.
      for (const [key, record] of Object.entries(values)) {
        if (record?.submitKey === "resolution") values[key] = { ...record, value: resolution, messageText: "" };
      }
    }
    const seconds = selectedDurationPref();
    let durationApplied = false;
    for (const [key, record] of Object.entries(values)) {
      if (key === "video-duration" || record?.submitKey === "duration") {
        values[key] = seconds
          ? { ...record, value: String(seconds), submitKey: record?.submitKey || "duration", messageText: "" }
          : { ...record, messageText: "" };
        if (seconds) durationApplied = true;
      }
    }
    if (seconds) {
      params["video-duration"] = String(seconds);
      if (!durationApplied) {
        values["video-duration"] = { value: String(seconds), submitKey: "duration", messageText: "" };
      }
    }
    const next = { ...context, content: { ...content, actionBarControlParams: params, actionBarControlValues: values } };
    // 交给模型选择适配器补 video-model 原生值（返回原对象表示无需改动）
    return globalThis.__DOUBAO_DOLA_MODEL_SELECTION__?.prepareContext(next) ?? next;
  }

  function installDolaPipeline(pipeline) {
    if (!IS_DOLA || !Array.isArray(pipeline?.submitNodes)) return false;
    const node = pipeline.submitNodes.find((entry) => entry?.id === "video-creation:input-skill-node");
    if (!node || typeof node.execute !== "function") return false;
    if (installedNodes.get(node) === node.execute) return true;
    const original = node.execute;
    function resolutionInNativePipeline(context, ...args) {
      if (context?.content?.abilityType != null && Number(context.content.abilityType) !== 17) {
        return original.call(this, context, ...args);
      }
      const value = selected();
      const prepared = prepareDolaContext(context, value);
      const result = original.call(this, prepared, ...args);
      function finish(output) {
        const params = output?.content?.abilityParams;
        const recognized = Boolean(params && typeof params === "object");
        state.pipelineCount += 1;
        state.lastPipeline = { selected: value, recognized,
          resolution: recognized ? String(params.resolution || "").toLowerCase() : "",
          model: typeof params?.model === "string" ? params.model.slice(0, 80) : "",
          duration: Number.isFinite(Number(params?.duration)) ? Number(params.duration) : null };
        let next = output;
        if (recognized) {
          const text = stripAddedDuration(output.content.text, pendingPrompt, Number(params.duration));
          if (text !== output.content.text) next = { ...output, content: { ...output.content, text } };
        }
        // 原生节点可能用旧/默认选项重建参数，签名前把 model 纠正回用户所选
        try { next = globalThis.__DOUBAO_DOLA_MODEL_SELECTION__?.preparePipeline(next) ?? next; }
        catch { /* 无法识别的原生模型结果保持原样 */ }
        publish();
        return next;
      }
      return result && typeof result.then === "function" ? result.then(finish) : finish(result);
    }
    try { node.execute = resolutionInNativePipeline; } catch { return false; }
    if (node.execute !== resolutionInNativePipeline) return false;
    installedNodes.set(node, resolutionInNativePipeline);
    return true;
  }

  function discoverDolaPipeline(require) {
    if (!IS_DOLA || !require?.m) return;
    const count = Object.keys(require.m).length;
    if (count !== scannedModuleCount) {
      scannedModuleCount = count;
      for (const [id, factory] of Object.entries(require.m)) {
        const source = String(factory);
        if (source.includes("__input_action_control_values__") && source.includes("chatInputStore")) inputModuleIds.add(id);
        if (source.includes("video-creation:input-skill-node")) pipelineModuleIds.add(id);
      }
    }
    for (const id of inputModuleIds) {
      if (!Object.hasOwn(require.m, id)) continue;
      try {
        const api = require(id);
        if (typeof api?.qM === "function" && typeof api?.gl === "function") {
          nativeInput = api;
          // 适配器统一用 setRecord/getValue/getRecord；这里把 Dola 的 qM/gl/N5 映射过去
          globalThis.__DOUBAO_DOLA_MODEL_SELECTION__?.bindInput({
            setRecord: api.qM, getValue: api.gl, getRecord: api.N5
          });
          break;
        }
      } catch { /* The native input store has not hydrated yet. */ }
    }
    syncDolaInput();
    syncDolaDuration();
    let ready = false;
    for (const id of pipelineModuleIds) {
      if (!Object.hasOwn(require.m, id)) continue;
      try {
        const module = require(id);
        for (const pipeline of Object.values(module || {})) ready = installDolaPipeline(pipeline) || ready;
      } catch { /* Wait for the remaining lazy video modules. */ }
    }
    state.pipelineReady = ready;
    if (ready) { state.submitReady = true; state.reason = ""; }
  }

  function promptEditors(root = document) {
    return Array.from(root?.querySelectorAll?.(EDITOR_SELECTOR) || []).filter((editor) => {
      if (!editor.isConnected || editor.disabled || editor.readOnly) return false;
      const rect = editor.getBoundingClientRect();
      const style = getComputedStyle(editor);
      return rect.width > 1 && rect.height > 1 && style.display !== "none" && style.visibility !== "hidden";
    });
  }

  function editorForSubmit(node) {
    let parent = node?.tagName === "FORM" ? node : node?.parentElement || node;
    for (let depth = 0; parent && depth < 9; depth += 1, parent = parent.parentElement) {
      const editors = promptEditors(parent);
      if (editors.length) return editors[editors.length - 1];
    }
    const editors = promptEditors();
    return editors[editors.length - 1] || null;
  }

  // Read the authored prompt before the page clears its composer. These
  // listeners never change the editor, cancel an event or submit a second time.
  function rememberPrompt(event) {
    let editor = null;
    if (event.type === "keydown") {
      if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
      editor = event.target?.closest?.(EDITOR_SELECTOR);
    } else if (event.type === "submit") {
      editor = editorForSubmit(event.target);
    } else {
      const button = event.target?.closest?.('button, [role="button"]');
      if (!button || button.disabled || button.getAttribute("aria-haspopup") ||
          button.closest('[data-input-engine-actionbar-control-key]') || button.closest(`[${MENU_MARK}]`)) return;
      editor = editorForSubmit(button);
      if (!editor) return;
      const label = [button.textContent, button.getAttribute("aria-label"), button.getAttribute("title")].join(" ");
      if (/模型|比例|时长|seedance/i.test(label)) return;
      if (button.type !== "submit" && button.getAttribute("type") !== "submit" &&
          !/发送|提交|生成|\bsend\b|\bsubmit\b/i.test(label)) {
        const buttonRect = button.getBoundingClientRect();
        const editorRect = editor.getBoundingClientRect();
        if (!(buttonRect.left >= editorRect.right - 90 && buttonRect.top >= editorRect.top - 20 &&
              buttonRect.bottom <= editorRect.bottom + 80)) return;
      }
    }
    const text = /^(TEXTAREA|INPUT)$/.test(editor?.tagName || "")
      ? String(editor.value || "") : String(editor?.innerText ?? editor?.textContent ?? "");
    pendingPrompt = text.trim() ? { text, page: location.href, at: Date.now() } : null;
  }

  for (const type of ["pointerdown", "click", "keydown", "submit"]) {
    document.addEventListener(type, rememberPrompt, true);
  }
  document.addEventListener("input", (event) => {
    if (event.isTrusted && event.target?.closest?.(EDITOR_SELECTOR)) pendingPrompt = null;
  }, true);

  function durationOfMessage(message) {
    if (/system|assistant|bot|tool/i.test(String(message?.role || message?.sender_role || ""))) return null;
    try {
      const raw = message?.ext?.chat_ability;
      const ability = typeof raw === "string" ? JSON.parse(raw) : raw;
      if (Number(ability?.ability_type) !== 17) return null;
      const params = typeof ability.ability_param === "string" ? JSON.parse(ability.ability_param) : ability.ability_param;
      const duration = Number(params?.duration);
      return Number.isFinite(duration) && duration > 0 && duration <= 600 ? duration : null;
    } catch { return null; }
  }

  function removeGeneratedDuration(request, snapshot) {
    if (!snapshot || snapshot.page !== location.href || Date.now() - snapshot.at > 60_000 ||
        !Array.isArray(request?.messages)) return { request, changed: false };
    let changed = false;
    let target = -1;
    for (let index = request.messages.length - 1; index >= 0; index -= 1) {
      if (durationOfMessage(request.messages[index]) != null) { target = index; break; }
    }
    const messages = request.messages.map((message, index) => {
      if (index !== target) return message;
      const duration = durationOfMessage(message);
      if (duration == null) return message;
      const number = String(duration).replace(/\./g, "\\.");
      const suffix = new RegExp(`^(?:[ \\t]*[,，][ \\t]*|[ \\t]*\\n[ \\t]*)(?:(?:时长|duration)[ \\t]*[:：]?[ \\t]*)?${number}[ \\t]*(?:s|sec(?:onds?)?|秒(?:钟)?)[ \\t]*$`, "i");
      function clean(text) {
        if (typeof text !== "string" || !text.startsWith(snapshot.text)) return text;
        // A user-authored "30s" is part of the snapshot and remains intact.
        // Remove only an additional suffix equal to the structured duration.
        return suffix.test(text.slice(snapshot.text.length)) ? snapshot.text : text;
      }
      function replacePath(value, path) {
        if (!path.length) return clean(value);
        if (!value || typeof value !== "object" || Array.isArray(value)) return value;
        const [key, ...rest] = path;
        if (!Object.hasOwn(value, key)) return value;
        const next = replacePath(value[key], rest);
        return next === value[key] ? value : { ...value, [key]: next };
      }
      let output = message;
      for (const path of [["content"], ["text"], ["prompt"], ["content", "text"], ["content", "text_block", "text"]]) {
        output = replacePath(output, path);
      }
      if (Array.isArray(message.content_blocks_v2)) {
        let blocksChanged = false;
        const blocks = message.content_blocks_v2.map((block) => {
          if (![1, 10000].includes(Number(block?.block_type))) return block;
          let next = block;
          for (const path of [["text"], ["content", "text"], ["content", "text_block", "text"]]) {
            next = replacePath(next, path);
          }
          blocksChanged ||= next !== block;
          return next;
        });
        if (blocksChanged) output = { ...output, content_blocks_v2: blocks };
      }
      changed ||= output !== message;
      return output;
    });
    return { request: changed ? { ...request, messages } : request, changed };
  }

  // Patch an application-level message object BEFORE the native IM service
  // serializes/signs it. Never patch the fetch/XHR body or a signed URL.
  function patchDraft(request, resolution) {
    if (!VALUES.includes(resolution)) return { request, found: false, changed: false };
    const seen = new Set();
    let found = false;
    let changed = false;
    function walk(value, depth = 0) {
      if (!value || typeof value !== "object" || depth > 24 || seen.has(value)) return value;
      const prototype = Object.getPrototypeOf(value);
      if (!Array.isArray(value) && prototype !== null && prototype?.constructor?.name !== "Object") return value;
      seen.add(value);
      let output = value;
      if (IS_DOLA && Number(value.skill_type) === 17) {
        const vars = value.variables;
        if (vars == null || (typeof vars === "object" && !Array.isArray(vars))) {
          found = true;
          if (String(vars?.resolution || "").toLowerCase() !== resolution) {
            output = { ...value, variables: { ...vars, resolution } };
            changed = true;
          }
        }
      }
      if (Number(value.ability_type) === 17 && value.ability_param != null) {
        const encoded = typeof value.ability_param === "string";
        try {
          const params = encoded ? JSON.parse(value.ability_param) : value.ability_param;
          if (params && typeof params === "object" && !Array.isArray(params)) {
            found = true;
            if (String(params.resolution || "").toLowerCase() !== resolution) {
              const updated = { ...params, resolution };
              output = { ...value, ability_param: encoded ? JSON.stringify(updated) : updated };
              changed = true;
            }
          }
        } catch { /* Leave an unrecognized draft intact. */ }
      }
      for (const [key, child] of Object.entries(output)) {
        if (["prompt", "text", "content", "content_blocks_v2"].includes(key)) continue;
        let updated = child;
        if ((key === "chat_ability" || (IS_DOLA && key === "input_skill")) && typeof child === "string") {
          try {
            const parsed = JSON.parse(child);
            const next = walk(parsed, depth + 1);
            if (next !== parsed) updated = JSON.stringify(next);
          } catch { /* Do not reinterpret other strings as configuration. */ }
        } else if (key !== "ability_param") updated = walk(child, depth + 1);
        if (updated !== child) {
          if (output === value) output = Array.isArray(value) ? [...value] : { ...value };
          output[key] = updated;
        }
      }
      return output;
    }
    return { request: walk(request), found, changed };
  }

  function installService(service) {
    if (!service || typeof service.sendMessage !== "function") return false;
    if (installedServices.get(service) === service.sendMessage ||
        (installedServices.has(service) && service.sendMessage.__DOUBAO_DOLA_RESOLUTION_BRIDGE__)) {
      state.submitReady = true;
      state.reason = "";
      return true;
    }
    const original = service.sendMessage;
    function resolutionBeforeSigning(request, ...args) {
      const snapshot = pendingPrompt;
      pendingPrompt = null;
      let prompt = { request, changed: false };
      try { prompt = removeGeneratedDuration(request, snapshot); } catch { /* An unknown native draft stays intact. */ }
      if (!IS_DOLA && typeof globalThis.__DOUBAO_VIDEO_DIRECT_GENERATION__?.prepare === "function") {
        try { prompt.request = globalThis.__DOUBAO_VIDEO_DIRECT_GENERATION__.prepare(prompt.request); }
        catch { /* Preserve native submission if its text shape is unknown. */ }
      }
      const value = selected();
      let result = { request: prompt.request, found: false, changed: false };
      if (value !== "auto") result = patchDraft(prompt.request, value);
      // 签名前把最后一条用户消息里的 model 纠正为用户所选（不改历史、不重签已签名请求）
      try { result.request = globalThis.__DOUBAO_DOLA_MODEL_SELECTION__?.prepare(result.request) ?? result.request; }
      catch { /* 无法识别的模型草稿保持原样 */ }
      state.draftCount += 1;
      state.lastDraft = { resolution: value, recognized: result.found, changed: result.changed, promptSuffixRemoved: prompt.changed };
      if (value !== "auto" && !result.found) state.reason = "提交对象未包含可识别的视频能力参数";
      else state.reason = "";
      publish();
      // Exactly one native call; its return value, this and extra arguments survive.
      return original.call(this, result.request, ...args);
    }
    Object.defineProperty(resolutionBeforeSigning, "__DOUBAO_DOLA_RESOLUTION_BRIDGE__", { value: true });
    try { service.sendMessage = resolutionBeforeSigning; } catch { return false; }
    if (service.sendMessage !== resolutionBeforeSigning) return false;
    if (installedServices.has(service)) state.serviceRebinds += 1;
    installedServices.set(service, resolutionBeforeSigning);
    state.submitReady = true;
    state.reason = "";
    return true;
  }

  function installIMAccessor(im) {
    if (!IS_DOLA || !im || typeof im.getMessageService !== "function") return;
    if (installedAccessors.get(im) === im.getMessageService) return;
    const original = im.getMessageService;
    function currentMessageService(...args) {
      const result = original.apply(this, args);
      const install = (service) => {
        try { installService(service); publish(); } catch { /* Preserve the native accessor result. */ }
      };
      if (result && typeof result.then === "function") {
        // Register before the caller awaits this same promise; keep its identity.
        result.then(install, () => {});
      } else install(result);
      return result;
    }
    try { im.getMessageService = currentMessageService; } catch { return; }
    if (im.getMessageService === currentMessageService) installedAccessors.set(im, currentMessageService);
  }

  function installLegacyService(service) {
    if (!IS_DOLA || !service || typeof service.sendMessage !== "function") return false;
    if (installedLegacyServices.get(service) === service.sendMessage) return true;
    const original = service.sendMessage;
    function legacyResolutionBeforeSigning(request, ...args) {
      let draft = request;
      // 旧版 Dola 把参数放在 input_skill.variables 里，同样在签名前纠正 model
      try { draft = globalThis.__DOUBAO_DOLA_MODEL_SELECTION__?.prepareLegacy(draft) ?? draft; }
      catch { /* 无法识别的旧版草稿保持原样 */ }
      const value = selected();
      const result = value === "auto" ? { request: draft, found: false, changed: false } : patchDraft(draft, value);
      state.lastLegacy = { selected: value, recognized: result.found, changed: result.changed };
      publish();
      return original.call(this, result.request, ...args);
    }
    try { service.sendMessage = legacyResolutionBeforeSigning; } catch { return false; }
    if (service.sendMessage !== legacyResolutionBeforeSigning) return false;
    installedLegacyServices.set(service, legacyResolutionBeforeSigning);
    return true;
  }

  function runtime() {
    if (typeof window.__DOLA_TOOLKIT_WEBPACK_REQUIRE__ === "function") return window.__DOLA_TOOLKIT_WEBPACK_REQUIRE__;
    if (typeof window.__DOUBAO_DOLA_RESOLUTION_REQUIRE__ === "function") return window.__DOUBAO_DOLA_RESOLUTION_REQUIRE__;
    const candidates = [window.__LOADABLE_LOADED_CHUNKS__];
    for (const key of Object.keys(window)) {
      if (/^webpackChunk/.test(key) && Array.isArray(window[key])) candidates.push(window[key]);
    }
    for (const chunks of candidates) {
      if (!Array.isArray(chunks)) continue;
      try {
        let captured = null;
        chunks.push([[`resolution-${Date.now()}`], {}, (require) => { captured = require; }]);
        if (typeof captured === "function") {
          window.__DOUBAO_DOLA_RESOLUTION_REQUIRE__ = captured;
          return captured;
        }
      } catch { /* Another chunk format; try the next known Webpack array. */ }
    }
    return null;
  }

  async function discoverService() {
    if (searching) return;
    searching = true;
    try {
      const require = runtime();
      if (!require?.m) return;
      state.runtimeFound = true;
      discoverDolaPipeline(require);
      const ids = new Set(["nR1", "431565", "645906"]);
      if (!IS_DOLA || ![...ids].some((id) => Object.hasOwn(require.m, id))) {
        for (const [id, factory] of Object.entries(require.m)) {
          if (/serviceContainer|StoreService/.test(String(factory))) ids.add(id);
        }
      }
      for (const id of ids) {
        if (!Object.hasOwn(require.m, id)) continue;
        try {
          const module = require(id);
          const accessor = module?.K || module?.D;
          if (typeof accessor !== "function") continue;
          const im = await accessor("chatIMService");
          installIMAccessor(im);
          const service = await im?.getMessageService?.();
          if (IS_DOLA) {
            try { state.legacyReady = installLegacyService(await accessor("chatMessageService")); } catch { /* A legacy service may be absent. */ }
          }
          if (installService(service)) break;
        } catch { /* A service may not be available until video-mode hydration. */ }
      }
    } finally {
      searching = false;
      if (state.pipelineReady || state.legacyReady) { state.submitReady = true; state.reason = ""; }
      publish();
      renderMenu();
    }
  }

  function visible(element) {
    if (!element?.isConnected) return false;
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width >= 160 && rect.width <= 680 && rect.height >= 80 && rect.height <= 720 &&
      style.display !== "none" && style.visibility !== "hidden";
  }

  function menu() {
    const candidates = new Set(document.querySelectorAll(
      '[role="dialog"], [role="menu"], [role="listbox"], [data-popper-placement], ' +
      '[class*="popover"], [class*="Popover"], [class*="popper"], [class*="dropdown"], [class*="Dropdown"]'
    ));
    for (const slider of document.querySelectorAll('[role="slider"], input[type="range"]')) {
      let parent = slider.parentElement;
      for (let depth = 0; parent && depth < 8; depth += 1, parent = parent.parentElement) candidates.add(parent);
    }
    return [...candidates].filter(visible).filter((node) => {
      const text = String(node.textContent || "");
      return /时长|duration/i.test(text) && /3\s*:\s*4/.test(text) && /16\s*:\s*9/.test(text);
    }).sort((a, b) => {
      const ar = a.getBoundingClientRect(); const br = b.getBoundingClientRect();
      return ar.width * ar.height - br.width * br.height;
    })[0] || null;
  }

  function renderMenu() {
    if (!document.documentElement) return;
    const root = menu();
    const before = `${state.menuVisible}:${state.menuInjected}:${state.nativeControl}`;
    state.menuVisible = Boolean(root);
    state.menuInjected = false;
    state.nativeControl = false;
    if (root) {
      const nativeText = [...root.children].filter((node) => !node.hasAttribute(MENU_MARK)).map((node) => node.textContent).join(" ");
      state.nativeControl = /720\s*p/i.test(nativeText) && /1080\s*p/i.test(nativeText);
      let row = root.querySelector(`[${MENU_MARK}]`);
      if (state.nativeControl) row?.remove();
      else {
        if (!row) {
          row = document.createElement("div");
          row.setAttribute(MENU_MARK, "1");
          row.style.cssText = "box-sizing:border-box;width:100%;grid-column:1/-1;flex-shrink:0;padding:12px 0 4px;margin-top:12px;border-top:1px solid #eee;font-family:inherit;";
          const label = document.createElement("div");
          label.textContent = "分辨率 · 插件";
          label.style.cssText = "color:#929292;font-size:12px;margin-bottom:8px;";
          row.appendChild(label);
          const buttons = document.createElement("div");
          buttons.style.cssText = "display:flex;gap:8px;";
          for (const value of VALUES) {
            const button = document.createElement("button");
            button.type = "button";
            button.textContent = value.toUpperCase();
            button.setAttribute("data-resolution", value);
            button.addEventListener("click", (event) => {
              event.stopPropagation();
              if (state.submitReady) save(value);
            });
            buttons.appendChild(button);
          }
          row.appendChild(buttons);
          const note = document.createElement("div");
          note.setAttribute("data-resolution-note", "1");
          note.style.cssText = "color:#929292;font-size:11px;line-height:1.5;margin-top:6px;";
          row.appendChild(note);
          root.appendChild(row);
        }
        state.menuInjected = true;
        for (const button of row.querySelectorAll("button")) {
          const active = selected() === button.getAttribute("data-resolution");
          button.disabled = !state.submitReady;
          button.setAttribute("aria-pressed", String(active));
          button.style.cssText = `flex:1;height:36px;border-radius:9px;font:inherit;font-size:13px;cursor:${state.submitReady ? "pointer" : "wait"};border:1px solid ${active ? "#347eff" : "#eee"};background:${active ? "#eff5ff" : "#f7f7f7"};color:${active ? "#347eff" : "#333"};opacity:${state.submitReady ? "1" : ".6"};`;
        }
        const note = row.querySelector("[data-resolution-note]");
        const text = state.submitReady ? "选择将用于下一次视频生成" : "等待网页提交服务，暂未应用";
        if (note.textContent !== text) note.textContent = text;
      }
    }
    if (before !== `${state.menuVisible}:${state.menuInjected}:${state.nativeControl}`) publish();
  }

  function queueRender() {
    if (renderQueued) return;
    renderQueued = true;
    setTimeout(() => { renderQueued = false; renderMenu(); }, 0);
  }

  document.addEventListener("click", (event) => {
    const target = event.target?.closest?.('button, [role="button"], [role="radio"], [role="option"]');
    const label = String(target?.textContent || "").trim().toLowerCase();
    const text = label.match(/^(?:720|1080)\s*p\b/i)?.[0].replace(/\s/g, "") || label;
    const root = menu();
    if (VALUES.includes(text) && root?.contains(target) && !target.closest(`[${MENU_MARK}]`) && !target.disabled) save(text);
    queueRender();
  }, true);
  document.addEventListener(PREF_EVENT, () => {
    syncDolaInput(); renderMenu(); publish();
    if (IS_DOLA) void discoverService();
  });
  document.addEventListener("watermark-free-duration-30-change", () => {
    syncDolaDuration();
  });
  document.addEventListener("watermark-free-resolution-status", queueRender);

  function start() {
    renderMenu();
    void discoverService();
    if (typeof MutationObserver === "function") {
      new MutationObserver((records) => {
        if (records.some((record) => !record.target?.closest?.(`[${MENU_MARK}]`))) queueRender();
      }).observe(document.documentElement, { childList: true, subtree: true });
    }
    setInterval(() => {
      if (location.href !== lastPageUrl) {
        pendingPrompt = null;
        lastPageUrl = location.href;
        state.submitReady = false;
        state.reason = "页面已切换，等待当前会话提交服务";
      }
      renderMenu();
      if (IS_DOLA || !state.submitReady) void discoverService();
    }, 1000);
    publish();
  }

  Object.defineProperty(window, "__DOUBAO_DOLA_RESOLUTION_BRIDGE__", {
    configurable: true, value: Object.freeze({
      get status() { return JSON.parse(JSON.stringify({ ...state, selected: selected() })); },
      retry: discoverService
    })
  });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true });
  else start();
})();
