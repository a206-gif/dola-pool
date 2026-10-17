(function () {
  if (!globalThis.chrome?.runtime?.id) {
    console.warn("[dola30] content-panel: 缺少扩展上下文，无法挂载哆啦助手面板");
    return;
  }

  const PANEL_ID = "watermark-free-media-panel";
  const INSTANCE_KEY = "__watermarkFreeMediaPanelActive";
  const DURATION_KEY = "intl_doubao_enable_30s_v1";
  const TARGET_DURATION_KEY = "intl_doubao_target_duration_v1";
  const DURATION_STATE_ATTRIBUTE = "data-watermark-free-duration-30";
  const DURATION_STATE_EVENT = "watermark-free-duration-30-change";
  const MEDIA_APIS_ATTRIBUTE = "data-dola-media-fallback-apis";
  const MEDIA_APIS_EVENT = "dola-media-fallback-apis";
  const QIANWEN_MEDIA_ATTRIBUTE = "data-dola-qianwen-media";
  const QIANWEN_MEDIA_EVENT = "dola-qianwen-media";
  const XYQ_MEDIA_ATTRIBUTE = "data-dola-xyq-media";
  const XYQ_MEDIA_EVENT = "dola-xyq-media";
  const MINIMIZED_KEY = "intl_doubao_media_panel_minimized_v1";
  const PANEL_POS_KEY = "intl_doubao_media_panel_pos_v5";
  const PANEL_TOP_PX = 8;
  const PANEL_RIGHT_GAP_PX = 180;
  const PARSE_PANEL_KEY = "intl_doubao_media_panel_parse_v1";
  const RESOLUTION_KEY = "intl_doubao_target_resolution_v1";
  const RESOLUTION_STATUS_ATTRIBUTE = "data-watermark-free-resolution-status";
  const RESOLUTION_STATUS_EVENT = "watermark-free-resolution-status";
  const RESOLUTION_BRIDGE_ATTRIBUTE = "data-watermark-free-resolution-bridge";
  const MEDIA_CAPTURE_ATTRIBUTE = "data-watermark-free-media-capture";
  const MEDIA_RETRY_EVENT = "watermark-free-media-retry";
  const PROMPT_GUARD_INJECT_KEY = "intl_doubao_prompt_guard_inject_v1";
  const TIMELINE_REWRITE_KEY = "intl_doubao_timeline_rewrite_v1";
  const items = new Map();

  if (globalThis[INSTANCE_KEY]) {
    return;
  }
  globalThis[INSTANCE_KEY] = true;

  const existingHost = document.getElementById(PANEL_ID);
  existingHost?.remove();

  const host = document.createElement("div");
  host.id = PANEL_ID;
  forceHostVisible();
  mountHost();

  const shadow = host.attachShadow({ mode: "open" });
  shadow.innerHTML = `
    <style>
      :host {
        all: initial;
        position: fixed;
        top: 8px;
        left: auto;
        right: 180px;
        bottom: auto;
        z-index: 2147483647;
        font-family: "Microsoft YaHei UI", "Microsoft YaHei", Arial, sans-serif;
      }

      .shell {
        position: relative;
        display: flex;
        flex-direction: column;
        align-items: flex-start;
        gap: 8px;
      }

      .bar {
        display: flex;
        flex-direction: row;
        align-items: center;
        gap: 6px;
      }

      .panel {
        width: auto;
        flex: 0 0 auto;
        box-sizing: border-box;
        display: flex;
        flex-direction: row;
        align-items: stretch;
        color: #34304B;
        background: #FFFFFF;
        border: 2px solid rgba(122, 92, 240, 0.16);
        border-radius: 14px;
        box-shadow: 0 8px 24px rgba(52, 48, 75, 0.16);
        overflow: hidden;
      }

      /* ===== 品牌头部：哆啦助手渐变 ===== */
      .header {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 8px 10px 8px 12px;
        background: linear-gradient(100deg, #7A5CF0 0%, #B46FE0 55%, #5AA9F0 100%);
        cursor: pointer;
        touch-action: none;
        user-select: none;
      }

      .header:active {
        cursor: pointer;
      }

      .logo {
        width: 22px;
        height: 22px;
        min-width: 22px;
        border-radius: 6px;
        object-fit: contain;
        display: block;
      }

      .title {
        flex: 0 0 auto;
        min-width: 0;
        color: #fff;
        font-size: 13px;
        line-height: 20px;
        font-weight: 700;
        letter-spacing: 0.2px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .panel-toggle {
        display: none;
      }

      /* ===== 开关：左右拨动，文字在下方一行 ===== */
      .switch-rows,
      .parse-block {
        padding: 8px 8px 6px;
        background: #F8F5FE;
        border-bottom: 1px solid #EFE8FB;
      }

      .switch-rows {
        display: flex;
        flex-direction: row;
        align-items: flex-start;
        gap: 2px;
        border-bottom: 0;
      }

      .switch-row {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 4px;
        width: auto;
        min-height: 28px;
        padding: 0 2px;
      }

      .switch-label {
        flex: 0 0 auto;
        font-size: 11px;
        line-height: 14px;
        font-weight: 600;
        color: #6E6A88;
        text-align: center;
        white-space: nowrap;
        user-select: none;
      }

      .switch-label.on {
        color: #2E9E5B;
      }

      .switch {
        position: relative;
        display: inline-block;
        flex-shrink: 0;
        width: 38px;
        height: 22px;
        min-width: 38px;
        cursor: pointer;
        -webkit-user-select: none;
        user-select: none;
      }

      .switch input {
        position: absolute;
        width: 0;
        height: 0;
        opacity: 0;
        margin: 0;
      }

      .switch .track {
        position: absolute;
        inset: 0;
        border-radius: 999px;
        background: #D8D2E8;
        transition: background 0.18s ease;
      }

      .switch .thumb {
        position: absolute;
        top: 2px;
        left: 2px;
        width: 18px;
        height: 18px;
        border-radius: 50%;
        background: #fff;
        box-shadow: 0 1px 3px rgba(52, 48, 75, 0.3);
        transition: transform 0.18s ease;
      }

      .switch input:checked + .track {
        background: linear-gradient(100deg, #3FBF7F, #2FBF71);
      }

      .switch input:checked + .track .thumb {
        transform: translateX(16px);
      }

      .cookie-edit-btn {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 4px;
        width: auto;
        margin: 0;
        padding: 0;
        border: 0;
        background: transparent;
        cursor: pointer;
        color: #6E6A88;
      }

      .cookie-edit-mark {
        width: 60px;
        height: 22px;
        border-radius: 999px;
        background: linear-gradient(100deg, #7A5CF0, #9A7BF4);
        color: #fff;
        font-size: 11px;
        font-weight: 700;
        line-height: 22px;
        text-align: center;
      }

      .cookie-edit-text {
        font-size: 11px;
        line-height: 14px;
        font-weight: 600;
        text-align: center;
        white-space: nowrap;
      }

      .parse-block {
        padding-top: 6px;
        padding-bottom: 8px;
      }

      .list-panel {
        display: none;
        width: 220px;
        box-sizing: border-box;
        flex-direction: column;
        min-height: 0;
        color: #34304B;
        background: #FFFFFF;
        border: 1px solid rgba(122, 92, 240, 0.16);
        border-radius: 14px;
        box-shadow: 0 0px 5px rgba(122, 92, 240, 0.9);
        overflow: hidden;
      }

      .list-panel[data-open="1"] {
        display: flex;
      }

      /* ===== 资源列表：在横条下方展开 ===== */
      .list {
        flex: 1 1 auto;
        min-height: 0;
        overflow: auto;
        padding: 8px;
        background: #FFFFFF;
      }

      .empty {
        padding: 20px 10px;
        color: #A79FBE;
        font-size: 13px;
        line-height: 20px;
        text-align: center;
      }

      .item {
        display: grid;
        grid-template-columns: auto 1fr auto;
        gap: 8px;
        align-items: center;
        padding: 7px 8px;
        border: 1px solid #EAE3F7;
        border-radius: 10px;
        background: #FFFFFF;
      }

      .item + .item {
        margin-top: 6px;
      }

      .label {
        min-width: 0;
        color: #34304B;
        font-size: 13px;
        line-height: 18px;
        font-weight: 600;
        display: flex;
        align-items: center;
        gap: 6px;
      }

      .label .index {
        color: #B3AACB;
        font-size: 11px;
        font-weight: 400;
        font-variant-numeric: tabular-nums;
      }

      .tag {
        display: inline-block;
        min-width: 32px;
        padding: 1px 6px;
        border-radius: 999px;
        color: #fff;
        font-size: 11px;
        font-weight: 700;
        line-height: 16px;
        text-align: center;
      }

      .tag.video {
        background: linear-gradient(100deg, #7A5CF0, #9A7BF4);
      }

      .tag.image {
        background: linear-gradient(100deg, #14B8A6, #2BB3A3);
      }

      .media-thumb {
        width: 56px;
        height: 42px;
        border-radius: 8px;
        object-fit: cover;
        background: #EFE8FB;
        border: 1px solid #EAE3F7;
        display: block;
        flex-shrink: 0;
      }

      .media-thumb-fallback {
        width: 56px;
        height: 42px;
        border-radius: 8px;
        background: linear-gradient(135deg, #EFE8FB, #E2D9F8);
        border: 1px solid #EAE3F7;
        color: #9A7BF4;
        font-size: 10px;
        font-weight: 700;
        display: flex;
        align-items: center;
        justify-content: center;
      }

      button.dl {
        height: 28px;
        padding: 0 14px;
        border: 0;
        border-radius: 8px;
        background: linear-gradient(100deg, #7A5CF0, #9A7BF4);
        color: #fff;
        font-size: 12px;
        font-weight: 600;
        line-height: 28px;
        cursor: pointer;
        transition: filter 0.15s ease;
      }

      button.dl:hover {
        filter: brightness(1.08);
      }

      .label .spec {
        color: #6E6A88;
        font-size: 11px;
        margin-left: 4px;
      }

      .list-tools {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        align-items: center;
        padding: 8px 8px 0;
        background: #FFFFFF;
      }

      .list-tools button {
        height: 26px;
        padding: 0 10px;
        border: 1px solid #D8D2E8;
        border-radius: 8px;
        background: #fff;
        color: #34304B;
        font: inherit;
        font-size: 12px;
        font-weight: 600;
        cursor: pointer;
      }

      .tool-state {
        flex: 1 1 100%;
        color: #6E6A88;
        font-size: 11px;
        line-height: 16px;
      }

      /* ===== 参考上传：把音频/视频/照片交给 Dola 视频生成输入区 ===== */
      .ref-panel {
        width: 220px;
        box-sizing: border-box;
        display: flex;
        flex-direction: column;
        gap: 6px;
        padding: 8px;
        color: #34304B;
        background: #FFFFFF;
        border: 1px solid rgba(122, 92, 240, 0.16);
        border-radius: 14px;
        box-shadow: 0 0 5px rgba(122, 92, 240, 0.6);
      }

      .ref-title {
        font-size: 11px;
        line-height: 14px;
        font-weight: 700;
        color: #6E6A88;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .ref-btns {
        display: flex;
        gap: 6px;
      }

      .ref-btn {
        flex: 1 1 0;
        height: 26px;
        padding: 0 4px;
        border: 0;
        border-radius: 8px;
        background: linear-gradient(100deg, #7A5CF0, #9A7BF4);
        color: #fff;
        font: inherit;
        font-size: 12px;
        font-weight: 600;
        line-height: 26px;
        cursor: pointer;
        white-space: nowrap;
        transition: filter 0.15s ease;
      }

      .ref-btn:hover {
        filter: brightness(1.08);
      }

      .ref-btn:disabled {
        opacity: 0.55;
        cursor: default;
        filter: none;
      }

      .ref-btn[data-ref-kind="video"] {
        background: linear-gradient(100deg, #5AA9F0, #6F86F2);
      }

      .ref-btn[data-ref-kind="image"] {
        background: linear-gradient(100deg, #14B8A6, #2BB3A3);
      }

      .ref-state {
        min-height: 14px;
        font-size: 11px;
        line-height: 14px;
        color: #A79FBE;
        word-break: break-all;
      }

      .ref-state.ok {
        color: #2E9E5B;
      }

      .ref-state.err {
        color: #D25B5B;
      }

      /* ===== 收起：只留「哆啦助手」标题，右边开关和解析列表隐藏 ===== */
      :host([data-minimized="1"]) .switch-rows,
      :host([data-minimized="1"]) .ref-panel,
      :host([data-minimized="1"]) .list-panel {
        display: none;
      }
    </style>
    <div class="shell">
      <div class="bar">
      <section class="panel" aria-label="哆啦助手 无水印资源面板">
        <div class="header">
          <div class="title">哆啦助手</div>
          <button class="panel-toggle" type="button" aria-label="最小化资源面板" title="最小化资源面板">
            <span class="panel-toggle-icon" aria-hidden="true">−</span>
          </button>
        </div>
        <div class="switch-rows">
          <div class="switch-row doubao-only">
            <label class="switch" title="SD2.5+30秒">
              <input class="duration-toggle" type="checkbox">
              <span class="track"><span class="thumb"></span></span>
            </label>
            <span class="switch-label switch-label-duration">SD2.5+30秒</span>
          </div>
          <div class="switch-row doubao-only">
            <label class="switch" title="开启 Debugger 深度嗅探">
              <input class="sniff-deep-toggle" type="checkbox">
              <span class="track"><span class="thumb"></span></span>
            </label>
            <span class="switch-label switch-label-deep">深度嗅探</span>
          </div>
          <div class="switch-row doubao-only">
            <label class="switch" title="自动 Debugger 嗅探">
              <input class="sniff-auto" type="checkbox" checked>
              <span class="track"><span class="thumb"></span></span>
            </label>
            <span class="switch-label switch-label-auto">自动嗅探</span>
          </div>
          <div class="switch-row doubao-only">
            <label class="switch" title="发送时在正文前注入【直接生成控制】等指令（关闭则不附加控制段）">
              <input class="prompt-guard-toggle" type="checkbox">
              <span class="track"><span class="thumb"></span></span>
            </label>
            <span class="switch-label switch-label-guard">控制指令</span>
          </div>
          <div class="switch-row doubao-only">
            <label class="switch" title="把提示词里的秒数/时间码换成全片进度百分比（关闭后原文发送）">
              <input class="timeline-rewrite-toggle" type="checkbox">
              <span class="track"><span class="thumb"></span></span>
            </label>
            <span class="switch-label switch-label-timeline">秒数换算</span>
          </div>
          <div class="switch-row">
            <button type="button" class="cookie-edit-btn" title="打开 Cookie 编辑器，查看和导入当前页的 Cookie">
              <span class="cookie-edit-mark">Facebook</span>
              <span class="cookie-edit-text">Cookie编辑</span>
            </button>
          </div>
          <div class="switch-row">
            <label class="switch" title="在下方展开资源解析列表">
              <input class="parse-panel-toggle" type="checkbox">
              <span class="track"><span class="thumb"></span></span>
            </label>
            <span class="switch-label switch-label-parse">解析列表</span>
          </div>
        </div>
      </section>
      </div>
      <section class="ref-panel" aria-label="参考上传">
        <div class="ref-title" title="选择本地素材，直接喂给 Dola 视频生成输入区（使用前先进入视频生成技能）">参考上传 · 交给 Dola 视频生成</div>
        <div class="ref-btns">
          <button type="button" class="ref-btn" data-ref-kind="audio" title="选择 mp3 / wav 音频参考">选择音频</button>
          <button type="button" class="ref-btn" data-ref-kind="video" title="选择 mp4 / mov 视频参考">选择视频</button>
          <button type="button" class="ref-btn" data-ref-kind="image" title="选择图片参考">选择照片</button>
        </div>
        <div class="ref-state">音频 mp3/wav · 视频 mp4/mov · 图片 jpg/png</div>
      </section>
      <div class="list-panel" data-open="0">
        <div class="list-tools doubao-only">
          <button type="button" class="resolution-diagnostics">诊断</button>
          <button type="button" class="media-retry">重试提取</button>
          <span class="tool-state"></span>
        </div>
        <div class="list">
          <div class="empty">等待捕获资源…</div>
        </div>
      </div>
    </div>
  `;

  const list = shadow.querySelector(".list");
  const listPanel = shadow.querySelector(".list-panel");
  const refreshParseButton = shadow.querySelector(".refresh-parse");
  const copyVideoButton = shadow.querySelector(".copy-video-url");
  const channelPop = shadow.querySelector(".channel-pop");
  let lastFallbackUrls = [];
  const parsePanelToggle = shadow.querySelector(".parse-panel-toggle");
  const parseLabel = shadow.querySelector(".switch-label-parse");
  const durationToggle = shadow.querySelector(".duration-toggle");
  const durationLabel = shadow.querySelector(".switch-label-duration");
  const sniffDeepToggle = shadow.querySelector(".sniff-deep-toggle");
  const sniffAuto = shadow.querySelector(".sniff-auto");
  const sniffDeepLabel = shadow.querySelector(".switch-label-deep");
  const sniffAutoLabel = shadow.querySelector(".switch-label-auto");
  const promptGuardToggle = shadow.querySelector(".prompt-guard-toggle");
  const promptGuardLabel = shadow.querySelector(".switch-label-guard");
  const timelineRewriteToggle = shadow.querySelector(".timeline-rewrite-toggle");
  const timelineRewriteLabel = shadow.querySelector(".switch-label-timeline");
  const cookieEditButton = shadow.querySelector(".cookie-edit-btn");
  const toolState = shadow.querySelector(".tool-state");
  const panelToggle = shadow.querySelector(".panel-toggle");
  const durationControlSupported = /(^|\.)(doubao|dola)\.com$/i.test(location.hostname);
  const sniffControlSupported = durationControlSupported;
  let currentSourceKey = "";
  let statusText = "等待捕获资源";
  let minimized = false;

  cookieEditButton?.addEventListener("click", () => {
    document.dispatchEvent(new CustomEvent("dola-open-cookie-editor"));
  });

  function readMinimizedState() {
    try {
      return localStorage.getItem(MINIMIZED_KEY) === "1";
    } catch {
      return false;
    }
  }

  function setMinimizedState(nextValue, persist = true) {
    const next = Boolean(nextValue);
    minimized = next;
    if (minimized) host.setAttribute("data-minimized", "1");
    else host.removeAttribute("data-minimized");
    if (persist) {
      try {
        localStorage.setItem(MINIMIZED_KEY, minimized ? "1" : "0");
      } catch {
        // Keep the current visual state when storage is unavailable.
      }
    }
    const label = minimized ? "点击展开" : "点击收起";
    panelHeader?.setAttribute("title", label);
    panelToggle?.setAttribute("aria-label", label);
    panelToggle?.setAttribute("title", label);
  }

  const panelHeader = shadow.querySelector(".header");
  let dragSession = null;

  function readPanelOffset() {
    const left = parseFloat(host.style.getPropertyValue("left"));
    const top = parseFloat(host.style.getPropertyValue("top"));
    return {
      left: Number.isFinite(left) ? left : Math.max(8, window.innerWidth - PANEL_RIGHT_GAP_PX),
      top: Number.isFinite(top) ? top : PANEL_TOP_PX
    };
  }

  function viewBox() {
    const pad = 8;
    const vv = window.visualViewport;
    if (vv && vv.width > 0 && vv.height > 0) {
      return {
        left: vv.offsetLeft + pad,
        top: vv.offsetTop + pad,
        right: vv.offsetLeft + vv.width - pad,
        bottom: vv.offsetTop + vv.height - pad
      };
    }
    return {
      left: pad,
      top: pad,
      right: Math.max(pad + 1, window.innerWidth - pad),
      bottom: Math.max(pad + 1, window.innerHeight - pad)
    };
  }

  function hostUnionRect() {
    const hr = host.getBoundingClientRect();
    return { left: hr.left, top: hr.top, right: hr.right, bottom: hr.bottom };
  }

  function placePanel(left, top) {
    host.style.setProperty("left", `${left}px`, "important");
    host.style.setProperty("top", `${top}px`, "important");
    host.style.setProperty("right", "auto", "important");
    host.style.setProperty("bottom", "auto", "important");
  }

  function applyPanelOffset(left, top) {
    placePanel(left, top);
    const box = viewBox();
    let l = left;
    let t = top;
    for (let i = 0; i < 2; i += 1) {
      const u = hostUnionRect();
      if (!(u.right > u.left) || !(u.bottom > u.top)) break;
      if (u.right > box.right) l -= u.right - box.right;
      if (u.left < box.left) l += box.left - u.left;
      if (u.bottom > box.bottom) t -= u.bottom - box.bottom;
      if (u.top < box.top) t += box.top - u.top;
      l = Math.max(8, l);
      t = Math.max(8, t);
      placePanel(l, t);
    }
    return { left: l, top: t };
  }

  let keepInViewTimer = 0;
  function queueKeepPanelInView() {
    if (keepInViewTimer) cancelAnimationFrame(keepInViewTimer);
    keepInViewTimer = requestAnimationFrame(() => {
      keepInViewTimer = 0;
      const { left, top } = readPanelOffset();
      applyPanelOffset(left, top);
    });
  }

  let defaultPlaceTries = 0;
  function applyDefaultPanelOffset() {
    const width = host.getBoundingClientRect().width;
    if (!(width > 1)) {
      if (defaultPlaceTries < 8) {
        defaultPlaceTries += 1;
        requestAnimationFrame(applyDefaultPanelOffset);
      }
      return;
    }
    const vv = window.visualViewport;
    const viewLeft = vv && vv.width > 0 ? vv.offsetLeft : 0;
    const viewW = vv && vv.width > 0 ? vv.width : window.innerWidth;
    applyPanelOffset(viewLeft + viewW - width - PANEL_RIGHT_GAP_PX, PANEL_TOP_PX);
  }

  function restorePanelOffset() {
    try {
      const raw = localStorage.getItem(PANEL_POS_KEY);
      if (!raw) {
        applyDefaultPanelOffset();
        return;
      }
      const parsed = JSON.parse(raw);
      if (typeof parsed?.left === "number" && typeof parsed?.top === "number") {
        applyPanelOffset(parsed.left, parsed.top);
        return;
      }
    } catch {
      // fall through
    }
    applyDefaultPanelOffset();
  }

  function persistPanelOffset() {
    const { left, top } = readPanelOffset();
    try {
      localStorage.setItem(PANEL_POS_KEY, JSON.stringify({ left, top }));
    } catch {
      // ignore
    }
  }

  restorePanelOffset();
  window.addEventListener("resize", queueKeepPanelInView);
  window.visualViewport?.addEventListener("resize", queueKeepPanelInView);
  window.visualViewport?.addEventListener("scroll", queueKeepPanelInView);

  function beginDrag(event, captureEl) {
    if (event.button !== 0) return;
    const offset = readPanelOffset();
    dragSession = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      startLeft: offset.left,
      startTop: offset.top,
      captureEl,
      moved: false
    };
    captureEl.setPointerCapture(event.pointerId);
  }

  function moveDrag(event) {
    if (!dragSession || event.pointerId !== dragSession.pointerId) return;
    const dx = event.clientX - dragSession.startX;
    const dy = event.clientY - dragSession.startY;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) dragSession.moved = true;
    if (!dragSession.moved) return;
    applyPanelOffset(dragSession.startLeft + dx, dragSession.startTop + dy);
  }

  function finishDrag(event) {
    if (!dragSession || event.pointerId !== dragSession.pointerId) return;
    const moved = dragSession.moved;
    const captureEl = dragSession.captureEl;
    dragSession = null;
    try {
      captureEl?.releasePointerCapture(event.pointerId);
    } catch {
      // ignore
    }
    if (moved) persistPanelOffset();
    else if (event.type === "pointerup" && captureEl === panelHeader) {
      setMinimizedState(!minimized);
      queueKeepPanelInView();
    }
  }

  panelHeader?.addEventListener("pointerdown", (event) => {
    if (event.target.closest(".panel-toggle")) return;
    beginDrag(event, panelHeader);
    if (dragSession) event.preventDefault();
  });
  panelHeader?.addEventListener("pointermove", moveDrag);
  panelHeader?.addEventListener("pointerup", finishDrag);
  panelHeader?.addEventListener("pointercancel", finishDrag);
  setMinimizedState(readMinimizedState(), false);

  function readParsePanelState() {
    try {
      const v = localStorage.getItem(PARSE_PANEL_KEY);
      if (v === null || v === "") return false;
      return v === "1";
    } catch {
      return false;
    }
  }

  function setParsePanelOpen(open, persist = true) {
    const on = Boolean(open);
    if (parsePanelToggle) parsePanelToggle.checked = on;
    if (listPanel) listPanel.setAttribute("data-open", on ? "1" : "0");
    if (parseLabel) parseLabel.classList.toggle("on", on);
    if (persist) {
      try {
        localStorage.setItem(PARSE_PANEL_KEY, on ? "1" : "0");
      } catch {
        // ignore
      }
    }
    if (on) {
      requestPageMediaScan();
      consumeFallbackApis();
      render();
    }
    queueKeepPanelInView();
  }

  setParsePanelOpen(readParsePanelState(), false);
  parsePanelToggle?.addEventListener("change", () => {
    setParsePanelOpen(!!parsePanelToggle.checked);
  });

  if (durationControlSupported) {
    syncDurationControl();
    durationToggle.addEventListener("change", () => {
      setDuration30Enabled(durationToggle.checked);
    });
    document.addEventListener(DURATION_STATE_EVENT, syncDurationControl);
  } else {
    shadow.querySelectorAll(".switch-row.doubao-only, .list-tools").forEach((row) => row.remove());
  }

  if (durationControlSupported) {
    shadow.querySelector(".resolution-diagnostics")?.addEventListener("click", async () => {
      const read = (name) => {
        try { return JSON.parse(document.documentElement?.getAttribute(name) || "null"); }
        catch { return null; }
      };
      const report = {
        resolution: read(RESOLUTION_STATUS_ATTRIBUTE),
        bridge: read(RESOLUTION_BRIDGE_ATTRIBUTE),
        mediaCapture: read(MEDIA_CAPTURE_ATTRIBUTE)
      };
      const text = JSON.stringify(report);
      try { await navigator.clipboard.writeText(text); } catch { /* 剪贴板不可用时仍显示摘要。 */ }
      if (toolState) toolState.textContent = "诊断已复制";
    });
    shadow.querySelector(".media-retry")?.addEventListener("click", () => {
      document.documentElement?.setAttribute("data-watermark-free-media-force", "1");
      document.dispatchEvent(new Event(MEDIA_RETRY_EVENT));
      if (toolState) toolState.textContent = "正在按已捕获入口重试提取";
    });
    document.addEventListener(RESOLUTION_STATUS_EVENT, syncResolutionControl);
    syncResolutionControl();
  }

  function selectedResolution() {
    try {
      const value = localStorage.getItem(RESOLUTION_KEY) || "auto";
      return ["720p", "1080p"].includes(value) ? value : "auto";
    } catch {
      return "auto";
    }
  }

  function syncResolutionControl() {
    let status = null;
    try { status = JSON.parse(document.documentElement?.getAttribute(RESOLUTION_STATUS_ATTRIBUTE) || "null"); }
    catch { /* 状态还没写完。 */ }
    const sent = String(status?.lastRequest?.resolution || "");
    if (!toolState) return;
    if (sent) toolState.textContent = `最近请求参数：${sent.toUpperCase()}`;
  }

  if (sniffControlSupported) {
    chrome.runtime.sendMessage({ type: "GET_AUTO_DEBUGGER_SNIFF" }, (response) => {
      if (!chrome.runtime.lastError && response?.ok && sniffAuto) {
        sniffAuto.checked = !!response.enabled;
        syncSniffAutoLabel();
      }
    });
    sniffDeepToggle?.addEventListener("change", () => {
      if (!sniffDeepToggle.checked) {
        syncSniffDeepLabel();
        return;
      }
      sniffDeepToggle.disabled = true;
      chrome.runtime.sendMessage({ type: "ENABLE_DEBUGGER_SNIFF" }, (response) => {
        sniffDeepToggle.disabled = false;
        if (!response?.ok) {
          sniffDeepToggle.checked = false;
        }
        syncSniffDeepLabel();
      });
    });
    sniffAuto?.addEventListener("change", () => {
      chrome.runtime.sendMessage({
        type: "SET_AUTO_DEBUGGER_SNIFF",
        enabled: !!sniffAuto.checked
      });
      syncSniffAutoLabel();
    });
    syncSniffDeepLabel();
    syncSniffAutoLabel();
  }

  function readPromptGuardInjectState() {
    try {
      return localStorage.getItem(PROMPT_GUARD_INJECT_KEY) === "1";
    } catch {
      return false;
    }
  }

  function setPromptGuardInject(enabled, persist = true) {
    const on = Boolean(enabled);
    if (promptGuardToggle) promptGuardToggle.checked = on;
    if (promptGuardLabel) promptGuardLabel.classList.toggle("on", on);
    if (persist) {
      try {
        localStorage.setItem(PROMPT_GUARD_INJECT_KEY, on ? "1" : "0");
      } catch {
        // ignore
      }
    }
  }

  if (durationControlSupported && promptGuardToggle) {
    setPromptGuardInject(readPromptGuardInjectState(), false);
    promptGuardToggle.addEventListener("change", () => {
      setPromptGuardInject(!!promptGuardToggle.checked);
    });
  }

  function readTimelineRewriteState() {
    try {
      return localStorage.getItem(TIMELINE_REWRITE_KEY) === "1";
    } catch {
      return false;
    }
  }

  function setTimelineRewrite(enabled, persist = true) {
    const on = Boolean(enabled);
    if (timelineRewriteToggle) timelineRewriteToggle.checked = on;
    if (timelineRewriteLabel) timelineRewriteLabel.classList.toggle("on", on);
    if (persist) {
      try {
        localStorage.setItem(TIMELINE_REWRITE_KEY, on ? "1" : "0");
      } catch {
        // ignore
      }
    }
  }

  if (durationControlSupported && timelineRewriteToggle) {
    setTimelineRewrite(readTimelineRewriteState(), false);
    timelineRewriteToggle.addEventListener("change", () => {
      setTimelineRewrite(!!timelineRewriteToggle.checked);
    });
  }

  function syncSniffDeepLabel() {
    if (!sniffDeepLabel || !sniffDeepToggle) return;
    sniffDeepLabel.classList.toggle("on", sniffDeepToggle.checked);
  }

  function syncSniffAutoLabel() {
    if (!sniffAutoLabel || !sniffAuto) return;
    sniffAutoLabel.classList.toggle("on", sniffAuto.checked);
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message) {
      return;
    }

    if (message.type === "PANEL_PING") {
      sendResponse({ ready: true });
      return;
    }

    if (message.type === "MEDIA_STATUS" && typeof message.text === "string") {
      resetForSource(message.sourceKey);
      if (!items.size) {
        statusText = message.text;
      }
      render();
      return;
    }

    if (message.type === "MEDIA_FOUND" && Array.isArray(message.items)) {
      resetForSource(message.sourceKey);
      rememberFallbackUrls(message.fallbackApis);
      addItems(message.items);
      statusText = items.size ? "" : "未提取到资源";
      render();
      return;
    }

    if (message.type === "MEDIA_RESET") {
      currentSourceKey = typeof message.sourceKey === "string" ? message.sourceKey : "";
      items.clear();
      statusText = "";
      render();
      return;
    }

  });

  function consumeFallbackApis() {
    if (listPanel?.getAttribute("data-open") !== "1") return;
    const raw = document.documentElement?.getAttribute(MEDIA_APIS_ATTRIBUTE) || "";
    if (!raw) return;
    document.documentElement?.removeAttribute(MEDIA_APIS_ATTRIBUTE);
    try {
      const urls = JSON.parse(raw);
      if (!Array.isArray(urls) || !urls.length) return;
      rememberFallbackUrls(urls);
      statusText = "正在解析无水印资源…";
      render();
      const requestStartedAt = Date.now();
      const force = document.documentElement?.getAttribute("data-watermark-free-media-force") === "1";
      if (force) document.documentElement?.removeAttribute("data-watermark-free-media-force");
      chrome.runtime.sendMessage({
        type: "RESOLVE_FALLBACK_APIS",
        urls: lastFallbackUrls,
        resolution: selectedResolution(),
        force
      }, (response) => {
        if (chrome.runtime.lastError) {
          if (!items.size) {
            statusText = "后台解析未完成，请切换会话后重试";
            render();
          }
          return;
        }
        if (response?.ok === false && !items.size) {
          statusText = "无水印资源解析失败";
          render();
        }
      });
      setTimeout(() => {
        if (!items.size && statusText === "正在解析无水印资源…" && Date.now() - requestStartedAt >= 12000) {
          statusText = "本次未解析到资源，请切换会话后重试";
          render();
        }
      }, 12000);
    } catch {
      // Ignore malformed cross-world payloads.
    }
  }

  document.addEventListener(MEDIA_APIS_EVENT, consumeFallbackApis);
  consumeFallbackApis();
  document.addEventListener(QIANWEN_MEDIA_EVENT, () => consumePageMedia(QIANWEN_MEDIA_ATTRIBUTE));
  document.addEventListener(XYQ_MEDIA_EVENT, () => consumePageMedia(XYQ_MEDIA_ATTRIBUTE));
  consumePageMedia(QIANWEN_MEDIA_ATTRIBUTE);
  consumePageMedia(XYQ_MEDIA_ATTRIBUTE);

  function requestPageMediaScan() {
    document.dispatchEvent(new Event("dola-qianwen-media-request"));
    document.dispatchEvent(new Event("dola-xyq-media-request"));
  }

  function consumePageMedia(attributeName) {
    const raw = document.documentElement?.getAttribute(attributeName) || "";
    if (!raw) return;
    document.documentElement?.removeAttribute(attributeName);
    try {
      const snapshot = JSON.parse(raw);
      if (!snapshot || !Array.isArray(snapshot.items)) return;
      resetForSource(typeof snapshot.sourceKey === "string" ? snapshot.sourceKey : "");
      if (snapshot.items.length) {
        addItems(snapshot.items);
        statusText = "";
      }
      render();
    } catch {
      // 页面快照不是 JSON 时忽略。
    }
  }

  refreshParseButton?.addEventListener("click", (event) => {
    event.stopPropagation();
    closeChannelPop();
    refreshParsedVideos();
  });

  copyVideoButton?.addEventListener("click", (event) => {
    event.stopPropagation();
    if (channelPop.classList.contains("open")) {
      closeChannelPop();
      return;
    }
    renderChannelPop();
    channelPop.classList.add("open");
  });

  shadow.addEventListener("pointerdown", (event) => {
    if (!event.target.closest(".copy-wrap, .channel-pop")) closeChannelPop();
  });

  function rememberFallbackUrls(urls) {
    if (!Array.isArray(urls)) return;
    const next = urls.filter((url) => typeof url === "string" && url);
    if (next.length) lastFallbackUrls = next;
  }

  function closeChannelPop() {
    channelPop?.classList.remove("open");
  }

  function collectChannelUrls() {
    for (const item of items.values()) {
      if (item.type !== "video") continue;
      const list = item.channels && item.channels.length ? item.channels : [item.url];
      return list.filter((url) => isHttpUrl(url)).slice(0, 2);
    }
    return [];
  }

  function renderChannelPop() {
    if (!channelPop) return;
    channelPop.textContent = "";
    const urls = collectChannelUrls();
    for (let index = 0; index < 2; index += 1) {
      const url = urls[index] || "";
      const row = document.createElement("div");
      row.className = "channel-row";
      const name = document.createElement("span");
      name.className = "channel-name";
      name.textContent = `渠道${index + 1}`;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "channel-copy";
      button.textContent = url ? "复制" : "暂无";
      button.disabled = !url;
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        if (url) copyChannelAndOpen(url, button);
      });
      row.append(name, button);
      channelPop.appendChild(row);
    }
  }

  function copyChannelAndOpen(url, button) {
    const copied = copyText(url);
    button.textContent = copied ? "已复制" : "复制失败";
    chrome.runtime.sendMessage({ type: "OPEN_MEDIA_URL", url });
    setTimeout(() => {
      if (button.isConnected) button.textContent = "复制";
    }, 1200);
  }

  function copyText(url) {
    const area = document.createElement("textarea");
    area.value = url;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.left = "-9999px";
    document.documentElement.appendChild(area);
    area.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch {
      ok = false;
    }
    area.remove();
    if (!ok && navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).catch(() => {});
      return true;
    }
    return ok;
  }

  function refreshParsedVideos() {
    for (const [key, item] of items) {
      if (item.type === "video") items.delete(key);
    }
    statusText = "正在刷新解析…";
    render();
    if (refreshParseButton) refreshParseButton.disabled = true;
    chrome.runtime.sendMessage({
      type: "REFRESH_PARSED_VIDEOS",
      urls: lastFallbackUrls,
      resolution: selectedResolution()
    }, (response) => {
      if (refreshParseButton) refreshParseButton.disabled = false;
      if (chrome.runtime.lastError || response?.ok === false) {
        statusText = "刷新解析失败";
        render();
      }
    });
  }

  function resetForSource(sourceKey) {
    if (typeof sourceKey !== "string" || !sourceKey) {
      return;
    }

    if (sourceKey !== currentSourceKey) {
      currentSourceKey = sourceKey;
      items.clear();
      statusText = "";
    }
  }

  function duration30Enabled() {
    try {
      return localStorage.getItem(DURATION_KEY) === "1";
    } catch {
      return false;
    }
  }

  function setDuration30Enabled(enabled) {
    try {
      localStorage.setItem(DURATION_KEY, enabled ? "1" : "0");
      localStorage.setItem(TARGET_DURATION_KEY, enabled ? "30" : "15");
    } catch {
      return;
    }
    document.documentElement?.setAttribute(DURATION_STATE_ATTRIBUTE, enabled ? "1" : "0");
    document.dispatchEvent(new Event(DURATION_STATE_EVENT));
    syncDurationControl();
  }

  function syncDurationControl() {
    let selected = duration30Enabled() ? 30 : 15;
    try {
      const explicit = Number(localStorage.getItem(TARGET_DURATION_KEY));
      if (explicit === 15 || explicit === 30) selected = explicit;
    } catch {
      // Keep the legacy switch value.
    }
    const enabled = selected === 30;
    durationToggle.checked = enabled;
    if (durationLabel) {
      durationLabel.textContent = "SD2.5+30秒";
      durationLabel.classList.toggle("on", enabled);
      durationLabel.title = enabled ? `2.5 · ${selected}s 已开启` : `当前 ${selected}s，开启为 2.5 · 30s`;
    }
    const durSwitch = durationToggle.closest(".switch");
    if (durSwitch) {
      durSwitch.title = enabled ? `Seedance 2.5 · ${selected}s` : "开启 Seedance 2.5 · 30s";
    }
  }

  function createMediaThumb(item) {
    if (item.type === "image") {
      const img = document.createElement("img");
      img.className = "media-thumb";
      img.src = item.url;
      img.alt = "";
      img.loading = "lazy";
      img.decoding = "async";
      img.referrerPolicy = "no-referrer";
      img.addEventListener("error", () => {
        img.replaceWith(createThumbFallback("图片"));
      }, { once: true });
      return img;
    }

    const video = document.createElement("video");
    video.className = "media-thumb";
    video.src = item.url;
    video.muted = true;
    video.playsInline = true;
    video.preload = "metadata";
    video.setAttribute("playsinline", "");
    video.setAttribute("webkit-playsinline", "");
    video.addEventListener("loadeddata", () => {
      try {
        if (video.currentTime < 0.01) video.currentTime = 0.05;
      } catch {
        // ignore seek errors
      }
    }, { once: true });
    video.addEventListener("error", () => {
      video.replaceWith(createThumbFallback("视频"));
    }, { once: true });
    return video;
  }

  function createThumbFallback(kind) {
    const box = document.createElement("div");
    box.className = "media-thumb-fallback";
    box.textContent = kind;
    box.title = kind;
    return box;
  }

  function render() {
    if (!listPanel || listPanel.getAttribute("data-open") !== "1") {
      return;
    }
    list.textContent = "";

    if (!items.size) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = statusText || "等待捕获资源…";
      list.appendChild(empty);
      return;
    }

    Array.from(items.values()).forEach((item, index) => {
      const row = document.createElement("div");
      row.className = "item";

      const thumb = createMediaThumb(item);
      thumb.title = item.url;

      const label = document.createElement("div");
      label.className = "label";
      label.title = item.url;

      const indexText = document.createElement("span");
      indexText.className = "index";
      indexText.textContent = String(index + 1);

      label.append(indexText);
      if (item.spec) {
        const spec = document.createElement("span");
        spec.className = "spec";
        spec.textContent = item.spec;
        label.append(spec);
      }

      const button = document.createElement("button");
      button.type = "button";
      button.className = "dl";
      button.textContent = "下载";
      button.addEventListener("click", () => {
        chrome.runtime.sendMessage({ type: "DOWNLOAD_MEDIA", url: item.url, mediaType: item.type });
      });

      row.append(thumb, label, button);
      list.appendChild(row);
    });
  }

  function addItems(nextItems) {
    for (const item of nextItems) {
      if (!item || typeof item.url !== "string" || !isHttpUrl(item.url)) {
        continue;
      }
      const type = item.type === "image" ? "image" : "video";
      const fingerprint = mediaFingerprint(item.url);
      const channels = Array.isArray(item.channels)
        ? item.channels.filter((url) => typeof url === "string" && isHttpUrl(url)).slice(0, 2)
        : [];
      items.set(`${type}:${fingerprint}`, {
        type,
        url: item.url,
        channels,
        spec: typeof item.spec === "string" ? item.spec : ""
      });
    }
  }

  function isHttpUrl(url) {
    return /^https?:\/\//i.test(url);
  }

  function mediaFingerprint(rawUrl) {
    try {
      const url = new URL(rawUrl);
      url.hash = "";
      for (const key of Array.from(url.searchParams.keys())) {
        if (isVolatileMediaQueryKey(key)) {
          url.searchParams.delete(key);
        }
      }
      url.searchParams.sort();
      return url.toString();
    } catch {
      return String(rawUrl || "").split("#", 1)[0];
    }
  }

  function isVolatileMediaQueryKey(key) {
    const normalized = String(key || "").toLowerCase();
    return /^(?:x-amz-|x-oss-|x-cos-|x-qn-)/.test(normalized) ||
      /^(?:sign|signature|token|access_token|auth|auth_key|authorization|expires?|expire_at|timestamp|ts|wssecret|wstime|policy|key-pair-id)$/.test(normalized);
  }

  function forceHostVisible() {
    const properties = {
      all: "initial",
      display: "block",
      position: "fixed",
      width: "auto",
      height: "auto",
      visibility: "visible",
      opacity: "1",
      "pointer-events": "auto",
      "z-index": "2147483647",
      top: `${PANEL_TOP_PX}px`,
      left: "auto",
      bottom: "auto",
      right: `${PANEL_RIGHT_GAP_PX}px`
    };
    for (const [name, value] of Object.entries(properties)) {
      host.style.setProperty(name, value, "important");
    }
  }

  function mountHost() {
    if (!host.isConnected && document.documentElement) {
      document.documentElement.appendChild(host);
    }
  }

  // ===== 参考上传：把本地音频/视频/照片直接喂给 Dola 视频生成输入区 =====
  // 面板（隔离世界）选文件后派发 dola-toolkit-media-upload-request，
  // media-upload-main.js（主世界）走 Dola 官方上传管线，结果写回结果节点。
  const REF_UPLOAD_SUPPORTED = /(^|\.)dola\.com$/i.test(location.hostname);
  const REF_ACCEPT = {
    audio: ".mp3,.wav,audio/mpeg,audio/wav,audio/*",
    video: ".mp4,.mov,video/mp4,video/quicktime,video/*",
    image: ".jpg,.jpeg,.png,.webp,.apng,image/*"
  };
  const REF_BRIDGE_ID = "dola-toolkit-media-upload-bridge";
  const REF_RESULT_ID = "dola-toolkit-media-upload-result";
  const REF_REQUEST_EVENT = "dola-toolkit-media-upload-request";
  const REF_BRIDGE_READY_ATTR = "data-dola-toolkit-media-upload-ready";
  const refPanel = shadow.querySelector(".ref-panel");
  const refState = shadow.querySelector(".ref-state");
  const refButtons = Array.from(shadow.querySelectorAll(".ref-btn"));
  let refBridgeInput = null;
  let refResultNode = null;
  let refChangeBound = false;
  let refPollTimer = 0;

  function refKindOf(file) {
    const type = String(file?.type || "").toLowerCase();
    const name = String(file?.name || "").toLowerCase();
    if (type.startsWith("image/") || /\.(jpe?g|png|webp|apng)$/.test(name)) return "image";
    if (type.startsWith("video/") || /\.(mp4|mov)$/.test(name)) return "video";
    if (type.startsWith("audio/") || /\.(mp3|wav)$/.test(name)) return "audio";
    return "";
  }

  function setRefState(text, tone = "") {
    if (!refState) return;
    refState.textContent = String(text || "");
    refState.classList.toggle("ok", tone === "ok");
    refState.classList.toggle("err", tone === "err");
  }

  function setRefBusy(busy) {
    for (const btn of refButtons) btn.disabled = !!busy;
  }

  function ensureRefNodes() {
    if (!refBridgeInput || !refBridgeInput.isConnected) {
      refBridgeInput = document.getElementById(REF_BRIDGE_ID);
      if (!refBridgeInput) {
        refBridgeInput = document.createElement("input");
        refBridgeInput.type = "file";
        refBridgeInput.id = REF_BRIDGE_ID;
        refBridgeInput.multiple = true;
        refBridgeInput.tabIndex = -1;
        refBridgeInput.setAttribute("aria-hidden", "true");
        refBridgeInput.style.cssText = "position:fixed;left:-9999px;top:0;width:1px;height:1px;opacity:0;";
        (document.body || document.documentElement).appendChild(refBridgeInput);
      }
    }
    if (!refChangeBound) {
      refBridgeInput.addEventListener("change", onRefFilesChosen);
      refChangeBound = true;
    }
    if (!refResultNode || !refResultNode.isConnected) {
      refResultNode = document.getElementById(REF_RESULT_ID);
      if (!refResultNode) {
        refResultNode = document.createElement("div");
        refResultNode.id = REF_RESULT_ID;
        refResultNode.style.display = "none";
        (document.body || document.documentElement).appendChild(refResultNode);
      }
    }
  }

  function stopRefPoll() {
    if (refPollTimer) {
      clearInterval(refPollTimer);
      refPollTimer = 0;
    }
  }

  function startRefPoll() {
    stopRefPoll();
    const startedAt = Date.now();
    refPollTimer = setInterval(() => {
      const node = refResultNode;
      if (!node || !node.isConnected) return;
      if (node.getAttribute("data-ready") !== "1") {
        if (Date.now() - startedAt > 20 * 60 * 1000) {
          stopRefPoll();
          setRefBusy(false);
          setRefState("处理超时：可打开页面控制台查看 __DOLA_TOOLKIT_MEDIA_UPLOAD__", "err");
        }
        return;
      }
      stopRefPoll();
      setRefBusy(false);
      let payload = null;
      try {
        payload = JSON.parse(node.textContent || "null");
      } catch {
        payload = null;
      }
      if (payload && payload.ok) {
        const counts = payload.counts || {};
        setRefState(`已交给 Dola 处理：图 ${counts.image || 0} · 视频 ${counts.video || 0} · 音频 ${counts.audio || 0}`, "ok");
      } else {
        const reason = String(payload?.reason || "上传桥没有返回结果");
        setRefState(`失败：${reason.slice(0, 120)}`, "err");
        try {
          console.warn("[哆啦助手] 参考上传失败：", payload);
        } catch {
          // ignore
        }
      }
    }, 500);
  }

  function onRefFilesChosen() {
    const files = Array.from(refBridgeInput?.files || []);
    if (!files.length) return;
    const names = files.map((file) => file.name).join("、");
    if (files.some((file) => !refKindOf(file))) {
      setRefState(`不支持的格式：${names}（音频 mp3/wav · 视频 mp4/mov · 图片 jpg/png）`, "err");
      return;
    }
    ensureRefNodes();
    refResultNode.removeAttribute("data-ready");
    refResultNode.textContent = "";
    setRefBusy(true);
    setRefState(`已选择 ${names}，正在交给 Dola…`);
    startRefPoll();
    document.dispatchEvent(new Event(REF_REQUEST_EVENT));
  }

  if (refPanel && refButtons.length) {
    if (!REF_UPLOAD_SUPPORTED) {
      refPanel.remove();
    } else {
      refButtons.forEach((btn) => {
        btn.addEventListener("click", () => {
          if (!document.documentElement?.hasAttribute(REF_BRIDGE_READY_ATTR)) {
            setRefState("上传桥未就绪：请刷新本页后再试（扩展需完整加载）", "err");
            return;
          }
          ensureRefNodes();
          const kind = btn.getAttribute("data-ref-kind") || "audio";
          refBridgeInput.accept = REF_ACCEPT[kind] || "";
          refBridgeInput.value = "";
          setRefState("等待选择文件…");
          refBridgeInput.click();
        });
      });
    }
  }

  const hostObserver = new MutationObserver(() => {
    const currentHost = document.getElementById(PANEL_ID);
    if (currentHost && currentHost !== host) {
      hostObserver.disconnect();
      return;
    }
    mountHost();
  });
  hostObserver.observe(document.documentElement, { childList: true });
})();
