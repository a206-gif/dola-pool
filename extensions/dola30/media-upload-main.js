(function () {
  "use strict";

  const INSTALL_MARK = "__DOLA_TOOLKIT_MEDIA_UPLOAD_BRIDGE_V3__";
  const REQUEST_EVENT = "dola-toolkit-media-upload-request";
  const READY_ATTRIBUTE = "data-dola-toolkit-media-upload-ready";
  const RESULT_SELECTOR = "#dola-toolkit-media-upload-result";

  if (globalThis[INSTALL_MARK]) return;
  globalThis[INSTALL_MARK] = true;

  function normalizeUploadUrl(raw) {
    const value = String(raw || '');
    const match = value.match(/^(?:https?:\/\/www\.dola\.com\/chat\/)?(vod-[a-z0-9.-]+\.bytevcloudapi\.com)(\/?\?.*)$/i);
    return match ? `https://${match[1]}${match[2]}` : value;
  }

  function installUploadUrlNormalizer() {
    if (globalThis.__DOLA_TOOLKIT_UPLOAD_URL_NORMALIZER__) return;
    globalThis.__DOLA_TOOLKIT_UPLOAD_URL_NORMALIZER__ = true;
    const originalFetch = window.fetch;
    if (typeof originalFetch === 'function') {
      window.fetch = function normalizedUploadFetch(input, init) {
        const originalUrl = typeof input === 'string' ? input : (input?.url || String(input || ''));
        const normalizedUrl = normalizeUploadUrl(originalUrl);
        if (normalizedUrl !== originalUrl) {
          try {
            input = input instanceof Request ? new Request(normalizedUrl, input) : normalizedUrl;
          } catch (_) {
            input = normalizedUrl;
          }
        }
        return originalFetch.call(this, input, init);
      };
    }
    const originalOpen = XMLHttpRequest.prototype.open;
    if (typeof originalOpen === 'function') {
      XMLHttpRequest.prototype.open = function normalizedUploadOpen(method, url) {
        const args = Array.from(arguments);
        args[1] = normalizeUploadUrl(url);
        return originalOpen.apply(this, args);
      };
    }
  }

  installUploadUrlNormalizer();

  async function uploadReferenceMedia({ bridgeSelector }) {
  const VIDEO_SKILL = 17;
  const ATTACHMENT_BLOCK = 10052;
  const ACCEPTED_TYPES = ['image', 'video', 'audio'];
  const ACCEPTED_EXTENSIONS = ['jpg', 'jpeg', 'png', 'webp', 'apng', 'mp4', 'mov', 'mp3', 'wav'];
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  let activeRuntime = null;
  let activeChatId = '';

  function safeDebugJson(value) {
    const seen = new WeakSet();
    try {
      return JSON.stringify(value, (key, child) => {
        if (child instanceof File) return { name: child.name, size: child.size, type: child.type };
        if (child instanceof Blob) return { size: child.size, type: child.type };
        if (child && typeof child === 'object') {
          if (seen.has(child)) return '[Circular]';
          seen.add(child);
        }
        if (typeof child === 'function') return `[Function ${child.name || 'anonymous'}]`;
        return child;
      }).slice(0, 12000);
    } catch (error) {
      return String(error?.message || error);
    }
  }

  function materialKind(file) {
    const type = String(file?.type || '').toLowerCase();
    const name = String(file?.name || '').toLowerCase();
    if (type.startsWith('image/') || /\.(jpe?g|png|webp|apng)$/.test(name)) return 'image';
    if (type.startsWith('video/') || /\.(mp4|mov)$/.test(name)) return 'video';
    if (type.startsWith('audio/') || /\.(mp3|wav)$/.test(name)) return 'audio';
    return '';
  }

  function attachmentKind(attachment) {
    const direct = String(attachment?.type || '').toLowerCase();
    if (ACCEPTED_TYPES.includes(direct)) return direct;
    const numeric = Number(attachment?.type);
    if (numeric === 1) return 'image';
    if (numeric === 2) {
      const mediaType = Number(attachment?.media?.media_type ?? attachment?.mediaType);
      if (mediaType === 1) return 'audio';
      if (mediaType === 2) return 'video';
    }
    const mime = String(attachment?.mimeType || attachment?.file?.type || '').toLowerCase();
    if (mime.startsWith('image/')) return 'image';
    if (mime.startsWith('video/')) return 'video';
    if (mime.startsWith('audio/')) return 'audio';
    return '';
  }

  function getRuntime() {
    if (typeof window.__DOLA_TOOLKIT_WEBPACK_REQUIRE__ === 'function') {
      return window.__DOLA_TOOLKIT_WEBPACK_REQUIRE__;
    }
    const chunks = window.__LOADABLE_LOADED_CHUNKS__;
    if (!Array.isArray(chunks)) return null;
    try {
      chunks.push([[`dola-toolkit-media-${Date.now()}`], {}, runtime => {
        window.__DOLA_TOOLKIT_WEBPACK_REQUIRE__ = runtime;
      }]);
    } catch (_) {}
    return window.__DOLA_TOOLKIT_WEBPACK_REQUIRE__ || null;
  }

  function getStoreApi(runtime) {
    if (typeof runtime !== 'function' || !runtime.m) return null;
    // 新版 store runtime：qBy.Bc(name) 读取 state，qBy.QY(name) 读取 store。
    try {
      const module = runtime('qBy');
      if (typeof module?.Bc === 'function') {
        return { GX: module.Bc, Wp: module.QY };
      }
    } catch (_) {}
    for (const [rawId, factory] of Object.entries(runtime.m)) {
      const source = String(factory || '');
      if (!/GX:function|attachmentsStore|attachmentUploadStore/.test(source)) continue;
      try {
        const module = runtime(rawId);
        const getState = module?.GX || module?.Bc;
        const getStore = module?.Wp || module?.QY;
        if (typeof getState !== 'function') continue;
        const attachments = getState('attachmentsStore');
        const uploads = getState('attachmentUploadStore');
        if (attachments?.attachmentsMap && uploads?.preprocessTaskMap) {
          return { ...module, GX: getState, Wp: getStore };
        }
      } catch (_) {}
    }
    return null;
  }

  function getStoreState(runtime, name) {
    const api = getStoreApi(runtime);
    if (!api) return null;
    try { return api.GX(name) || null; } catch (_) {}
    try { return api.Wp(name)?.getState?.() || null; } catch (_) { return null; }
  }

  function getServiceApi(runtime) {
    if (typeof runtime !== 'function' || !runtime.m) return null;
    // 新版 service container 在 nR1.K。
    try {
      const module = runtime('nR1');
      if (typeof module?.K === 'function') return { ...module, D: module.K };
    } catch (_) {}
    for (const [rawId, factory] of Object.entries(runtime.m)) {
      const source = String(factory || '');
      if (!/serviceContainer|StoreService/.test(source)) continue;
      try {
        const module = runtime(rawId);
        const getService = module?.D || module?.K;
        if (typeof getService === 'function') return { ...module, D: getService };
      } catch (_) {}
    }
    for (const id of [431565, 645906]) {
      try {
        const module = runtime(id);
        if (typeof module?.D === 'function') return module;
      } catch (_) {}
    }
    return null;
  }

  function getMediaConfig(runtime) {
    if (typeof runtime !== 'function' || !runtime.m) return null;
    for (const [rawId, factory] of Object.entries(runtime.m)) {
      const source = String(factory || '');
      if (!/tV:function/.test(source) || !/Ew:function/.test(source) || !/eq:function/.test(source)) continue;
      try {
        const module = runtime(rawId);
        if (module?.tV && typeof module.Ew === 'string' && typeof module.eq === 'string') return module;
      } catch (_) {}
    }
    for (const id of [114816, 607159]) {
      try {
        const module = runtime(id);
        if (module?.tV && typeof module.Ew === 'string' && typeof module.eq === 'string') return module;
      } catch (_) {}
    }
    return null;
  }

  function findUploadRunner(runtime) {
    if (typeof runtime !== 'function' || !runtime.m) return null;
    // Dola 2026-10 的 async-shared-upload-pipeline 使用字符串 module id
    // `IK`，并改成 named export `runConfiguredUploadPipeline`。旧实现把所有
    // module id 强转 Number，`IK` 会变成 NaN，因此永远扫描不到音频管线。
    for (const rawId of ['IK']) {
      try {
        const module = runtime(rawId);
        if (typeof module?.runConfiguredUploadPipeline === 'function') {
          return { id: rawId, run: module.runConfiguredUploadPipeline };
        }
      } catch (_) {}
    }
    for (const [rawId, factory] of Object.entries(runtime.m)) {
      const source = String(factory || '');
      if (!/inputUploadConfig/.test(source) || !/targetAttachmentChannel/.test(source) ||
          !/uploadPipelineConfig/.test(source)) continue;
      try {
        const module = runtime(rawId);
        const run = module?.runConfiguredUploadPipeline || module?.Z;
        if (typeof run === 'function') return { id: rawId, run };
      } catch (_) {}
    }
    return null;
  }

  function staticAssetBase() {
    const urls = [
      ...performance.getEntriesByType('resource').map(entry => String(entry.name || '')),
      ...Array.from(document.querySelectorAll('script[src]')).map(node => String(node.src || ''))
    ];
    const url = urls.find(value => /\/static\/js\/.*\.js(?:[?#]|$)/i.test(value));
    return url ? url.slice(0, url.indexOf('/static/js/')) : '';
  }

  function loadScript(url) {
    if (!url) return Promise.resolve(false);
    return new Promise(resolve => {
      const script = document.createElement('script');
      let done = false;
      const finish = ok => { if (!done) { done = true; resolve(ok); } };
      script.onload = () => finish(true);
      script.onerror = () => finish(false);
      script.src = url;
      script.async = true;
      (document.head || document.documentElement).appendChild(script);
      setTimeout(() => finish(false), 15000);
    });
  }

  async function resolveUploadRunner(runtime) {
    let runner = findUploadRunner(runtime);
    if (runner) return runner;
    const routeAssets = window._MODERNJS_ROUTE_MANIFEST?.routeAssets || {};
    const entries = Object.entries(routeAssets)
      .filter(([name, value]) => /shared-upload-pipeline/i.test(name) ||
        (Array.isArray(value?.assets) && value.assets.some(asset => /shared-upload-pipeline/i.test(asset))))
      .map(([, value]) => value);
    // 当前官网 manifest 只给 async-shared-upload-pipeline.chunkIds=['3rw']，
    // 没有 assets 字段。直接走 webpack chunk loader，不能再依赖静态文件名。
    if (!entries.length) entries.push({ chunkIds: ['3rw'], assets: [] });
    for (const entry of entries) {
      for (const rawChunkId of entry.chunkIds || []) {
        if (typeof runtime.e !== 'function') continue;
        try { await runtime.e(rawChunkId); } catch (_) {}
        runner = findUploadRunner(runtime);
        if (runner) return runner;
      }
    }
    const base = staticAssetBase();
    for (const entry of entries) {
      for (const asset of entry.assets || []) {
        if (!/shared-upload-pipeline.*\.js(?:[?#]|$)/i.test(asset)) continue;
        await loadScript(`${base}/${asset}`);
        runner = findUploadRunner(runtime);
        if (runner) return runner;
      }
    }
    return null;
  }

  function collectInTree(root, selector, out, seen) {
    if (!root || !root.querySelectorAll || seen.has(root)) return;
    seen.add(root);
    let nodes = [];
    try { nodes = root.querySelectorAll(selector); } catch (_) { return; }
    for (const node of nodes) out.push(node);
    let all = [];
    try { all = root.querySelectorAll('*'); } catch (_) { return; }
    for (const el of all) {
      if (el.shadowRoot) collectInTree(el.shadowRoot, selector, out, seen);
    }
  }

  function pageNodes(selector) {
    const out = [];
    collectInTree(document, selector, out, new Set());
    return out;
  }

  function composerPropsOf(input) {
    const key = Object.keys(input).find(name => name.startsWith('__reactFiber$') || name.startsWith('__reactInternalInstance$'));
    let fiber = key ? input[key] : null;
    for (let i = 0; fiber && i < 40; i += 1, fiber = fiber.return) {
      const props = fiber.memoizedProps || fiber.pendingProps || {};
      // 未进入技能时 skill 为空，但 chatId 和上传配置已经在。旧条件把这种输入框当成不存在。
      if (props.chatId && props.inputUploadConfig) return props;
    }
    return null;
  }

  function findInputContext() {
    // 文件框可能在 shadow 里，外层 querySelector 看不到。
    const inputs = pageNodes('input[type="file"]')
      .filter(node => !node.matches(bridgeSelector));
    let fallback = null;
    for (const input of inputs) {
      const props = composerPropsOf(input);
      if (!props) continue;
      if (Number(props.skill?.skill_type) === VIDEO_SKILL) return { input, props };
      if (!fallback) fallback = { input, props };
    }
    return fallback;
  }

  function findInputProps() {
    return findInputContext()?.props || null;
  }

  function enableAllReferenceTypes(pipelineConfig) {
    if (!pipelineConfig || typeof pipelineConfig !== 'object') return pipelineConfig;
    pipelineConfig.acceptedAttachmentTypes = ACCEPTED_TYPES;
    pipelineConfig.acceptedFileExtensions = ACCEPTED_EXTENSIONS;
    pipelineConfig.maxCount = 50;
    pipelineConfig.addAttachmentType = ({ file } = {}) => materialKind(file) || 'file';
    pipelineConfig.isReusableAttachment = attachment => ACCEPTED_TYPES.includes(attachment?.type);
    return pipelineConfig;
  }

  async function installNativeUploadFallback(inputUploadConfig, initialConfig) {
    const originalFactory = inputUploadConfig?.uploadPipelineConfig;
    if (typeof originalFactory !== 'function') return;
    if (originalFactory.__DOLA_TOOLKIT_ALL_MEDIA__) return;
    const wrapped = async function () {
      let current = await originalFactory.apply(this, arguments);
      if (!current) current = initialConfig || {};
      return enableAllReferenceTypes(current);
    };
    wrapped.__DOLA_TOOLKIT_ALL_MEDIA__ = true;
    try { inputUploadConfig.uploadPipelineConfig = wrapped; } catch (_) {}
  }

  async function dispatchNativeUpload(file) {
    const context = findInputContext();
    const input = context?.input;
    if (!input) throw new Error('Dola 官网文件入口尚未挂载');
    const transfer = new DataTransfer();
    transfer.items.add(file);
    input.accept = '.jpg,.jpeg,.png,.webp,.apng,.mp4,.mov,.mp3,.wav,image/*,video/*,audio/*';
    input.multiple = true;
    input.value = '';
    input.files = transfer.files;
    input.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
    input.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
  }

  function describeInputContexts() {
    const inputs = pageNodes('input[type="file"]')
      .filter(node => !node.matches(bridgeSelector))
      .slice(0, 12)
      .map((input, inputIndex) => {
        const fiberKey = Object.keys(input).find(name => name.startsWith('__reactFiber$'));
        let fiber = fiberKey ? input[fiberKey] : null;
        const levels = [];
        for (let depth = 0; fiber && depth < 32; depth += 1, fiber = fiber.return) {
          const props = fiber.memoizedProps || fiber.pendingProps || {};
          const keys = props && typeof props === 'object' ? Object.keys(props).slice(0, 40) : [];
          if (keys.length) levels.push({ depth, keys });
        }
        return {
          inputIndex,
          accept: input.getAttribute('accept') || '',
          multiple: !!input.multiple,
          fiber: !!fiberKey,
          ownKeys: Object.keys(input).filter(key => key.startsWith('__react')).slice(0, 20),
          ancestorReactKeys: (() => {
            const rows = [];
            let node = input.parentElement;
            for (let depth = 1; node && depth <= 8; depth += 1, node = node.parentElement) {
              const keys = Object.keys(node).filter(key => key.startsWith('__react'));
              if (keys.length) rows.push({ depth, tag: node.tagName, keys: keys.slice(0, 20) });
            }
            return rows;
          })(),
          levels,
        };
      });
    const controls = pageNodes('button,[role="button"]')
      .filter(node => {
        const rect = node.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
      })
      .slice(-80)
      .map((node, index) => ({
        index,
        text: String(node.innerText || '').trim().slice(0, 120),
        ariaLabel: node.getAttribute('aria-label') || '',
        title: node.getAttribute('title') || '',
        testId: node.getAttribute('data-testid') || '',
        actionKey: node.getAttribute('data-input-engine-actionbar-control-key') || '',
        className: String(node.className || '').slice(0, 180),
        html: String(node.outerHTML || '').slice(0, 420),
      }));
    return { inputs, controls };
  }

  function findComposerAddButton() {
    return pageNodes('button').find(node => {
      const rect = node.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return false;
      const text = String(node.innerText || '').trim();
      const pathData = Array.from(node.querySelectorAll('svg path'))
        .map(path => path.getAttribute('d') || '').join(' ');
      return !text && /V10\.9951H2|V11H3|M12(?:\.0005)?\s+2\.(?:25|05)/.test(pathData);
    }) || null;
  }

  function readMediaDuration(file, kind) {
    return new Promise(resolve => {
      if (!['audio', 'video'].includes(kind)) return resolve(0);
      const url = URL.createObjectURL(file);
      const media = document.createElement(kind);
      let settled = false;
      const finish = value => {
        if (settled) return;
        settled = true;
        URL.revokeObjectURL(url);
        resolve(Number.isFinite(value) && value > 0 ? value : 0);
      };
      const timer = setTimeout(() => finish(0), 5000);
      media.preload = 'metadata';
      media.onloadedmetadata = () => { clearTimeout(timer); finish(media.duration); };
      media.onerror = () => { clearTimeout(timer); finish(0); };
      media.src = url;
    });
  }

  async function initMediaUploader(runtime, kind) {
    if (!['audio', 'video'].includes(kind)) return;
    const service = await getServiceApi(runtime)?.D?.('attachmentUploadStoreService');
    const config = getMediaConfig(runtime);
    const base = config?.tV;
    const scene = kind === 'audio' ? config?.Ew : config?.eq;
    if (!service?.initUploader || !base || !scene) {
      throw new Error(`${kind === 'audio' ? '音频' : '视频'} uploader 初始化失败`);
    }
    await service.initUploader({
      mediaUploaderOptions: {
        ...base,
        scene,
        resourcePrepareUploadParams: { ...(base.resourcePrepareUploadParams || {}), scene_id: scene }
      }
    });
  }

  function botId(runtime, chatId) {
    try { return runtime(372595)?.S?.(chatId, true) || runtime(29155)?.IO?.(runtime(986344)?.NW?.getState?.()) || ''; }
    catch (_) { return ''; }
  }

  function taskFailed(task) {
    const status = String(task?.status || task?.state || '').toLowerCase();
    return !!(task?.failed || task?.error || ['failed', 'error', 'retry', 'rejected', 'cancelled', 'canceled'].includes(status));
  }

  function taskLoading(task) {
    const status = String(task?.status || task?.state || '').toLowerCase();
    return !!(task?.loading || ['uploading', 'pending', 'loading', 'processing', 'preprocessing'].includes(status));
  }

  function taskFailureDetail(task) {
    if (!task || typeof task !== 'object') return '';
    const source = task.error || task.err || task.reason || task.message ||
      task.uploadResult?.error || task.uploadResult?.message || task.ctx?.error || '';
    if (typeof source === 'string') return source;
    try {
      return JSON.stringify(source, (key, value) => {
        if (key === 'file' || key === 'blob' || key === 'controller') return undefined;
        return value;
      }).slice(0, 2000);
    } catch (_) {
      return String(source || '');
    }
  }

  function attachmentKey(attachment, task) {
    const result = task?.uploadResult || {};
    return String(attachment?.key || attachment?.fileKey || attachment?.media?.key || task?.key || task?.fileKey ||
      result.FileKey || result.file_key || result?.media?.key || '').trim();
  }

  async function waitForUpload(runtime, chatId, beforeKeys, file) {
    const deadline = Date.now() + 10 * 60 * 1000;
    const kind = materialKind(file);
    while (Date.now() < deadline) {
      const attachments = getStoreState(runtime, 'attachmentsStore')?.attachmentsMap?.[chatId]?.[`_${VIDEO_SKILL}`] || [];
      const tasks = getStoreState(runtime, 'attachmentUploadStore')?.preprocessTaskMap || {};
      const attachment = attachments.find(item => {
        if (beforeKeys.has(item?.localKey || '')) return false;
        const name = item?.fileName || item?.file?.name || '';
        return attachmentKind(item) === kind && (!name || name === file.name);
      });
      if (attachment) {
        const task = tasks[attachment.localKey];
        if (taskFailed(task)) {
          const detail = taskFailureDetail(task);
          const state = String(task?.status || task?.state || 'failed');
          throw new Error(`${file.name} 上传失败（${state}）${detail ? `：${detail}` : ''}`);
        }
        const key = attachmentKey(attachment, task);
        if (key && !taskLoading(task)) return { attachment, key };
      }
      await sleep(300);
    }
    throw new Error(`${file.name} 上传超时`);
  }

  function snapshot(runtime, chatId) {
    const attachments = getStoreState(runtime, 'attachmentsStore')?.attachmentsMap?.[chatId]?.[`_${VIDEO_SKILL}`] || [];
    const tasks = getStoreState(runtime, 'attachmentUploadStore')?.preprocessTaskMap || {};
    const counts = { image: 0, video: 0, audio: 0 };
    let pending = 0;
    const keys = [];
    const states = [];
    for (const attachment of attachments) {
      const kind = attachmentKind(attachment);
      if (kind in counts) counts[kind] += 1;
      const task = tasks[attachment?.localKey] || {};
      const result = task.uploadResult || {};
      const meta = result.VideoMeta || {};
      const extra = task?.ctx?.extraParams || {};
      const key = attachmentKey(attachment, task);
      if (ACCEPTED_TYPES.includes(kind) && (!key || taskLoading(task))) pending += 1;
      keys.push(key || undefined);
      states.push({
        type: kind,
        localKey: attachment?.localKey || '',
        fileKey: key || undefined,
        fileName: attachment?.fileName || attachment?.file?.name || meta.FileName || '',
        size: Number(attachment?.size || attachment?.file?.size || meta.Size || 0),
        md5: attachment?.md5 || task?.md5 || meta.Md5,
        mediaDuration: Number(attachment?.mediaDuration || meta.Duration || extra.duration_seconds || 0),
        mediaResourceUri: attachment?.mediaResourceUri || task?.mediaResourceUri,
        mediaCoverUrl: attachment?.mediaCoverUrl || extra.cover_local_url || task?.blobUrl || '',
        mediaCoverWidth: Number(attachment?.mediaCoverWidth || extra.cover_width || meta.Width || 0),
        mediaCoverHeight: Number(attachment?.mediaCoverHeight || extra.cover_height || meta.Height || 0),
        imageWidth: Number(attachment?.imageWidth || result.ImageWidth || 0),
        imageHeight: Number(attachment?.imageHeight || result.ImageHeight || 0),
        url: attachment?.url || task?.blobUrl || '',
        src: attachment?.src || task?.blobUrl || '',
        uploadPercent: Number.isFinite(Number(task?.percent)) ? Number(task.percent) : 100,
        status: taskFailed(task) ? 'Retry' : taskLoading(task) ? 'Uploading' : 'Normal'
      });
    }
    return { attachments, keys, states, counts, pending };
  }

  function attachmentCounts(blocks) {
    const counts = { image: 0, video: 0, audio: 0 };
    for (const block of blocks || []) {
      for (const item of block?.content?.attachment_block?.attachments || []) {
        if (Number(item?.type) === 1) counts.image += 1;
        if (Number(item?.type) === 2 && Number(item?.media?.media_type) === 1) counts.audio += 1;
        if (Number(item?.type) === 2 && Number(item?.media?.media_type) === 2) counts.video += 1;
      }
    }
    return counts;
  }

  function findBlockBuilder(runtime) {
    try { if (typeof runtime(597338)?.S === 'function') return runtime(597338).S; } catch (_) {}
    for (const [rawId, factory] of Object.entries(runtime.m || {})) {
      const source = String(factory || '');
      if (!/attachmentStates/.test(source) || !/attachmentKeys/.test(source) || !/attachment_block/.test(source)) continue;
      try {
        const module = runtime(rawId);
        if (typeof module?.S === 'function') return module.S;
      } catch (_) {}
    }
    return null;
  }

  async function installSubmitPatch(runtime, chatId) {
    const serviceApi = getServiceApi(runtime);
    const chatIMService = await serviceApi?.D?.('chatIMService');
    const messageService = await chatIMService?.getMessageService?.();
    if (!messageService || typeof messageService.sendMessage !== 'function') return false;
    if (messageService.sendMessage.__DOLA_TOOLKIT_MEDIA_PATCHED__) return true;
    const original = messageService.sendMessage;
    const patched = function (request, ...args) {
      const snap = snapshot(runtime, chatId);
      if (!snap.attachments.length || snap.pending) return original.call(this, request, ...args);
      const messages = Array.isArray(request?.messages) ? request.messages : [];
      const nativeBlocks = messages.flatMap(message => message?.content_blocks_v2 || [])
        .filter(block => Number(block?.block_type) === ATTACHMENT_BLOCK);
      const native = attachmentCounts(nativeBlocks);
      if (native.image >= snap.counts.image && native.video >= snap.counts.video && native.audio >= snap.counts.audio) {
        return original.call(this, request, ...args);
      }
      const builder = findBlockBuilder(runtime);
      const generated = builder?.({ attachments: snap.attachments, attachmentKeys: snap.keys, attachmentStates: snap.states });
      if (!generated) throw new Error('音频已上传，但无法建立 Dola 素材引用块');
      const submitted = attachmentCounts([generated]);
      if (submitted.audio < snap.counts.audio || submitted.video < snap.counts.video) {
        throw new Error('音频/视频尚未进入生成请求，请稍后重试');
      }
      const cloned = messages.map(message => ({ ...message, content_blocks_v2: [...(message.content_blocks_v2 || [])] }));
      let index = cloned.findIndex(message => {
        try {
          const ability = typeof message?.ext?.chat_ability === 'string'
            ? JSON.parse(message.ext.chat_ability) : message?.ext?.chat_ability;
          return Number(ability?.ability_type) === VIDEO_SKILL;
        } catch (_) { return false; }
      });
      if (index < 0) index = 0;
      if (!cloned[index]) return original.call(this, request, ...args);
      const blockIndex = cloned[index].content_blocks_v2.findIndex(block => Number(block?.block_type) === ATTACHMENT_BLOCK);
      if (blockIndex >= 0) cloned[index].content_blocks_v2[blockIndex] = generated;
      else cloned[index].content_blocks_v2.unshift(generated);
      window.__DOLA_TOOLKIT_LAST_MEDIA_SUBMIT__ = { patched: true, counts: submitted };
      return original.call(this, { ...request, messages: cloned }, ...args);
    };
    patched.__DOLA_TOOLKIT_MEDIA_PATCHED__ = true;
    if (original.__DOUBAO_DOLA_RESOLUTION_BRIDGE__) {
      patched.__DOUBAO_DOLA_RESOLUTION_BRIDGE__ = true;
    }
    messageService.sendMessage = patched;
    return true;
  }

  try {
    const bridge = document.querySelector(bridgeSelector);
    const files = Array.from(bridge?.files || []);
    if (!files.length) return { ok: false, reason: '没有读到待上传素材' };
    // Dola 是分阶段水合的 SPA：页面外壳、视频模式、React fiber 和
    // upload pipeline 会在慢网络下先后出现。不能在第一次查找失败时
    // 立即返回，否则工作台会误以为素材已处理。
    const hydrationDeadline = Date.now() + 150000;
    let props = findInputProps();
    let addButtonClicked = false;
    while ((!props || Number(props.skill?.skill_type) !== VIDEO_SKILL) && Date.now() < hydrationDeadline) {
      if (!props && !addButtonClicked) {
        const addButton = findComposerAddButton();
        if (addButton) {
          addButton.click();
          addButtonClicked = true;
        }
      }
      await sleep(1000);
      props = findInputProps();
    }
    if (!props) {
      const addButton = findComposerAddButton();
      if (addButton) {
        addButton.click();
        await sleep(500);
      }
      const debugRuntime = getRuntime();
      let knownUploadModule = null;
      try {
        const module = debugRuntime?.(784675);
        knownUploadModule = {
          keys: Object.keys(module || {}),
          configType: typeof module?.videoUploadPipelineConfig,
          configKeys: Object.keys(module?.videoUploadPipelineConfig || {}).slice(0, 80),
        };
      } catch (error) {
        knownUploadModule = { error: String(error?.message || error) };
      }
      const chunkGlobals = Object.keys(window)
        .filter(key => /webpack|chunk|loadable|modern/i.test(key))
        .slice(0, 80)
        .map(key => {
          let value;
          try { value = window[key]; } catch (_) { value = null; }
          return {
            key,
            type: typeof value,
            array: Array.isArray(value),
            length: Array.isArray(value) ? value.length : undefined,
            keys: value && typeof value === 'object' ? Object.keys(value).slice(0, 30) : [],
          };
        });
      return {
        ok: false,
        reason: '未找到 Dola 视频上传上下文',
        detail: JSON.stringify({
          addButtonFound: !!addButton,
          runtime: typeof debugRuntime,
          runtimeKeys: Object.keys(debugRuntime || {}).slice(0, 80),
          runtimeFactoryCount: Object.keys(debugRuntime?.m || {}).length,
          uploadRunner: findUploadRunner(debugRuntime)?.id || null,
          knownUploadModule,
          attachmentChatIds: Object.keys(getStoreState(debugRuntime, 'attachmentsStore')?.attachmentsMap || {}),
          chunkGlobals,
          afterAddClick: describeInputContexts()
        })
      };
    }
    if (Number(props.skill?.skill_type) !== VIDEO_SKILL) return { ok: false, reason: '当前页面尚未进入视频创作模式' };
    let runtime = getRuntime();
    while (!runtime && Date.now() < hydrationDeadline) {
      await sleep(1000);
      runtime = getRuntime();
    }
    if (!runtime) return { ok: false, reason: '未找到 Dola Webpack 运行时' };
    activeRuntime = runtime;
    activeChatId = props.chatId || '';
    const inputUploadConfig = props.inputUploadConfig || {};
    let pipelineConfig = await inputUploadConfig.uploadPipelineConfig?.();
    while (!pipelineConfig && Date.now() < hydrationDeadline) {
      await sleep(1000);
      pipelineConfig = await inputUploadConfig.uploadPipelineConfig?.();
    }
    if (!pipelineConfig) return { ok: false, reason: 'Dola 上传配置尚未加载' };
    enableAllReferenceTypes(pipelineConfig);
    await installNativeUploadFallback(inputUploadConfig, pipelineConfig);
    let runner = await resolveUploadRunner(runtime);
    // 新版页面已经移除了旧 module 特征时，不再傻等完整的 150 秒水合窗口。
    // 给动态 chunk 12 秒加载机会，随后立即转官网 input 兜底。
    const runnerDeadline = Math.min(hydrationDeadline, Date.now() + 12000);
    while (!runner && Date.now() < runnerDeadline) {
      await sleep(1500);
      runner = await resolveUploadRunner(runtime);
    }
    // Dola 会不定期调整 shared-upload-pipeline 的 chunk/module id。模块扫描不到时，
    // 不再丢弃音频和视频：把文件交给官网 React input 的 onChange，让当前版本的
    // Composer 自己调用上传管线；仍然使用 attachmentsStore + fileKey 做最终校验。
    const transport = runner ? 'internal_pipeline' : 'native_react_input';
    if (!runner && !findInputContext()?.input) {
      return { ok: false, reason: '未找到 Dola 官方上传管线，且官网文件入口尚未挂载' };
    }

    for (const file of files) {
      const kind = materialKind(file);
      if (!kind) return { ok: false, reason: `不支持的素材格式：${file.name}` };
      const existing = getStoreState(runtime, 'attachmentsStore')?.attachmentsMap?.[props.chatId]?.[`_${VIDEO_SKILL}`] || [];
      const beforeKeys = new Set(existing.map(item => item?.localKey).filter(Boolean));
      try {
        await initMediaUploader(runtime, kind);
      } catch (error) {
        // 官网的 runConfiguredUploadPipeline / React onChange 会按当前素材类型
        // 初始化 uploader。私有 media config 改名时不能在调用官方管线前先行阻断。
      }
      const duration = await readMediaDuration(file, kind);
      const uploadPipelineConfig = {
        ...pipelineConfig,
        buildAttachmentCtx: ({ file: currentFile, skill, uploadCollectionId }) => {
          const skillType = Number(skill?.skill_type || VIDEO_SKILL);
          return {
            conversationId: props.chatId || '',
            chatType: 'other_default',
            skillType,
            skillId: String(skillType),
            botId: botId(runtime, props.chatId),
            from: '',
            uploadCollectionWay: 'single',
            uploadId: '',
            uploadWay: 'browse_folder',
            uploadFrom: 'video_generation',
            uploadCollectionId,
            uploadType: String(currentFile?.type || ''),
            extraParams: duration > 0 ? { duration_seconds: String(duration) } : {}
          };
        }
      };
      if (runner) {
        await runner.run({
          inputUploadConfig,
          trigger: 'click',
          chatId: props.chatId,
          chatType: props.chatType,
          skill: props.skill,
          reportParams: props.reportParams,
          files: [file],
          inputConfigKey: props.inputConfigKey,
          attachmentChannel: VIDEO_SKILL,
          targetAttachmentChannel: VIDEO_SKILL,
          uploadPipelineConfig
        });
      } else {
        await dispatchNativeUpload(file);
      }
      await waitForUpload(runtime, props.chatId, beforeKeys, file);
    }

    const submitPatched = await installSubmitPatch(runtime, props.chatId);
    const result = snapshot(runtime, props.chatId);
    window.__DOLA_TOOLKIT_MEDIA_UPLOAD__ = { ok: true, chatId: props.chatId, counts: result.counts, submitPatched };
    return {
      ok: true,
      chatId: props.chatId,
      counts: result.counts,
      submitPatched,
      transport,
      files: files.map(file => ({ name: file.name, kind: materialKind(file), size: file.size }))
    };
  } catch (error) {
    let taskDebug = '';
    try {
      const attachments = getStoreState(activeRuntime, 'attachmentsStore')?.attachmentsMap?.[activeChatId]?.[`_${VIDEO_SKILL}`] || [];
      const tasks = getStoreState(activeRuntime, 'attachmentUploadStore')?.preprocessTaskMap || {};
      taskDebug = safeDebugJson({
        attachments: attachments.map(item => ({
          type: attachmentKind(item),
          localKey: item?.localKey,
          fileName: item?.fileName || item?.file?.name,
          keys: Object.keys(item || {})
        })),
        tasks: Object.fromEntries(attachments.map(item => [item?.localKey || '', tasks[item?.localKey] || null]))
      });
    } catch (_) {}
    const detail = `${String(error?.stack || error?.message || error)}${taskDebug ? `\nUPLOAD_DEBUG=${taskDebug}` : ''}`;
    window.__DOLA_TOOLKIT_MEDIA_UPLOAD__ = { ok: false, reason: detail };
    return { ok: false, reason: String(error?.message || error), detail };
  }
  }

  async function handleUploadRequest() {
    const bridge = document.querySelector("#dola-toolkit-media-upload-bridge");
    const resultNode = document.querySelector(RESULT_SELECTOR);
    if (!bridge || !resultNode || bridge.dataset.uploadRunning === "1") return;
    bridge.dataset.uploadRunning = "1";
    resultNode.removeAttribute("data-ready");
    resultNode.textContent = "";
    let result;
    try {
      result = await uploadReferenceMedia({ bridgeSelector: "#dola-toolkit-media-upload-bridge" });
    } catch (error) {
      const detail = String(error?.stack || error?.message || error);
      result = { ok: false, reason: String(error?.message || error), detail };
    } finally {
      delete bridge.dataset.uploadRunning;
    }
    resultNode.textContent = JSON.stringify(result || { ok: false, reason: "上传桥没有返回结果" });
    resultNode.setAttribute("data-ready", "1");
    resultNode.dispatchEvent(new Event("change", { bubbles: true }));
  }

  document.addEventListener(REQUEST_EVENT, handleUploadRequest);
  document.documentElement?.setAttribute(READY_ATTRIBUTE, "3");
})();
