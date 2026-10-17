(() => {
  const pageWindow = typeof unsafeWindow !== "undefined" ? unsafeWindow : window;
  const FLAG = "__dolaQianwenMediaLoaded";
  if (pageWindow[FLAG]) return;
  pageWindow[FLAG] = true;

  const ATTR = "data-dola-qianwen-media";
  const EVENT = "dola-qianwen-media";
  const REQUEST_EVENT = "dola-qianwen-media-request";
  const SESSION_MESSAGES_PATH = "/api/v1/session/msg/list";
  const SHARE_INFO_PATH = "/api/v1/share/info";
  const CHAT_SNAPSHOT_PATH = "/api/v1/chat/snap";

  const originalFetch = pageWindow.fetch;
  let imageCache = [];
  let videoCache = [];
  let routeRevision = 0;
  let routeRefreshTimer = 0;

  function routeKey() {
    const loc = pageWindow.location;
    return `${loc.hostname}${loc.pathname}${loc.search}${loc.hash || ""}`;
  }

  let currentRouteKey = routeKey();
  let currentRouteToken = `${currentRouteKey}#${routeRevision}`;

  function isCurrentRoute(token) {
    return !token || token === currentRouteToken;
  }

  function publish() {
    const root = document.documentElement;
    if (!root) return;
    const items = [
      ...imageCache.map((image) => ({ type: "image", url: image.url })),
      ...videoCache.map((video) => ({ type: "video", url: video.url }))
    ];
    root.setAttribute(ATTR, JSON.stringify({
      sourceKey: `qianwen:${currentRouteKey}`,
      items
    }));
    document.dispatchEvent(new Event(EVENT));
    postHost(items);
  }

  function postHost(items) {
    try {
      const bridge = pageWindow.chrome && pageWindow.chrome.webview;
      if (bridge && typeof bridge.postMessage === "function") {
        bridge.postMessage({ type: "dola-page-media", items });
      }
    } catch {
      // 外挂浏览器没有宿主通道时，解析列表仍可用。
    }
  }

  function detectRouteChange() {
    const next = routeKey();
    if (next === currentRouteKey) return false;
    currentRouteKey = next;
    routeRevision += 1;
    currentRouteToken = `${currentRouteKey}#${routeRevision}`;
    imageCache = [];
    videoCache = [];
    publish();
    clearTimeout(routeRefreshTimer);
    routeRefreshTimer = setTimeout(() => {
      scanPageMedia();
      publish();
    }, 650);
    return true;
  }

  function addImage(image, token) {
    if (!isCurrentRoute(token) || !image?.url) return false;
    if (imageCache.some((item) => item.url === image.url)) return false;
    imageCache.unshift(image);
    return true;
  }

  function addVideo(video, token) {
    if (!isCurrentRoute(token) || !video?.url) return false;
    if (videoCache.some((item) => (video.resourceId && item.resourceId === video.resourceId) || item.url === video.url)) {
      return false;
    }
    videoCache.unshift(video);
    return true;
  }

  function isInsidePanel(element) {
    return Boolean(element.closest?.("#watermark-free-media-panel"));
  }

  function hasExcludedAncestor(element) {
    return Boolean(element.closest?.(
      "[class*=source], [class*=reference], [class*=citation], [class*=avatar], [class*=icon], [class*=logo]"
    ));
  }

  function isLargeEnough(element) {
    const bounds = element.getBoundingClientRect?.();
    return Boolean(bounds && bounds.width >= 140 && bounds.height >= 120);
  }

  function isExcludedMediaUrl(url) {
    return !url || url.startsWith("data:") || url.startsWith("blob:")
      || /avatar|icon|logo|source|favicon|emoji|sprite/i.test(url);
  }

  function scanPageMedia(token = currentRouteToken) {
    if (!isCurrentRoute(token)) return false;
    let changed = false;
    document.querySelectorAll("img").forEach((imageElement) => {
      if (isInsidePanel(imageElement) || hasExcludedAncestor(imageElement) || !isLargeEnough(imageElement)) return;
      const url = imageElement.currentSrc || imageElement.src;
      if (isExcludedMediaUrl(url)) return;
      changed = addImage({ url }, token) || changed;
    });
    document.querySelectorAll("video").forEach((videoElement) => {
      if (isInsidePanel(videoElement) || !isLargeEnough(videoElement)) return;
      const url = videoElement.currentSrc || videoElement.src || videoElement.querySelector("source")?.src;
      if (!url || url.startsWith("blob:")) return;
      changed = addVideo({ url, resourceId: url }, token) || changed;
    });
    if (changed) publish();
    return changed;
  }

  function toArray(value) {
    if (Array.isArray(value)) return value;
    return value ? [value] : [];
  }

  function mediaUrl(media) {
    if (typeof media === "string") return media;
    if (!media || typeof media !== "object") return "";
    return media.url || media.main_url || media.play_url || media.video_url
      || media.download_url || media.source_url || media.src || media.file_url
      || media.resource_url || "";
  }

  function parseResponseMessages(responseMessages, token = currentRouteToken) {
    if (!isCurrentRoute(token) || !Array.isArray(responseMessages)) return false;
    let changed = false;
    for (const responseMessage of responseMessages) {
      if (responseMessage?.mime_type !== "multi_load/iframe") continue;
      const multiLoadEntries = responseMessage?.meta_data?.multi_load;
      if (!Array.isArray(multiLoadEntries)) continue;
      for (const multiLoadEntry of multiLoadEntries) {
        const displayList = multiLoadEntry?.content?.display_list;
        if (!Array.isArray(displayList)) continue;
        for (const displayItem of displayList) {
          for (const image of toArray(displayItem?.image)) {
            const url = mediaUrl(image);
            if (!url) continue;
            changed = addImage({ url }, token) || changed;
          }
          const videoCandidates = [
            ...toArray(displayItem?.video),
            ...toArray(displayItem?.videos),
            ...toArray(displayItem?.video_list),
            ...toArray(displayItem?.media?.video)
          ];
          const itemDescribesVideo = displayItem?.type === "video"
            || displayItem?.media_type === "video"
            || String(displayItem?.mime_type || "").includes("video")
            || displayItem?.video_url || displayItem?.play_url
            || displayItem?.download_url || displayItem?.source_url;
          if (itemDescribesVideo) videoCandidates.push(displayItem);
          for (const video of videoCandidates) {
            const url = mediaUrl(video);
            if (!url) continue;
            changed = addVideo({
              url,
              resourceId: video?.vid || video?.video_id || video?.id || url
            }, token) || changed;
          }
        }
      }
    }
    if (changed) publish();
    return changed;
  }

  function parseRecordList(records, token) {
    if (!Array.isArray(records) || !isCurrentRoute(token)) return;
    for (const record of records) {
      parseResponseMessages(record?.response_messages, token);
    }
  }

  function requestUrl(input) {
    const raw = typeof input === "string" ? input : input?.url || "";
    if (!raw) return null;
    try {
      return new URL(raw, pageWindow.location.href);
    } catch {
      return null;
    }
  }

  function inspectJsonResponse(response, token, selectRecords) {
    try {
      response.clone().json().then((payload) => {
        if (!isCurrentRoute(token)) return;
        parseRecordList(selectRecords(payload), token);
      }).catch(() => {});
    } catch {
      // 响应体不能复制时跳过。
    }
    return response;
  }

  function createSseParser(onEvent) {
    let textBuffer = "";
    let eventName = "";
    let dataLines = [];
    function dispatchFrame() {
      if (eventName || dataLines.length) onEvent(eventName, dataLines.join("\n"));
      eventName = "";
      dataLines = [];
    }
    function processLine(rawLine) {
      const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
      if (line === "") {
        dispatchFrame();
        return;
      }
      if (line.startsWith(":")) return;
      const separatorIndex = line.indexOf(":");
      const field = separatorIndex < 0 ? line : line.slice(0, separatorIndex);
      let value = separatorIndex < 0 ? "" : line.slice(separatorIndex + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "event") eventName = value;
      else if (field === "data") dataLines.push(value);
    }
    return {
      push(text) {
        textBuffer += text;
        const lines = textBuffer.split("\n");
        textBuffer = lines.pop() || "";
        lines.forEach(processLine);
      },
      finish(text = "") {
        textBuffer += text;
        if (textBuffer) {
          processLine(textBuffer);
          textBuffer = "";
        }
        dispatchFrame();
      }
    };
  }

  function wrapChatSnapshotStream(response, token) {
    if (!response?.body || typeof pageWindow.ReadableStream !== "function") return response;
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    const sseParser = createSseParser((eventName, data) => {
      if (eventName !== "complete" || !data || !isCurrentRoute(token)) return;
      try {
        const payload = JSON.parse(data);
        parseResponseMessages(payload?.data?.messages, token);
      } catch {
        // 忽略不完整的完成帧。
      }
    });
    const proxyStream = new pageWindow.ReadableStream({
      async pull(controller) {
        try {
          const { done, value } = await reader.read();
          if (done) {
            sseParser.finish(decoder.decode());
            controller.close();
            return;
          }
          sseParser.push(decoder.decode(value, { stream: true }));
          controller.enqueue(value);
        } catch (error) {
          controller.error(error);
        }
      },
      cancel(reason) {
        return reader.cancel(reason);
      }
    });
    const ResponseCtor = pageWindow.Response || Response;
    return new ResponseCtor(proxyStream, {
      headers: response.headers,
      status: response.status,
      statusText: response.statusText
    });
  }

  function installFetchInterceptor() {
    if (typeof originalFetch !== "function") return;
    pageWindow.fetch = async function dolaQianwenFetch(...args) {
      detectRouteChange();
      const token = currentRouteToken;
      const url = requestUrl(args[0]);
      const response = await originalFetch.apply(this, args);
      if (!url || !url.hostname.endsWith("qianwen.com")) return response;
      if (url.pathname.includes(SESSION_MESSAGES_PATH)) {
        return inspectJsonResponse(response, token, (payload) => payload?.data?.list || []);
      }
      if (url.pathname.includes(SHARE_INFO_PATH)) {
        return inspectJsonResponse(response, token, (payload) => payload?.data?.session?.record_list || []);
      }
      if (url.pathname.includes(CHAT_SNAPSHOT_PATH)) {
        return wrapChatSnapshotStream(response, token);
      }
      return response;
    };
  }

  function installRouteTracking() {
    const originalPushState = pageWindow.history.pushState;
    const originalReplaceState = pageWindow.history.replaceState;
    if (typeof originalPushState === "function") {
      pageWindow.history.pushState = function dolaQianwenPushState(...args) {
        const result = originalPushState.apply(this, args);
        setTimeout(detectRouteChange, 0);
        return result;
      };
    }
    if (typeof originalReplaceState === "function") {
      pageWindow.history.replaceState = function dolaQianwenReplaceState(...args) {
        const result = originalReplaceState.apply(this, args);
        setTimeout(detectRouteChange, 0);
        return result;
      };
    }
    pageWindow.addEventListener("popstate", detectRouteChange);
    pageWindow.addEventListener("hashchange", detectRouteChange);
    setInterval(detectRouteChange, 800);
  }

  function refreshNow() {
    detectRouteChange();
    scanPageMedia();
    publish();
  }

  document.addEventListener(REQUEST_EVENT, refreshNow);
  installFetchInterceptor();
  installRouteTracking();

  function initialize() {
    scanPageMedia();
    publish();
    [500, 1500, 3000].forEach((delay) => {
      setTimeout(() => {
        detectRouteChange();
        scanPageMedia();
        publish();
      }, delay);
    });
  }

  if (document.readyState === "loading" || !document.body) {
    document.addEventListener("DOMContentLoaded", initialize, { once: true });
  } else {
    initialize();
  }
})();
