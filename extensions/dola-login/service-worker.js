/* Dola Pool · CK 自动登录 —— 后台部分
 *
 * 职责只有一件：收到页面转来的 token 后，去本机 Dola Pool 取该账号的 Cookie，
 * 用 chrome.cookies.set 写进浏览器（HttpOnly 也能写，网页脚本写不了这个），
 * 然后让页面刷新成登录态。
 *
 * 为什么不从页面里直接 fetch 本机接口：本机接口返回的是明文 CK，
 * 不能让 dola.com 的页面 JS 拿到；扩展后台有 host_permissions，能跨域取，
 * 页面只负责转一个一次性 token。
 */

const HOME = "https://www.dola.com/chat/";

function normalizeCookie(c) {
  const host = c.host || "www.dola.com";
  const out = {
    url: "https://" + host + "/",
    name: String(c.name || ""),
    value: String(c.value == null ? "" : c.value),
    domain: c.domain || ".dola.com",
    path: c.path || "/",
    secure: c.secure !== false,
    httpOnly: c.httpOnly !== false,
  };
  if (c.sameSite && c.sameSite !== "unspecified") out.sameSite = c.sameSite;
  if (c.expirationDate) out.expirationDate = Number(c.expirationDate);
  return out;
}

async function applyCookies(cookies) {
  let ok = 0;
  const fails = [];
  for (const raw of cookies) {
    if (!raw || !raw.name) continue;
    try {
      await chrome.cookies.set(normalizeCookie(raw));
      ok += 1;
    } catch (e) {
      fails.push(raw.name + "：" + String((e && e.message) || e));
    }
  }
  return { ok, fails };
}

async function loadFromPool(port, token) {
  const url =
    "http://127.0.0.1:" + encodeURIComponent(port) +
    "/api/desktop/bridge/cookies?token=" + encodeURIComponent(token);
  const res = await fetch(url, { cache: "no-store", credentials: "omit" });
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      detail = body && body.error ? body.error : "";
    } catch (e) {
      detail = "";
    }
    throw new Error("本机接口 " + res.status + (detail ? "：" + detail : ""));
  }
  return await res.json();
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.type !== "DP_LOGIN" || !msg.token) return;
  (async () => {
    try {
      const data = await loadFromPool(msg.port || "8000", msg.token);
      const done = await applyCookies(data.cookies || []);
      sendResponse({
        ok: done.ok > 0,
        count: done.ok,
        account: data.account_id || 0,
        fails: done.fails.slice(0, 5),
        error: done.ok > 0 ? "" : "一个 Cookie 都没写进去：" + done.fails.slice(0, 3).join("；"),
      });
    } catch (e) {
      sendResponse({ ok: false, error: String((e && e.message) || e) });
    }
  })();
  return true;      // 异步回包
});

chrome.runtime.onInstalled.addListener(() => {
  try { chrome.storage.local.set({ dpLoginReady: Date.now() }); } catch (e) {}
});
