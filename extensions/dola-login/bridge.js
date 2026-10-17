/* Dola Pool · CK 自动登录 —— 页面部分
 *
 * 账号池点【🌐 网页】时，Dola Pool 会先打开本机的一张桥接页：
 *   http://127.0.0.1:<端口>/desktop/bridge?token=<一次性 token>
 * 这里读到 token 就交给扩展后台去取 CK、写 Cookie；写成后页面自己跳到 dola.com。
 *
 * 为什么不在 dola.com 的网址里塞参数：dola 是 SPA，未知的 hash/query 有可能
 * 被它自己的路由当成路径；而且 token 出现在 dola 的地址栏/Referer 里也没必要。
 */

(function () {
  if (window.__dpLoginRan) return;
  window.__dpLoginRan = true;

  const isBridge = location.pathname === "/desktop/bridge";
  if (!isBridge) return;                       // dola.com 页面什么都不做

  const params = new URLSearchParams(location.search || "");
  const token = params.get("token") || "";
  const port = params.get("port") || location.port || "8000";
  if (!token) return;

  function toast(text, color) {
    try {
      const box = document.createElement("div");
      box.textContent = text;
      box.style.cssText = [
        "position:fixed", "right:18px", "bottom:18px", "z-index:2147483647",
        "max-width:420px", "padding:12px 16px", "border-radius:10px",
        "font:13px/1.6 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif",
        "background:#10191b", "color:#e7eeee", "box-shadow:0 8px 28px rgba(0,0,0,.45)",
        "border:1px solid " + (color || "#2c3b3e"),
      ].join(";");
      (document.body || document.documentElement).appendChild(box);
    } catch (e) {
      /* 页面还没 body 就算了，bridge 页自己会写状态 */
    }
  }

  try {
    chrome.runtime.sendMessage({ type: "DP_LOGIN", token: token, port: port }, (res) => {
      const err = chrome.runtime.lastError;
      if (err || !res) {
        toast("⚠️ 自动登录失败：扩展后台没回话（" +
              String((err && err.message) || "未知原因") + "）", "#f5c76b");
        return;
      }
      if (!res.ok) {
        toast("⚠️ 自动登录失败：" + (res.error || "CK 没写进去"), "#f5c76b");
        return;
      }
      toast("✅ 已写入 " + res.count + " 个 Cookie，正在进入 #" + res.account + " 的 Dola…", "#2f8f7c");
      // 页面自己会轮询状态并跳转；这里再兜一次，防止桥接页脚本被 CSP 之类挡住
      setTimeout(() => {
        try { if (location.pathname === "/desktop/bridge") location.replace("https://www.dola.com/chat/"); }
        catch (e) {}
      }, 900);
    });
  } catch (e) {
    toast("⚠️ 自动登录失败：扩展被禁用或版本不对（" + String(e && e.message) + "）", "#f5c76b");
  }
})();
