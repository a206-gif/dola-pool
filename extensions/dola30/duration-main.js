(function () {
  "use strict";

  const INSTALL_KEY = "__watermarkFreeDuration30Installed";
  const DURATION_KEY = "intl_doubao_enable_30s_v1";
  const TARGET_DURATION_KEY = "intl_doubao_target_duration_v1";
  const STATE_ATTRIBUTE = "data-watermark-free-duration-30";
  const STATE_EVENT = "watermark-free-duration-30-change";
  const MENU_MARK = "data-watermark-free-duration-option";
  const NATIVE_BOUND_MARK = "data-watermark-free-native-duration-bound";
  const MODEL_SELECTOR = "[data-input-engine-actionbar-control-key=\"video-model\"], [data-input-engine-actionbar-control-key=\"model\"]";
  // Dola 底部输入栏的模型 / 时长触发器（仅视频生成模式下出现），与 C# Automation 读取选择器保持一致。
  const VIDEO_MODEL_TRIGGER = "[data-input-engine-actionbar-control-key=\"video-model\"]";
  const VIDEO_DURATION_TRIGGER = "[data-input-engine-actionbar-control-key=\"video-duration\"]";
  // 宿主软件（Ctrl+M）显式关闭后置此会话锁：在用户于页面重新点选 30s 前，UI 对账不得自动弹回开启。
  const UI_SUPPRESS_KEY = "__dola30UiSuppress";
  const VIDEO_COMPLETION_PATH = "/chat/completion";
  const VIDEO_ABILITY_TYPE = 17;
  const SEEDANCE_25_MODEL = "seedance_v2.5";
  const TIMELINE_GUARD_MARK = "【单片生成控制】";
  const DIRECT_GENERATION_GUARD_MARK = "【直接生成控制】";
  const PROMPT_GUARD_INJECT_KEY = "intl_doubao_prompt_guard_inject_v1";
  const TIMELINE_REWRITE_KEY = "intl_doubao_timeline_rewrite_v1";
  const TIMELINE_DURATION_SECONDS = 30;
  const PROMPT_FIELD_NAMES = new Set([
    "content", "text", "prompt", "query", "input", "input_text", "message"
  ]);

  if (globalThis[INSTALL_KEY]) {
    return;
  }
  globalThis[INSTALL_KEY] = true;

  const host = location.hostname.replace(/^www\./i, "").toLowerCase();
  if (!(host === "dola.com" || host.endsWith(".dola.com") || host === "doubao.com" || host.endsWith(".doubao.com"))) {
    return;
  }

  let lastKnownModelTarget = 0;
  let native30Observed = false;

  function textOf(element) {
    return String(
      element?.textContent ||
      element?.getAttribute?.("aria-label") ||
      element?.getAttribute?.("title") ||
      ""
    ).replace(/\s+/g, " ").trim();
  }

  function visible(element) {
    try {
      const rect = element?.getBoundingClientRect();
      const style = element && getComputedStyle(element);
      return Boolean(
        element &&
        element.isConnected &&
        rect.width > 1 &&
        rect.height > 1 &&
        style.display !== "none" &&
        style.visibility !== "hidden" &&
        Number(style.opacity || 1) > 0.01
      );
    } catch {
      return false;
    }
  }

  function durationText(value) {
    return String(value || "").replace(/\s+/g, "").replace(/[✓✔√]/g, "").trim();
  }

  function selectedModelText() {
    return durationText(textOf(document.querySelector(MODEL_SELECTOR)));
  }

  function modelTargetDuration() {
    // 开启 30 秒模式后，以扩展状态为准，不再依赖 Dola 易变的按钮文案。
    // 这样页面重绘或平台把模型文案暂时恢复成 Fast 时，也不会丢失 30 秒入口。
    if (duration30Enabled()) {
      lastKnownModelTarget = 30;
      return 30;
    }
    const text = selectedModelText();
    if (/2\.5|seedance[^\d]*2[^\d]*5/i.test(text)) {
      lastKnownModelTarget = 30;
    } else if (/2\.0|seedance[^\d]*2[^\d]*0|seedance[^\d]*fast/i.test(text)) {
      lastKnownModelTarget = 0;
    }
    return lastKnownModelTarget;
  }

  function isDolaHost() {
    return host === "dola.com" || host.endsWith(".dola.com");
  }

  function nativeSkillPackReady() {
    try {
      return document.documentElement.getAttribute("data-dola-native-30-ready") === "1";
    } catch {
      return false;
    }
  }

  function duration30Enabled() {
    try {
      return localStorage.getItem(DURATION_KEY) === "1";
    } catch {
      return false;
    }
  }

  function uiSuppressActive() {
    try {
      return sessionStorage.getItem(UI_SUPPRESS_KEY) === "1";
    } catch {
      return false;
    }
  }

  function setUiSuppress(on) {
    try {
      if (on) {
        sessionStorage.setItem(UI_SUPPRESS_KEY, "1");
      } else {
        sessionStorage.removeItem(UI_SUPPRESS_KEY);
      }
    } catch {
      // 隐私模式等场景忽略。
    }
  }

  // 读取 Dola 页面当前是否处于「视频生成 + Seedance 2.5 + 30s」状态。
  // 两个触发器仅在视频生成模式下渲染；模型/时长文案读触发器自身。
  function readDolaVideo30sUiActive() {
    if (!isDolaHost() || typeof document !== "object" || !document.querySelector) {
      return false;
    }
    try {
      const modelTrigger = document.querySelector(VIDEO_MODEL_TRIGGER);
      const durationTrigger = document.querySelector(VIDEO_DURATION_TRIGGER);
      if (!modelTrigger || !durationTrigger || !visible(modelTrigger) || !visible(durationTrigger)) {
        return false;
      }
      const modelText = durationText(textOf(modelTrigger));
      const durationTextValue = durationText(textOf(durationTrigger));
      const modelIs25 = /2\.5|seedance[^\d]*2[^\d]*5/i.test(modelText);
      const durationIs30 = /^(30)(s|秒)$/i.test(durationTextValue);
      return modelIs25 && durationIs30;
    } catch {
      return false;
    }
  }

  // Dola：开关状态以页面选择为准（视频生成 + 2.5 + 30s ↔ 开；其余一律关）。
  // 豆包页面没有 30s 时长入口，维持 localStorage 软件开关模式，不参与对账。
  // fullReconcile=false（状态事件 / 桥接调用）只向上开启，避免「先置开关再点 UI」
  // 的 C# 编排被同一次调用立即关回；向下关闭只由每秒定时器（fullReconcile=true）执行。
  function reconcileDolaUiState(fullReconcile = false) {
    if (!isDolaHost() || uiSuppressActive()) {
      return;
    }
    const uiActive = readDolaVideo30sUiActive();
    if (uiActive && !duration30Enabled()) {
      saveTargetDuration(30);
      try { console.log("[Dola30s] 页面已选择 视频生成 + 2.5 + 30s，助手自动开启"); } catch {}
    } else if (fullReconcile && !uiActive && duration30Enabled()) {
      try { localStorage.removeItem(TARGET_DURATION_KEY); } catch {}
      saveDuration30(false);
    }
  }

  function selectedDuration() {
    try {
      const value = Number(localStorage.getItem(TARGET_DURATION_KEY));
      if (Number.isInteger(value) && value >= 4 && value <= 30) return value;
    } catch {
      // Fall through to the legacy switch.
    }
    return duration30Enabled() ? 30 : 30;
  }

  function saveTargetDuration(seconds) {
    const value = Number(seconds) === 15 ? 15 : 30;
    try {
      localStorage.setItem(TARGET_DURATION_KEY, String(value));
      localStorage.setItem(DURATION_KEY, value === 30 ? "1" : "0");
    } catch {
      return;
    }
    publishDurationState();
  }

  function publishDurationState() {
    const root = document.documentElement;
    if (!root) {
      return;
    }
    root.setAttribute(STATE_ATTRIBUTE, duration30Enabled() ? "1" : "0");
    document.dispatchEvent(new Event(STATE_EVENT));
  }

  function saveDuration30(enabled) {
    try {
      localStorage.setItem(DURATION_KEY, enabled ? "1" : "0");
    } catch {
      return;
    }
    publishDurationState();
  }

  document.addEventListener(STATE_EVENT, () => {
    const value = document.documentElement?.getAttribute(STATE_ATTRIBUTE);
    if (value !== "0" && value !== "1") {
      return;
    }
    try {
      localStorage.setItem(DURATION_KEY, value);
    } catch {
      return;
    }
    enhanceDurationOptions();
  });

  function getRequestUrl(input) {
    return typeof input === "string" ? input : (input && (input.url || input.href)) || String(input || "");
  }

  function isSupportedGenerationHost(rawUrl) {
    try {
      const requestHost = new URL(rawUrl, location.href).hostname.replace(/^www\./i, "").toLowerCase();
      return requestHost === "dola.com" || requestHost.endsWith(".dola.com") ||
        requestHost === "doubao.com" || requestHost.endsWith(".doubao.com");
    } catch {
      return false;
    }
  }

  function isVideoCompletionUrl(rawUrl) {
    if (!isSupportedGenerationHost(rawUrl || location.href)) {
      return false;
    }
    try {
      // 兼容 Dola/豆包给接口增加 /api、版本前缀或末尾斜杠。
      return new URL(rawUrl, location.href).pathname.replace(/\/$/, "").endsWith(VIDEO_COMPLETION_PATH);
    } catch {
      return false;
    }
  }

  function forceVideoAbility(ability) {
    if (!ability || Number(ability.ability_type) !== VIDEO_ABILITY_TYPE) {
      return false;
    }

    const wasString = typeof ability.ability_param === "string";
    let abilityParam = ability.ability_param;
    if (wasString) {
      try {
        abilityParam = JSON.parse(abilityParam);
      } catch {
        return false;
      }
    }
    if (!abilityParam || typeof abilityParam !== "object") {
      return false;
    }

    let changed = false;
    if (abilityParam.model !== SEEDANCE_25_MODEL) {
      abilityParam.model = SEEDANCE_25_MODEL;
      changed = true;
    }
    if (Number(abilityParam.duration) !== 30) {
      abilityParam.duration = 30;
      changed = true;
    }
    if (changed) {
      ability.ability_param = wasString ? JSON.stringify(abilityParam) : abilityParam;
    }
    return changed;
  }

  function forceGenerationPayload(value, seen = new Set()) {
    if (!value || typeof value !== "object" || seen.has(value)) {
      return false;
    }
    seen.add(value);
    let changed = forceVideoAbility(value);
    for (const child of Object.values(value)) {
      if (child && typeof child === "object") {
        changed = forceGenerationPayload(child, seen) || changed;
      }
    }
    return changed;
  }

  function timelinePercent(seconds) {
    const value = Math.max(0, seconds / TIMELINE_DURATION_SECONDS * 100);
    return Number(value.toFixed(1)).toString();
  }

  function timestampSeconds(hours, minutes, seconds) {
    return Number(hours || 0) * 3600 + Number(minutes || 0) * 60 + Number(seconds || 0);
  }

  function parseChineseSeconds(raw) {
    const text = String(raw || "");
    if (text === "半") {
      return 0.5;
    }
    const digits = { 零: 0, "〇": 0, 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9 };
    const [integerText, decimalText = ""] = text.split("点");
    let integer = 0;
    if (integerText.includes("十")) {
      const [tensText, onesText] = integerText.split("十");
      integer = (tensText ? digits[tensText] : 1) * 10 + (onesText ? digits[onesText] : 0);
    } else if (integerText.length === 1 && Object.hasOwn(digits, integerText)) {
      integer = digits[integerText];
    } else {
      return NaN;
    }
    if (!decimalText) {
      return integer;
    }
    if (![...decimalText].every((character) => Object.hasOwn(digits, character))) {
      return NaN;
    }
    return Number(`${integer}.${[...decimalText].map((character) => digits[character]).join("")}`);
  }

  function secondsAsProgress(seconds, source, offset, explicitPosition = false) {
    if (!Number.isFinite(seconds) || seconds < 0) {
      return null;
    }
    const prefix = source.slice(Math.max(0, offset - 10), offset);
    const durationContext = /(?:持续|延续|停留|保持|等待|耗时|历时|长达|时长|最后|剩余|约占|短暂|不到|超过|多于|少于|淡出|淡入|静止|定格|后)$/u.test(prefix);
    const positionContext = /(?:在|于|到|至|截至|接近)$/u.test(prefix);
    if (!explicitPosition && !positionContext && (durationContext || seconds <= 2)) {
      return `约占全片 ${timelinePercent(seconds)}% 的时长`;
    }
    return `全片进度约 ${timelinePercent(seconds)}% 处`;
  }

  function promptGuardInjectEnabled() {
    try {
      return localStorage.getItem(PROMPT_GUARD_INJECT_KEY) === "1";
    } catch {
      return false;
    }
  }

  function timelineRewriteEnabled() {
    try {
      return localStorage.getItem(TIMELINE_REWRITE_KEY) === "1";
    } catch {
      return false;
    }
  }

  function rewriteTimelinePrompt(text) {
    const original = String(text || "");
    if (!original) {
      return original;
    }

    let changed = false;
    let rewritten = original;
    if (!timelineRewriteEnabled()) {
      if (promptGuardInjectEnabled()) {
        return applyPromptGuards(rewritten, false);
      }
      return original;
    }
    const aspectHolds = [];
    // 9:16、16:9 是画幅，不是时间码。先整段拿走，避免 9:16 被当成 9 分 16 秒。
    rewritten = rewritten.replace(
      /(^|[^\d])(9\s*[:：]\s*16|16\s*[:：]\s*9)(?=$|[^\d])/g,
      (match, prefix, ratio) => {
        const token = `\uE000AR${aspectHolds.length}\uE001`;
        aspectHolds.push(ratio);
        return `${prefix}${token}`;
      }
    );
    // 时间范围分隔符：同时接受半角/全角连字符（－ U+FF0D、― U+2015）与波浪线。
    const rangeSeparator = "[-–—－―~～]|至|到|→";
    // 将 00:00-00:05、00 00 至 00 05 一类时间范围改为进度百分比。
    rewritten = rewritten.replace(
      new RegExp(
        "(^|[^\\d])(?:(\\d{1,2})\\s*(?::|：|\\s+)\\s*)?([0-5]?\\d)\\s*(?::|：|\\s+)\\s*([0-5]\\d)\\s*(?:"
          + rangeSeparator
          + ")\\s*(?:(\\d{1,2})\\s*(?::|：|\\s+)\\s*)?([0-5]?\\d)\\s*(?::|：|\\s+)\\s*([0-5]\\d)(?=$|[^\\d])",
        "g"
      ),
      (match, prefix, startHours, startMinutes, startSeconds, endHours, endMinutes, endSeconds) => {
        const start = timestampSeconds(startHours, startMinutes, startSeconds);
        const end = timestampSeconds(endHours, endMinutes, endSeconds);
        if (end <= start || start < 0) {
          return match;
        }
        changed = true;
        return `${prefix}全片进度 ${timelinePercent(start)}%–${timelinePercent(end)}%`;
      }
    );

    // 同一提示中残留的单点时间码也同步换算，避免再次触发秒数识别。
    rewritten = rewritten.replace(
      /(^|[^\d])(?:(\d{1,2})\s*[:：]\s*)?([0-5]?\d)\s*[:：]\s*([0-5]\d)(?=$|[^\d])/g,
      (match, prefix, hours, minutes, seconds) => {
        const point = timestampSeconds(hours, minutes, seconds);
        if (point < 0) {
          return match;
        }
        changed = true;
        return `${prefix}全片进度约 ${timelinePercent(point)}%`;
      }
    );

    // 裸秒范围（时间码已先处理，这里只吃 “3-8秒 / 29.5～30秒 / 3至8秒”），
    // 必须在单点秒数之前替换，否则结尾的 “8秒” 会先被单点规则吃掉。
    rewritten = rewritten.replace(
      // 单位同时接受“秒/秒钟/s/S”（如 0-1s、1-8s、8-15s）；s 前不允许空格、后不接字母数字，避免误伤英文词。
      new RegExp("(^|[^\\d.])(\\d{1,2}(?:\\.\\d+)?)\\s*(?:" + rangeSeparator + ")\\s*(\\d{1,2}(?:\\.\\d+)?)(?:\\s*秒(?:钟)?|[sS])(?![A-Za-z0-9])", "g"),
      (match, prefix, startText, endText) => {
        const start = Number(startText);
        const end = Number(endText);
        if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
          return match;
        }
        changed = true;
        return `${prefix}全片进度 ${timelinePercent(start)}%–${timelinePercent(end)}%`;
      }
    );

    // 中文数字裸秒范围，如 “三到八秒”“两秒～五秒”。
    const chineseNumber = "半|[零〇一二两三四五六七八九十]{1,3}(?:点[零〇一二两三四五六七八九]+)?";
    rewritten = rewritten.replace(
      new RegExp(
        "(第)?(" + chineseNumber + ")\\s*(?:" + rangeSeparator + ")\\s*(" + chineseNumber + ")\\s*秒(?:钟)?",
        "g"
      ),
      (match, ordinal, startText, endText) => {
        if (ordinal) {
          // “第3-8秒” 这类序数范围不换算，交回后续单点规则各自处理。
          return match;
        }
        const start = parseChineseSeconds(startText);
        const end = parseChineseSeconds(endText);
        if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
          return match;
        }
        changed = true;
        return `全片进度 ${timelinePercent(start)}%–${timelinePercent(end)}%`;
      }
    );

    // 处理 29.2秒、第3秒、持续0.5秒、8s 等阿拉伯数字写法（单位兼容 s/S）。
    rewritten = rewritten.replace(/(第)?(\d+(?:\.\d+)?)(?:\s*秒(?:钟)?|[sS](?![A-Za-z0-9]))/g, (match, ordinal, numberText, offset, source) => {
      const replacement = secondsAsProgress(Number(numberText), source, offset, Boolean(ordinal));
      if (!replacement) {
        return match;
      }
      changed = true;
      return replacement;
    });

    // 处理“半秒”“三秒”“二十九点五秒”等中文数字写法。
    rewritten = rewritten.replace(
      /(第)?(半|[零〇一二两三四五六七八九十]{1,3}(?:点[零〇一二两三四五六七八九]+)?)\s*秒(?:钟)?/g,
      (match, ordinal, numberText, offset, source) => {
        const replacement = secondsAsProgress(parseChineseSeconds(numberText), source, offset, Boolean(ordinal));
        if (!replacement) {
          return match;
        }
        changed = true;
        return replacement;
      }
    );

    if (aspectHolds.length) {
      rewritten = rewritten.replace(/\uE000AR(\d+)\uE001/g, (_, index) => aspectHolds[Number(index)] || "");
    }
    return applyPromptGuards(rewritten, changed);
  }

  function applyPromptGuards(rewritten, timelineChanged) {
    if (!promptGuardInjectEnabled()) {
      return rewritten;
    }
    if (timelineChanged && !rewritten.includes(TIMELINE_GUARD_MARK)) {
      const timelineGuard = [
        TIMELINE_GUARD_MARK,
        "这是一个单次、连续的视频生成任务。下方的“全片进度”只表示同一条成片内部的镜头节奏，",
        "不代表多个视频任务；不得拆分生成，不得改为多次任务。"
      ].join("");
      rewritten = `${timelineGuard}\n\n${rewritten}`;
    }

    // 旧逻辑只在提示词包含“几秒/时间码”时才注入直接生成指令。
    // 普通画面描述（例如只写 16:9）不会触发，Dola 就会先要求用户回复 A/B/C。
    // 30 秒模式下每次请求都明确禁止确认，参数以已固化的 ability_param 为准。
    if (!rewritten.includes(DIRECT_GENERATION_GUARD_MARK)) {
      const directGenerationGuard = [
        DIRECT_GENERATION_GUARD_MARK,
        "用户已经明确选定视频模型、比例和时长，参数以本请求的 chat_ability / ability_param 为准。",
        "禁止询问用户确认，禁止要求用户回复 A/B/C 或选择时长，禁止再次解释可选规格；",
        "立即调用一次视频生成能力，只回复已经受理并开始生成的确认结果。"
      ].join("");
      rewritten = `${directGenerationGuard}\n\n${rewritten}`;
    }
    return rewritten;
  }

  function editorText(editor) {
    if (!editor) return "";
    if (editor instanceof HTMLTextAreaElement || editor instanceof HTMLInputElement) {
      return String(editor.value || "");
    }
    return String(editor.innerText || editor.textContent || "");
  }

  function writeEditorText(editor, value) {
    if (!editor) return false;
    if (editor instanceof HTMLTextAreaElement || editor instanceof HTMLInputElement) {
      const prototype = editor instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
      if (setter) setter.call(editor, value);
      else editor.value = value;
    } else {
      editor.focus();
      const selection = getSelection();
      const range = document.createRange();
      range.selectNodeContents(editor);
      selection?.removeAllRanges();
      selection?.addRange(range);

      // Lexical / Slate / ProseMirror 等受控富文本不监听 textContent 赋值，
      // 而是监听 beforeinput 自行更新内部 state。先派发可取消的 beforeinput：
      // 若框架 preventDefault 并接管了插入，就不要再动 DOM，避免被 React 回滚。
      let handledByFramework = false;
      try {
        const beforeInput = new InputEvent("beforeinput", {
          bubbles: true,
          composed: true,
          cancelable: true,
          inputType: "insertText",
          data: value
        });
        handledByFramework = !editor.dispatchEvent(beforeInput);
      } catch {
        handledByFramework = false;
      }

      let inserted = false;
      if (!handledByFramework) {
        try {
          inserted = document.execCommand("insertText", false, value);
        } catch {
          inserted = false;
        }
        if (!inserted) editor.textContent = value;
      }
      selection?.removeAllRanges();
    }
    editor.dispatchEvent(new InputEvent("input", {
      bubbles: true,
      composed: true,
      inputType: "insertText",
      data: null
    }));
    editor.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }

  function visiblePromptEditors(root = document) {
    return Array.from(root.querySelectorAll(
      "textarea:not([disabled]), input[type=\"text\"]:not([disabled]), [contenteditable=\"true\"], [role=\"textbox\"]"
    )).filter((editor) => visible(editor) && editorText(editor).trim());
  }

  const ACTIONBAR_CONTROL_SELECTOR = "[data-input-engine-actionbar-control-key]";

  function centerOf(rect) {
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  }

  function distanceBetween(a, b) {
    const ca = centerOf(a);
    const cb = centerOf(b);
    return Math.hypot(ca.x - cb.x, ca.y - cb.y);
  }

  function closestToButton(editors, button) {
    const buttonRect = button.getBoundingClientRect();
    let best = null;
    let bestDistance = Infinity;
    for (const editor of editors) {
      const distance = distanceBetween(editor.getBoundingClientRect(), buttonRect);
      if (distance < bestDistance) {
        bestDistance = distance;
        best = editor;
      }
    }
    return best;
  }

  // 发送按钮与编辑器是否几何邻近：Dola/豆包改版后发送键可能换行、输入框多行变高，
  // 阈值要比旧版宽松，否则缩放窗口或工具栏换行时图标发送键会漏判。
  function nearEditor(button, editor) {
    const buttonRect = button.getBoundingClientRect();
    const editorRect = editor.getBoundingClientRect();
    return buttonRect.left >= editorRect.right - 160 &&
      buttonRect.right <= editorRect.right + 260 &&
      buttonRect.top >= editorRect.top - 80 &&
      buttonRect.bottom <= editorRect.bottom + 160;
  }

  function editorsForScope(scope, button) {
    return visiblePromptEditors(scope).filter((editor) =>
      editor !== button && !button.contains(editor) && !editor.contains(button)
    );
  }

  function resolveEditorForButton(button) {
    if (!button) return null;

    // 1) 同一 <form> 内的输入框（最可靠的语义边界）。
    const form = button.closest?.("form");
    if (form) {
      const inForm = editorsForScope(form, button);
      if (inForm.length) return closestToButton(inForm, button);
    }

    // 2) 沿祖先链逐层找（不再限制 9 层；首层命中时取几何最近的一个，
    //    避免“容器内最后一个 textbox”在多输入框页面取错）。
    let container = button.parentElement;
    while (container) {
      const editors = editorsForScope(container, button);
      if (editors.length) return closestToButton(editors, button);
      container = container.parentElement;
    }

    // 3) 全局回退：取与按钮几何最近的可见输入框，而不是页面最后一个 textbox。
    const globalEditors = editorsForScope(document, button);
    return closestToButton(globalEditors, button) || null;
  }

  function highConfidenceSend(button) {
    if (!button) return false;
    const label = textOf(button);
    return button.type === "submit" || /发送|提交|send|submit|生成/i.test(label);
  }

  function isLikelySendButton(button, editor) {
    if (!button || !editor || button.disabled || button.getAttribute("aria-haspopup")) return false;
    // 只排除模型/比例/时长控件本身（按钮是该控件或位于其内部）；
    // 发送键与其同处一个 actionbar 父容器时不再被连坐。
    if (button.hasAttribute?.("data-input-engine-actionbar-control-key") ||
        button.closest?.(ACTIONBAR_CONTROL_SELECTOR)) {
      return false;
    }
    const label = textOf(button);
    // 只拦截明确的规格选择文案；不再因含“视频/图片”字样误杀“生成视频”这类发送键。
    if (/选择?(?:模型|比例|时长)|模型切换|时长切换|seedance/i.test(label)) return false;
    if (/^\s*(?:5|10|15|30)\s*(?:s|秒)\s*$/.test(label) || /\b(?:5|10|15|30)\s*s\b/i.test(label)) return false;
    // 文案命中也要在编辑器附近，避免误改历史消息气泡上的“重新生成”按钮。
    if (highConfidenceSend(button)) return nearEditor(button, editor);
    // 无文案的纯图标发送键，按几何邻近判定。
    return nearEditor(button, editor);
  }

  // Enter 路径没有点击目标：以编辑器为中心，在 form / 祖先链里找发送按钮。
  function findSendButtonNearEditor(editor) {
    const candidates = [];
    let container = editor.parentElement;
    for (let depth = 0; container && depth < 14; depth += 1, container = container.parentElement) {
      for (const button of container.querySelectorAll("button, [role=\"button\"]")) {
        if (candidates.includes(button) || editor.contains(button) || !visible(button)) continue;
        if (isLikelySendButton(button, editor)) candidates.push(button);
      }
      if (candidates.some(highConfidenceSend)) break;
    }
    if (!candidates.length) return null;
    const editorRect = editor.getBoundingClientRect();
    candidates.sort((a, b) => {
      const confidenceDelta = (highConfidenceSend(a) ? 0 : 1) - (highConfidenceSend(b) ? 0 : 1);
      if (confidenceDelta !== 0) return confidenceDelta;
      return distanceBetween(a.getBoundingClientRect(), editorRect) -
        distanceBetween(b.getBoundingClientRect(), editorRect);
    });
    return candidates[0];
  }

  function rewriteEditorBeforeSend(editor) {
    const current = editorText(editor);
    const rewritten = rewriteTimelinePrompt(current);
    if (!current.trim() || rewritten === current) return false;
    return writeEditorText(editor, rewritten);
  }

  function resolveClickButton(target) {
    return target?.closest?.("button, [role=\"button\"]") || null;
  }

  // 等两帧再执行动作：受控富文本（React/Lexical）需要一次渲染周期把写回内容
  // flush 进内部 state；setTimeout(0) 常赶在同一批处理里，发送时取到的还是旧文本。
  function onNextFrame(action) {
    if (typeof requestAnimationFrame === "function") {
      requestAnimationFrame(() => requestAnimationFrame(action));
    } else {
      setTimeout(action, 32);
    }
  }

  function fireSyntheticClick(button) {
    button.dataset.dolaTimelineResend = "1";
    try {
      button.click();
    } finally {
      delete button.dataset.dolaTimelineResend;
    }
  }

  // 已拦截、正在等待两帧后合成重发的编辑器：期间的第二次点击/Enter 直接吞掉，
  // 防止快速双击或抖动产生两条相同生成请求。
  const pendingSends = new WeakSet();

  function installSafePromptRewrite() {
    // 在页面自身组装、签名请求之前改写输入框；不接触 fetch/XHR。

    // 第一道：pointerdown（鼠标/触摸都会触发）比 click 早一轮事件。
    // 仅对“高置信发送键”（submit / 发送·提交·生成文案）提前改写且不拦截事件，
    // 随后浏览器照常派发 click，页面拿到的已是改写后的文本，无需合成重发。
    document.addEventListener("pointerdown", (event) => {
      if (!event.isTrusted || event.pointerType === "mouse" && event.button > 0) return;
      const button = resolveClickButton(event.target);
      if (!button || !highConfidenceSend(button)) return;
      const editor = resolveEditorForButton(button);
      if (!isLikelySendButton(button, editor)) return;
      rewriteEditorBeforeSend(editor);
    }, true);

    // 第二道：click 捕获兜底——覆盖纯图标发送键、pointerdown 未命中的程序化点击。
    // pointerdown 已改写时这里幂等放行（文本已含百分比，rewrite 返回 false）。
    document.addEventListener("click", (event) => {
      if (!event.isTrusted) return;
      const button = resolveClickButton(event.target);
      if (!button || button.dataset.dolaTimelineResend === "1") return;
      const editor = resolveEditorForButton(button);
      if (!isLikelySendButton(button, editor)) return;
      if (pendingSends.has(editor)) {
        event.preventDefault();
        event.stopImmediatePropagation();
        return;
      }
      if (!rewriteEditorBeforeSend(editor)) return;

      event.preventDefault();
      event.stopImmediatePropagation();
      pendingSends.add(editor);
      onNextFrame(() => {
        try {
          fireSyntheticClick(button);
        } finally {
          pendingSends.delete(editor);
        }
      });
    }, true);

    document.addEventListener("keydown", (event) => {
      if (!event.isTrusted) return;
      if (event.key !== "Enter" || event.shiftKey) return;
      // 输入法组词确认（229=IME processing）：这次 Enter 是上屏候选词，交给原生，
      // 候选词上屏后用户再按的那一下 Enter（非 composing）才会被这里捕获改写。
      if (event.isComposing || event.keyCode === 229) return;
      const editor = event.target?.closest?.(
        "textarea, input[type=\"text\"], [contenteditable=\"true\"], [role=\"textbox\"]"
      );
      if (!editor) return;
      if (pendingSends.has(editor)) {
        event.preventDefault();
        event.stopImmediatePropagation();
        return;
      }
      if (!rewriteEditorBeforeSend(editor)) return;

      event.preventDefault();
      event.stopImmediatePropagation();
      pendingSends.add(editor);

      const release = () => pendingSends.delete(editor);

      const sendButton = findSendButtonNearEditor(editor);
      if (sendButton) {
        onNextFrame(() => {
          try {
            fireSyntheticClick(sendButton);
          } finally {
            release();
          }
        });
        return;
      }

      // 找不到发送按钮（部分编辑器靠自身 keydown 发送）：两帧后把 Enter 重新派给
      // 编辑器，让它的原生发送逻辑接管。合成事件 isTrusted=false，会在本监听器
      // 开头直接放行，不会形成循环。
      onNextFrame(() => {
        try {
          editor.dispatchEvent(new KeyboardEvent("keydown", {
            key: "Enter",
            code: "Enter",
            keyCode: 13,
            which: 13,
            bubbles: true,
            cancelable: true,
            composed: true
          }));
        } catch {
          // 极旧内核不支持 KeyboardEvent 构造：退而派发 keyup。
          try {
            editor.dispatchEvent(new Event("keyup", { bubbles: true, cancelable: true }));
          } catch {
            // 放弃自动提交，文本已完成改写并留在输入框，用户手动再发一次即可。
          }
        } finally {
          release();
        }
      });
    }, true);
  }

  function roleOf(value, inheritedRole = "") {
    const rawRole = value?.role || value?.sender_role || value?.sender_type || value?.author?.role || inheritedRole;
    return String(rawRole || "").toLowerCase();
  }

  function patchUserTimelinePrompts(value, inheritedRole = "", seen = new Set()) {
    if (!value || typeof value !== "object" || seen.has(value)) {
      return false;
    }
    seen.add(value);

    const currentRole = roleOf(value, inheritedRole);
    const blockedRole = /(?:system|assistant|bot|tool)/i.test(currentRole);
    let changed = false;

    if (Array.isArray(value)) {
      for (const item of value) {
        changed = patchUserTimelinePrompts(item, currentRole, seen) || changed;
      }
      return changed;
    }

    for (const [key, child] of Object.entries(value)) {
      const normalizedKey = key.toLowerCase();
      if (typeof child === "string" && PROMPT_FIELD_NAMES.has(normalizedKey) && !blockedRole) {
        const rewritten = rewriteTimelinePrompt(child);
        if (rewritten !== child) {
          value[key] = rewritten;
          changed = true;
        }
      } else if (child && typeof child === "object") {
        changed = patchUserTimelinePrompts(child, currentRole, seen) || changed;
      }
    }
    return changed;
  }

  function patchBody(body, requestUrl = "") {
    // Dola 域额外要求页面此刻确实停在「视频生成 + 2.5 + 30s」：开关以页面选择为准，
    // 切回普通对话 / 其他档位的瞬间即使对账定时器还没跑，也绝不改写请求。
    const dolaUiOk = !isDolaHost() || readDolaVideo30sUiActive();
    if (!duration30Enabled() || !dolaUiOk || typeof body !== "string" || !body.trim() || !isVideoCompletionUrl(requestUrl)) {
      return body;
    }

    let payload;
    try {
      payload = JSON.parse(body);
    } catch {
      return body;
    }

    // 只把用户时间线提示词改写为单片进度标记，阻止 Dola 在能力调用前把任务拆成 10/15 秒片段。
    // ability_param / 模型 / 时长一律不动：请求 URL 已带 a_bogus/msToken/fp 签名，
    // 签名后改参数会触发 710022002；30 秒能力由 document_start 的 skill-pack-main.js 注入。
    const changed = patchUserTimelinePrompts(payload);
    if (changed) {
      try { console.log("[Dola30s] /chat/completion 请求体时间轴提示词已换算"); } catch {}
      return JSON.stringify(payload);
    }

    // 方悦式兜底：仅豆包域，且 skill-pack 钩子尚未就绪时，改写 completion 里的 2.5+30。
    // Dola 域仍禁止改 ability_param（避免 710022002）。
    if (!isDolaHost() && !nativeSkillPackReady() && forceGenerationPayload(payload)) {
      try { console.log("[Dola30s] 豆包 completion 能力补丁（方悦式兜底）"); } catch {}
      return JSON.stringify(payload);
    }
    return body;
  }

  function patchRequests() {
    const originalFetch = window.fetch;
    if (typeof originalFetch === "function") {
      window.fetch = function patchedFetch(input, init = {}) {
        if (init && typeof init.body === "string") {
          const requestUrl = getRequestUrl(input);
          const body = patchBody(init.body, requestUrl);
          if (body !== init.body) {
            init = { ...init, body };
          }
        }
        // 兼容 fetch(new Request(...))。旧版只处理 init.body，这正是部分 Dola 页面失效的来源。
        if (typeof Request !== "undefined" && input instanceof Request && !init.body && !/^(GET|HEAD)$/i.test(input.method)) {
          const receiver = this;
          return input.clone().text().then((requestBody) => {
            const patched = patchBody(requestBody, input.url);
            if (patched === requestBody) {
              return originalFetch.call(receiver, input, init);
            }
            return originalFetch.call(receiver, new Request(input, { ...init, body: patched }));
          }, () => originalFetch.call(receiver, input, init));
        }
        return originalFetch.call(this, input, init);
      };
    }

    const originalOpen = XMLHttpRequest.prototype.open;
    const originalSend = XMLHttpRequest.prototype.send;
    if (typeof originalOpen === "function") {
      XMLHttpRequest.prototype.open = function patchedOpen(method, url) {
        this.__watermarkFreeRequestUrl = getRequestUrl(url);
        return originalOpen.apply(this, arguments);
      };
    }
    if (typeof originalSend === "function") {
      XMLHttpRequest.prototype.send = function patchedSend(body) {
        return originalSend.call(this, patchBody(body, this.__watermarkFreeRequestUrl || location.href));
      };
    }
  }

  function exactDuration(element) {
    const match = durationText(textOf(element)).match(/^(5|10|15|30)(s|秒)$/i);
    return match ? Number(match[1]) : 0;
  }

  function findDurationMenuRoot() {
    const candidates = Array.from(document.querySelectorAll(
      "[role=\"menu\"], [role=\"listbox\"], [data-slot*=\"dropdown-menu\"], [class*=\"popover\"], [class*=\"dropdown\"], div"
    ))
      .filter((element) => visible(element))
      .filter((element) => {
        const rect = element.getBoundingClientRect();
        if (rect.width < 70 || rect.width > 520 || rect.height < 40 || rect.height > 520) {
          return false;
        }
        const text = durationText(textOf(element));
        const hasNativeDurations = /5(s|秒)/i.test(text) && /10(s|秒)/i.test(text);
        const looksLikeWholeToolbar = /Seedance|比例|参考图|模型|Model|Fast/i.test(text) && rect.width > 360;
        return hasNativeDurations && !looksLikeWholeToolbar;
      })
      .sort((first, second) => {
        const firstRect = first.getBoundingClientRect();
        const secondRect = second.getBoundingClientRect();
        return firstRect.width * firstRect.height - secondRect.width * secondRect.height;
      });
    return candidates[0] || null;
  }

  function replaceDurationLabel(element, target) {
    try {
      const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
      let changed = false;
      while (walker.nextNode()) {
        const next = String(walker.currentNode.nodeValue || "").replace(/(?:5|10|15|30)(s|秒)/ig, `${target}s`);
        if (next !== walker.currentNode.nodeValue) {
          walker.currentNode.nodeValue = next;
          changed = true;
        }
      }
      if (changed) {
        return;
      }
    } catch {
      // Fall through to replacing the whole label.
    }
    element.textContent = `${target}s`;
  }

  function durationCheckMarks(item) {
    return Array.from(item?.querySelectorAll?.("svg") || []);
  }

  function setDurationMenuSelected(item, selected) {
    item?.setAttribute?.("aria-selected", selected ? "true" : "false");
    if (selected) {
      item?.setAttribute?.("data-state", "checked");
    } else {
      item?.removeAttribute?.("data-state");
    }
    for (const mark of durationCheckMarks(item)) {
      mark.style.visibility = selected ? "visible" : "hidden";
    }
  }

  function syncDurationMenuSelection(root) {
    if (!root) {
      return;
    }
    const targetDuration = selectedDuration();
    const options = Array.from(root.querySelectorAll("[role=\"menuitem\"], [role=\"option\"], li, button, div"))
      .filter((element) => element !== root && visible(element) && exactDuration(element) > 0);
    for (const option of options) {
      const value = exactDuration(option);
      if (targetDuration === 15 || targetDuration === 30) {
        setDurationMenuSelected(option, value === targetDuration);
      } else if (option.getAttribute(MENU_MARK) === "30") {
        setDurationMenuSelected(option, false);
      }
    }
  }

  function updateToolbarDurationText(seconds) {
    if (!document.body) {
      return;
    }
    const menuRoot = findDurationMenuRoot();
    const isInMenu = (element) => Boolean(
      element && (menuRoot?.contains?.(element) || element.closest?.("[role=\"menu\"], [role=\"listbox\"], [data-slot=\"dropdown-menu-content\"]"))
    );
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (!/^\s*(5|10|15|30)s\s*$/i.test(node.nodeValue || "")) {
        continue;
      }
      const element = node.parentElement;
      if (!visible(element) || isInMenu(element)) {
        continue;
      }
      const trigger = element.closest?.("button, [role=\"button\"], [aria-haspopup], [aria-expanded], [data-slot*=\"trigger\"]") || element;
      if (visible(trigger) && !isInMenu(trigger)) {
        node.nodeValue = `${seconds}s`;
      }
    }
  }

  function holdToolbarDurationText(seconds) {
    for (const delay of [0, 50, 120, 300, 800, 1500, 3000]) {
      setTimeout(() => updateToolbarDurationText(seconds), delay);
    }
  }

  function stripRedundantDurationTalk(text) {
    let result = String(text || "");
    // Dola 在已经接收 30 秒请求后，偶尔仍会输出旧能力提示。只删提示句，不碰生成确认和结果。
    result = result.replace(
      /视频生成目前支持\s*\d+\s*(?:到|至|[-~～—])\s*\d+\s*秒[。.!！]?\s*/g,
      ""
    );
    result = result.replace(
      /我将按照默认\s*(?:5|10|15)\s*秒生成[。.!！]?\s*/g,
      ""
    );
    result = result.replace(
      /请(?:你)?(?:选择|选取|从中选择)[^。.!！\n]{0,40}(?:5\s*秒)[^。.!！\n]{0,20}(?:10\s*秒)[^。.!！\n]{0,20}(?:15\s*秒)[。.!！]?\s*/g,
      ""
    );
    return result;
  }

  function cleanRedundantDurationTalk(root = document.body) {
    if (!duration30Enabled() || host !== "dola.com" && !host.endsWith(".dola.com") || !root) {
      return;
    }
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      const parent = node.parentElement;
      if (!parent || parent.closest?.("script, style, textarea, input, [contenteditable=\"true\"]")) {
        continue;
      }
      const current = String(node.nodeValue || "");
      const cleaned = stripRedundantDurationTalk(current);
      if (cleaned !== current) {
        node.nodeValue = cleaned;
      }
    }
  }

  function installDolaQuietMode() {
    if (typeof MutationObserver === "undefined" || !document.body) {
      return;
    }
    cleanRedundantDurationTalk();
    let queued = false;
    const observer = new MutationObserver(() => {
      if (queued) {
        return;
      }
      queued = true;
      queueMicrotask(() => {
        queued = false;
        cleanRedundantDurationTalk();
      });
    });
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
  }

  function injectDurationMenuOption() {
    const root = findDurationMenuRoot();
    if (!root) {
      return false;
    }

    const target = modelTargetDuration();
    let options = Array.from(root.querySelectorAll("[role=\"menuitem\"], [role=\"option\"], li, button, div"))
      .filter((element) => element !== root && visible(element) && exactDuration(element) > 0);

    for (const option of options) {
      if (option.getAttribute(MENU_MARK) === "30" && target !== 30) {
        option.remove();
      }
    }

    options = Array.from(root.querySelectorAll("[role=\"menuitem\"], [role=\"option\"], li, button, div"))
      .filter((element) => element !== root && visible(element) && exactDuration(element) > 0);

    // 15/30 必须来自能力配置形成的原生 React 选项，不再克隆 DOM 菜单。
    // rules.json 会在页面初始化阶段提供完整 5/10/15/30 配置。

    options = Array.from(root.querySelectorAll("[role=\"menuitem\"], [role=\"option\"], li, button, div"))
      .filter((element) => element !== root && visible(element) && exactDuration(element) > 0);
    for (const option of options) {
      const value = exactDuration(option);
      if (value === 30 && !option.hasAttribute(MENU_MARK)) native30Observed = true;
      if ((value === 5 || value === 10 || value === 15 || value === 30) && !option.hasAttribute(NATIVE_BOUND_MARK)) {
        option.setAttribute(NATIVE_BOUND_MARK, String(value));
        option.addEventListener("click", () => {
          if (value === 15 || value === 30) {
            // 用户在页面亲手选回 30s：清除宿主软件的显式关闭锁。
            if (value === 30) setUiSuppress(false);
            saveTargetDuration(value);
          } else {
            try { localStorage.removeItem(TARGET_DURATION_KEY); } catch {}
            saveDuration30(false);
          }
        }, true);
      }
    }

    syncDurationMenuSelection(root);
    return true;
  }

  function enhanceDurationOptions(fullReconcile = false) {
    injectDurationMenuOption();
    reconcileDolaUiState(fullReconcile);
  }

  // ===== 宿主软件（WebView2 / C#）桥接；扩展环境下为无副作用的冗余接口 =====
  // C# 侧（Ctrl+M / Automation）通过 window.setDola30s / setDoubao30s 切换开关，
  // 通过 getDola30sStatus 读取 { on, duration, ready }，字段名勿改（C# 有依赖）。
  window.setDola30s = function (on) {
    if (on) {
      // Dola 域真正的开启状态由页面选择对账决定（C# 随后会可信点击 视频生成/2.5/30s）；
      // 这里只负责预置状态并清除关闭锁。豆包域则直接以此开关为准。
      setUiSuppress(false);
      saveTargetDuration(30);
    } else {
      // Dola 域：加会话锁，防止 UI 停在 30s 时被每秒对账自动弹回；
      // 用户在页面重新点选 30s 即解锁。豆包域 setUiSuppress 无副作用。
      if (isDolaHost()) setUiSuppress(true);
      try { localStorage.setItem(DURATION_KEY, "0"); } catch {}
      try { localStorage.removeItem(TARGET_DURATION_KEY); } catch {}
      publishDurationState();
    }
    enhanceDurationOptions();
    try { console.log("[Dola30s] 2.5+30秒助手" + (duration30Enabled() ? "已开启" : "已关闭")); } catch (e) {}
    return duration30Enabled();
  };
  window.getDola30sStatus = function () {
    let ready = false;
    try { ready = document.documentElement.getAttribute("data-dola-native-30-ready") === "1"; } catch (e) {}
    return JSON.stringify({ on: duration30Enabled(), duration: selectedDuration(), ready: ready });
  };
  // 兼容旧 C# 调用名（原 Doubao30sAssistJs）
  window.setDoubao30s = function (on) {
    return window.setDola30s(!!on);
  };
  window.getDoubao30sStatus = function () {
    try {
      const s = JSON.parse(window.getDola30sStatus());
      return JSON.stringify({ on: !!s.on, applied: 0, duration: s.duration, ready: s.ready });
    } catch (e) {
      return JSON.stringify({ on: false, applied: 0 });
    }
  };

  // 恢复 v1.4.3 方式：fetch/XHR 发出 /chat/completion 前在请求体里换算用户提示词，
  // 只改提示词文本、不碰签名后的 ability_param（避免 710022002）。
  // 不再改写页面输入框（installSafePromptRewrite 停用），页面里剧本原文保持不变。
  publishDurationState();
  patchRequests();
  enhanceDurationOptions();
  // 定时器执行完整对账：页面切到非视频模式 / 其他档位时把开关关回（Dola 域）。
  setInterval(() => enhanceDurationOptions(true), 1000);
})();
