(() => {
  const FLAG = "__dolaXyqMediaLoaded";
  if (globalThis[FLAG]) return;
  globalThis[FLAG] = true;

  const PACKAGE_INFO_ENDPOINT = "https://xyq.jianying.com/api/biz/v1/editor/get_package_info";
  const THREAD_ENDPOINT = "https://xyq.jianying.com/api/biz/v1/agent/get_thread";
  const VIDEO_CONTAINER_SELECTOR = ".ag-ui-x-biz-video-part";
  const ATTR = "data-dola-xyq-media";
  const EVENT = "dola-xyq-media";
  const REQUEST_EVENT = "dola-xyq-media-request";
  const THREAD_RETRY_INTERVAL_MS = 3000;

  const processedContainers = new Set();
  const discoveredItemsByUrl = new Map();
  let videoParametersBySource = new Map();
  let scanInProgress = false;
  let scanRequested = false;
  let cachedThreadId = null;
  let lastThreadAttemptId = null;
  let lastThreadAttemptAt = 0;
  let threadRetryTimer = 0;
  let routeGeneration = 0;
  let parameterSourceKey = "";

  function sourceKey() {
    return `xyq:${location.hostname}${location.pathname}${location.search}${location.hash || ""}`;
  }

  let currentRouteKey = sourceKey();

  function isCurrentRoute(key, generation) {
    return key === currentRouteKey && key === sourceKey() && generation === routeGeneration;
  }

  function syncRouteState() {
    const next = sourceKey();
    if (next === currentRouteKey) return false;
    currentRouteKey = next;
    routeGeneration += 1;
    parameterSourceKey = "";
    videoParametersBySource = new Map();
    processedContainers.clear();
    discoveredItemsByUrl.clear();
    cachedThreadId = null;
    lastThreadAttemptId = null;
    lastThreadAttemptAt = 0;
    clearTimeout(threadRetryTimer);
    threadRetryTimer = 0;
    publish();
    return true;
  }

  function httpUrl(value) {
    if (typeof value !== "string" || !value.trim()) return "";
    try {
      const url = new URL(value, location.href);
      return /^https?:$/i.test(url.protocol) ? url.href : "";
    } catch {
      return "";
    }
  }

  function posterUrl(media) {
    if (!media || typeof media !== "object") return "";
    const candidates = [
      media.posterUrl, media.poster_url, media.coverUrl, media.cover_url,
      typeof media.poster === "string" ? media.poster : media.poster?.url,
      typeof media.cover === "string" ? media.cover : media.cover?.url,
      typeof media.thumbnail === "string" ? media.thumbnail : media.thumbnail?.url,
      typeof media.image === "string" ? media.image : media.image?.url
    ];
    for (const candidate of candidates) {
      const url = httpUrl(candidate);
      if (url) return url;
    }
    return "";
  }

  function pagePosterUrl(container, video) {
    const fromVideo = httpUrl(video?.poster || video?.getAttribute?.("poster"));
    if (fromVideo) return fromVideo;
    const image = container?.querySelector("img");
    return httpUrl(image?.currentSrc || image?.src);
  }

  function publish() {
    const root = document.documentElement;
    if (!root) return;
    root.setAttribute(ATTR, JSON.stringify({
      sourceKey: currentRouteKey,
      items: Array.from(discoveredItemsByUrl.values())
    }));
    document.dispatchEvent(new Event(EVENT));
    postHost(Array.from(discoveredItemsByUrl.values()));
  }

  function postHost(items) {
    try {
      const bridge = window.chrome && window.chrome.webview;
      if (bridge && typeof bridge.postMessage === "function") {
        bridge.postMessage({
          type: "dola-page-media",
          items: items.map((item) => ({
            type: item.type,
            url: item.url,
            posterUrl: item.posterUrl || ""
          }))
        });
      }
    } catch {
      // 外挂浏览器没有宿主通道时，解析列表仍可用。
    }
  }

  async function fetchOriginalVideoInfo(draftKey, renderTaskId) {
    try {
      const response = await fetch(PACKAGE_INFO_ENDPOINT, {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          draft_key: draftKey,
          package_id: "",
          render_task_id: renderTaskId,
          entrance_from: "web"
        })
      });
      if (!response.ok) return null;
      const result = await response.json();
      const packageOrigin = result?.data?.package_info?.package_origin;
      const segment = packageOrigin?.segment_materials?.[0];
      const originalVideo = segment?.videos?.[0];
      const url = httpUrl(originalVideo?.url);
      if (!url) return null;
      return {
        url,
        posterUrl: posterUrl(originalVideo) || posterUrl(segment) || posterUrl(packageOrigin)
      };
    } catch {
      return null;
    }
  }

  async function refreshVideoParameters(threadId, key, generation) {
    try {
      const response = await fetch(THREAD_ENDPOINT, {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          scopes: ["run_list.entry_list"],
          thread_id: threadId
        })
      });
      if (!response.ok) return false;
      const result = await response.json();
      const runs = result?.data?.thread?.run_list;
      if (!Array.isArray(runs) || !isCurrentRoute(key, generation)) return false;
      const parametersBySource = new Map();
      for (const run of runs) {
        const entries = run?.entry_list;
        if (!Array.isArray(entries)) continue;
        for (const entry of entries) {
          const content = entry?.artifact?.content;
          if (!Array.isArray(content)) continue;
          for (const contentItem of content) {
            if (!contentItem?.data) continue;
            try {
              const parsedData = JSON.parse(contentItem.data);
              const playUrl = httpUrl(parsedData?.video?.url);
              if (parsedData?.video?.draft_key && playUrl) {
                parametersBySource.set(playUrl, {
                  draftKey: parsedData.video.draft_key,
                  renderTaskId: parsedData.video.render_task_id,
                  posterUrl: posterUrl(parsedData.video)
                });
              }
            } catch {
              // 单条内容不是 JSON 时跳过。
            }
          }
        }
      }
      if (!isCurrentRoute(key, generation)) return false;
      videoParametersBySource = parametersBySource;
      parameterSourceKey = key;
      return parametersBySource.size > 0;
    } catch {
      return false;
    }
  }

  function scheduleThreadRetry(key, generation) {
    clearTimeout(threadRetryTimer);
    threadRetryTimer = setTimeout(() => {
      threadRetryTimer = 0;
      if (isCurrentRoute(key, generation)) scanVideoContainers();
    }, THREAD_RETRY_INTERVAL_MS);
  }

  async function addPanelItem(container, parameters, key, generation) {
    if (processedContainers.has(container)) return true;
    const video = container.querySelector("video");
    if (!video) return false;
    const originalVideo = await fetchOriginalVideoInfo(parameters.draftKey, parameters.renderTaskId);
    if (!originalVideo?.url || !isCurrentRoute(key, generation) || !container.isConnected) return false;
    discoveredItemsByUrl.set(originalVideo.url, {
      type: "video",
      url: originalVideo.url,
      posterUrl: originalVideo.posterUrl || parameters.posterUrl || pagePosterUrl(container, video),
      resourceId: parameters.renderTaskId || parameters.draftKey || ""
    });
    publish();
    processedContainers.add(container);
    return true;
  }

  async function scanVideoContainers() {
    syncRouteState();
    if (scanInProgress) {
      scanRequested = true;
      return;
    }
    scanInProgress = true;
    scanRequested = false;
    const scanKey = currentRouteKey;
    const scanGeneration = routeGeneration;
    try {
      const containers = document.querySelectorAll(VIDEO_CONTAINER_SELECTOR);
      if (!containers.length) return;
      if (parameterSourceKey !== scanKey) videoParametersBySource = new Map();
      if (videoParametersBySource.size === 0) {
        const threadId = location.href.match(/thread_id=([a-f0-9-]+)/)?.[1];
        const now = Date.now();
        const retryAllowed = threadId !== lastThreadAttemptId || now - lastThreadAttemptAt >= THREAD_RETRY_INTERVAL_MS;
        if (threadId && threadId !== cachedThreadId && retryAllowed) {
          lastThreadAttemptId = threadId;
          lastThreadAttemptAt = now;
          const refreshed = await refreshVideoParameters(threadId, scanKey, scanGeneration);
          if (!isCurrentRoute(scanKey, scanGeneration)) return;
          if (refreshed) {
            cachedThreadId = threadId;
            clearTimeout(threadRetryTimer);
            threadRetryTimer = 0;
          } else {
            scheduleThreadRetry(scanKey, scanGeneration);
          }
        } else if (threadId && videoParametersBySource.size === 0) {
          scheduleThreadRetry(scanKey, scanGeneration);
        }
      }
      for (const container of containers) {
        if (!isCurrentRoute(scanKey, scanGeneration)) return;
        const video = container.querySelector("video");
        if (!video) continue;
        const playUrl = httpUrl(video.currentSrc || video.src || video.querySelector("source")?.src);
        if (!playUrl) continue;
        let parameters = videoParametersBySource.get(playUrl) || null;
        if (!parameters) {
          const sourceId = playUrl.match(/[a-f0-9]{32}/i)?.[0];
          if (sourceId) {
            for (const [knownUrl, knownParameters] of videoParametersBySource) {
              if (knownUrl.includes(sourceId)) {
                parameters = knownParameters;
                break;
              }
            }
          }
        }
        if (!parameters) continue;
        const added = await addPanelItem(container, parameters, scanKey, scanGeneration);
        if (!added && isCurrentRoute(scanKey, scanGeneration)) {
          scheduleThreadRetry(scanKey, scanGeneration);
        }
      }
    } finally {
      scanInProgress = false;
      syncRouteState();
      if (scanRequested || !isCurrentRoute(scanKey, scanGeneration)) {
        scanRequested = false;
        setTimeout(scanVideoContainers, 0);
      }
    }
  }

  setInterval(() => {
    if (syncRouteState()) {
      scanVideoContainers();
      setTimeout(scanVideoContainers, 1500);
    }
  }, 500);

  const pageObserver = new MutationObserver(() => {
    scanVideoContainers();
  });
  document.addEventListener(REQUEST_EVENT, () => {
    syncRouteState();
    publish();
    scanVideoContainers();
  });

  function start() {
    if (document.body) {
      pageObserver.observe(document.body, { childList: true, subtree: true });
    }
    setTimeout(scanVideoContainers, 2000);
  }

  if (document.body) start();
  else document.addEventListener("DOMContentLoaded", start, { once: true });
})();
