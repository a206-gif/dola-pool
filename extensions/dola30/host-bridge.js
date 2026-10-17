(function () {
  "use strict";

  const HOST_SOURCE = "dola-multibrowser-host";
  const REPLY_SOURCE = "dola-multibrowser-host-reply";

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const data = event.data;
    if (!data || data.source !== HOST_SOURCE || !data.requestId) return;

    const reply = (payload) => {
      window.postMessage(
        { source: REPLY_SOURCE, requestId: data.requestId, ...payload },
        "*"
      );
    };

    const command = data.command;
    if (command === "ENABLE_DEBUGGER_SNIFF") {
      try {
        chrome.runtime.sendMessage({ type: "ENABLE_DEBUGGER_SNIFF" }, (response) => {
          const err = chrome.runtime.lastError;
          reply({ ok: !err && response?.ok !== false, error: err?.message || null });
        });
      } catch (e) {
        reply({ ok: false, error: String(e) });
      }
      return;
    }

    if (command === "SET_AUTO_DEBUGGER_SNIFF") {
      try {
        chrome.runtime.sendMessage(
          { type: "SET_AUTO_DEBUGGER_SNIFF", enabled: !!data.enabled },
          (response) => {
            const err = chrome.runtime.lastError;
            reply({ ok: !err && response?.ok !== false, error: err?.message || null });
          }
        );
      } catch (e) {
        reply({ ok: false, error: String(e) });
      }
      return;
    }

    if (command === "GET_AUTO_DEBUGGER_SNIFF") {
      try {
        chrome.runtime.sendMessage({ type: "GET_AUTO_DEBUGGER_SNIFF" }, (response) => {
          const err = chrome.runtime.lastError;
          reply({
            ok: !err && response?.ok !== false,
            enabled: !!response?.enabled,
            error: err?.message || null
          });
        });
      } catch (e) {
        reply({ ok: false, enabled: false, error: String(e) });
      }
      return;
    }

    reply({ ok: false, error: "unknown_command" });
  });
})();
