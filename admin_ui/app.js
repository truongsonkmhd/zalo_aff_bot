"use strict";
/*
 * Trang quản trị Bot Đổi Link.
 * Quy tắc an toàn: không dùng innerHTML; mã CSRF chỉ giữ trong bộ nhớ; cookie Shopee chỉ ghi, không hiển thị lại.
 */
(() => {
  const $ = (id) => document.getElementById(id);
  const SVG_NS = "http://www.w3.org/2000/svg";
  const REFRESH_MS = 30000;

  const state = {
    csrf: null,
    settings: null,
    overview: null,
    view: null,
    renderToken: 0,
    statsDays: 7,
    historyLimit: 50,
    historyFilter: "all",
    pollTimer: null,
  };

  // ── Dựng DOM an toàn ────────────────────────────────────────────
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : String(v));
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }

  function svg(tag, attrs, ...children) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v !== null && v !== undefined) el.setAttribute(k, String(v));
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }

  const ICONS = {
    check: ["M20 6 9 17l-5-5"],
    alert: ["M12 3l10 18H2z", "M12 10v4", "M12 17.5v.5"],
    stop: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z", "M8 12h8"],
    pause: ["M9 5v14", "M15 5v14"],
    refresh: ["M21 12a9 9 0 1 1-3-6.7", "M21 4v5h-5"],
    bell: ["M18 16V11a6 6 0 0 0-12 0v5l-2 2h16z", "M10 20a2 2 0 0 0 4 0"],
    shield: ["M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z", "M9 12l2 2 4-4"],
    trash: ["M4 7h16", "M10 11v6", "M14 11v6", "M6 7l1 13h10l1-13", "M9 7V4h6v3"],
    external: ["M14 4h6v6", "M20 4l-9 9", "M18 14v6H4V6h6"],
  };
  function icon(name) {
    return svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, ICONS[name].map((d) => svg("path", { d })));
  }

  // ── Định dạng & nhãn ────────────────────────────────────────────
  const nf = new Intl.NumberFormat("vi-VN");
  const fmtNum = (n) => (n === null || n === undefined ? "—" : nf.format(n));
  const fmtTime = (ts) => new Date(ts * 1000).toLocaleString("vi-VN", {
    hour: "2-digit", minute: "2-digit", day: "2-digit", month: "2-digit", year: "numeric",
  });
  const fmtClock = () => new Date().toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const fmtSeconds = (ms) => (ms === null || ms === undefined ? "—" : `${(ms / 1000).toLocaleString("vi-VN", { maximumFractionDigits: 2 })} giây`);
  function fmtDuration(s) {
    if (s >= 3600) return `${Math.floor(s / 3600)} giờ ${Math.floor((s % 3600) / 60)} phút`;
    if (s >= 60) return `${Math.floor(s / 60)} phút`;
    return `${s} giây`;
  }
  const shortHash = (hsh) => (hsh ? hsh.slice(0, 8) : "—");
  const fmtDec = (x) => (x === null || x === undefined ? "—" : Number(x).toLocaleString("vi-VN", { maximumFractionDigits: 1 }));
  const fmtPct = (x) => (x === null || x === undefined ? "—" : `${fmtDec(x)}%`);

  const STATUS = {
    ok: ["Thành công", "ok"],
    cached: ["Thành công (bộ nhớ đệm)", "ok"],
    rejected: ["Link không hợp lệ", "warn"],
    failed: ["Đổi link lỗi", "bad"],
    rate_limited: ["Vượt hạn mức", "warn"],
  };
  const SOURCE = { shopee_openapi: "Open API", shopee_web: "Phiên web", cache: "Bộ nhớ đệm", "-": "—" };
  const STAGE = { convert: "Đổi link", send: "Gửi tin Zalo", internal: "Lỗi nội bộ" };
  const ACTION = {
    login: "Đăng nhập", login_failed: "Đăng nhập sai mật khẩu", bot_pause: "Tạm dừng bot", bot_enable: "Bật bot",
    dlq_clear: "Xóa danh sách việc bị lỗi", web_session_update: "Cập nhật cookie Shopee", adapter_reset: "Chạy lại kết nối",
    orders_import: "Tải file đơn hàng", orders_sync: "Đồng bộ đơn hàng",
    browser_open: "Mở Chrome lấy phiên Shopee", browser_sync: "Lấy phiên Shopee từ Chrome",
  };
  const BROWSER_RESULT = {
    updated: ["Đã lấy phiên mới", "ok"], unchanged: ["Phiên đang dùng tốt", "ok"],
    not_logged_in: ["Chưa đăng nhập trong Chrome", "warn"], browser_closed: ["Chrome đang đóng", "warn"],
    rejected: ["Shopee từ chối phiên", "bad"], cookie_invalid: ["Cookie đọc được không hợp lệ", "bad"],
  };
  const browserResult = (r) => BROWSER_RESULT[r] || (r ? ["Không đọc được Chrome", "bad"] : ["Chưa kiểm tra", "warn"]);
  const ORDER_STATUS = { completed: ["Hoàn thành", "ok"], pending: ["Chờ xác nhận", "warn"], cancelled: ["Đã hủy", "bad"] };
  const ORDER_SOURCE = { openapi: "Open API", csv: "File CSV" };
  const ADAPTER = { shopee_openapi: "Shopee · Open API", shopee_web: "Shopee · Phương án 2 (cookie)", tiktok: "TikTok Shop" };
  const COUNTER = {
    webhook_bad_signature: "Webhook giả mạo (sai chữ ký)", webhook_missing_signature: "Webhook thiếu chữ ký",
    webhook_stale: "Tin cũ bị phát lại", webhook_wrong_app: "Webhook sai ứng dụng", webhook_bad_body: "Dữ liệu webhook hỏng",
    webhook_bad_timestamp: "Webhook sai thời gian", webhook_duplicate: "Tin trùng đã bỏ qua", queue_full: "Hàng đợi đầy",
  };
  const ERROR = {
    host_not_allowed: "Không phải link Shopee/TikTok", not_a_product: "Không phải link sản phẩm", too_long: "Link quá dài",
    unexpected_link: "Shopee trả link lạ, đã chặn", adapter_paused: "Tạm nghỉ do lỗi liên tiếp",
    web_session_missing: "Chưa nhập cookie Shopee", web_session_stopped: "Phiên Shopee bị chặn, cần cookie mới",
    web_rate_limited: "Vượt 20 link/phút của Phương án 2", tiktok_not_ready: "TikTok chưa hỗ trợ",
    user_minute: "Khách gửi quá nhanh (theo phút)", user_day: "Khách vượt hạn mức trong ngày", global: "Toàn hệ thống quá tải",
    network: "Shopee tạm thời lỗi", bad_json: "Shopee tạm thời lỗi", send_network: "Lỗi mạng khi gửi Zalo",
    no_adapter: "Chưa cấu hình sàn này",
  };
  const UNSAFE = new Set(["userinfo", "ip_literal", "bad_port", "bad_scheme", "malformed"]);
  const SHORT_FAIL = new Set(["non_public_ip", "dns_failed", "resolve_failed", "short_link_dead", "too_many_redirects"]);
  function errorLabel(code) {
    if (!code) return "—";
    if (ERROR[code]) return ERROR[code];
    if (UNSAFE.has(code)) return "Link bị chặn vì không an toàn";
    if (SHORT_FAIL.has(code)) return "Không mở được link rút gọn";
    if (/^http_(429|5\d\d)$/.test(code)) return "Shopee tạm thời lỗi";
    if (code.startsWith("shopee_")) return `Shopee từ chối (mã ${code.slice(7)})`;
    if (code.startsWith("fail_code_")) return `Shopee không tạo được link (mã ${code.slice(10)})`;
    if (code.startsWith("zalo_") || code.startsWith("token_refresh")) return "Lỗi gửi tin Zalo";
    return code;
  }

  // ── Gọi API ─────────────────────────────────────────────────────
  class ApiError extends Error {
    constructor(status, message, handled = false) {
      super(message);
      this.status = status;
      this.handled = handled;
    }
  }
  const STATUS_TEXT = {
    404: "Không truy cập được trang quản trị từ mạng này.",
    413: "Dữ liệu gửi lên quá lớn.",
    429: "Thao tác quá nhiều lần, vui lòng đợi rồi thử lại.",
    500: "Bot gặp lỗi khi xử lý. Xem nhật ký máy chủ.",
  };

  async function api(path, { method = "GET", body, retried = false } = {}) {
    const opts = { method, credentials: "same-origin", headers: {} };
    if (method !== "GET") opts.headers["X-CSRF-Token"] = state.csrf || "";
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(`/admin/api${path}`, opts);
    } catch {
      throw new ApiError(0, "Không kết nối được tới bot. Kiểm tra bot còn chạy và đường kết nối (SSH tunnel/VPN).");
    }
    let data = null;
    try { data = await res.json(); } catch { /* phản hồi không phải JSON */ }
    if (res.ok) return data;
    const message = data && typeof data.detail === "string" ? data.detail : (STATUS_TEXT[res.status] || `Lỗi ${res.status}`);
    if (res.status === 401 && path !== "/login") {
      showLogin("Phiên đăng nhập đã hết, vui lòng đăng nhập lại.");
      throw new ApiError(401, message, true);
    }
    if (res.status === 403 && method !== "GET" && !retried && path !== "/login") {
      const sess = await api("/session");
      if (sess && sess.authenticated) {
        state.csrf = sess.csrf_token;
        return api(path, { method, body, retried: true });
      }
      showLogin("Phiên đăng nhập đã hết, vui lòng đăng nhập lại.");
      throw new ApiError(403, message, true);
    }
    throw new ApiError(res.status, message);
  }

  // ── Thông báo & xác nhận ────────────────────────────────────────
  function toast(message, kind) {
    const el = h("div", { class: `toast${kind === "bad" ? " bad" : ""}`, role: kind === "bad" ? "alert" : "status", text: message });
    $("toasts").append(el);
    setTimeout(() => el.remove(), kind === "bad" ? 6000 : 3500);
  }
  function reportError(err) {
    if (!(err instanceof ApiError) || !err.handled) toast(err.message || "Có lỗi xảy ra", "bad");
  }

  function confirmAction({ title, text, ok, danger }) {
    const dialog = $("confirm-dialog");
    $("confirm-title").textContent = title;
    $("confirm-text").textContent = text;
    const okBtn = $("confirm-ok");
    okBtn.textContent = ok;
    okBtn.className = `btn ${danger ? "btn-danger" : "btn-primary"}`;
    dialog.returnValue = "";
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
      dialog.showModal();
    });
  }

  async function withBusy(button, fn) {
    button.disabled = true;
    try { await fn(); } catch (err) { reportError(err); } finally { button.disabled = false; }
  }

  // ── Giao diện sáng/tối ──────────────────────────────────────────
  function applyTheme(theme) {
    if (theme === "dark" || theme === "light") document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
  }
  function currentThemeIsDark() {
    const t = document.documentElement.dataset.theme;
    if (t) return t === "dark";
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  }
  try { applyTheme(localStorage.getItem("affbot-theme")); } catch { /* bỏ qua */ }
  $("theme-toggle").addEventListener("click", () => {
    const next = currentThemeIsDark() ? "light" : "dark";
    applyTheme(next);
    try { localStorage.setItem("affbot-theme", next); } catch { /* bỏ qua */ }
  });

  // ── Đăng nhập / đăng xuất ───────────────────────────────────────
  let lockTimer = null;
  function showLogin(message) {
    stopPolling();
    state.csrf = null;
    $("shell").hidden = true;
    $("login-view").hidden = false;
    const err = $("login-error");
    err.hidden = !message;
    err.textContent = message || "";
    $("login-password").focus();
  }

  function lockLogin(seconds) {
    const btn = $("login-submit");
    let left = seconds;
    clearInterval(lockTimer);
    btn.disabled = true;
    const tick = () => {
      btn.textContent = `Thử lại sau ${Math.ceil(left / 60)} phút`;
      if (left-- <= 0) {
        clearInterval(lockTimer);
        btn.disabled = false;
        btn.textContent = "Đăng nhập";
      }
    };
    tick();
    lockTimer = setInterval(tick, 1000);
  }

  $("login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const input = $("login-password");
    const btn = $("login-submit");
    const err = $("login-error");
    if (!input.value) {
      err.hidden = false;
      err.textContent = "Nhập mật khẩu quản trị.";
      return;
    }
    btn.disabled = true;
    err.hidden = true;
    let locked = false;
    try {
      const data = await api("/login", { method: "POST", body: { password: input.value } });
      state.csrf = data.csrf_token;
      input.value = "";
      await enterShell();
    } catch (e) {
      input.value = "";
      err.hidden = false;
      err.textContent = e.status === 401 ? "Sai mật khẩu. Sau 5 lần sai, đăng nhập bị khóa 15 phút." : e.message;
      if (e.status === 429) {
        locked = true;
        lockLogin(15 * 60);
      }
    } finally {
      if (!locked) btn.disabled = false;
    }
  });

  $("logout-btn").addEventListener("click", async () => {
    try { await api("/logout", { method: "POST" }); } catch { /* vẫn về trang đăng nhập */ }
    showLogin("Đã đăng xuất.");
  });

  async function enterShell() {
    $("login-view").hidden = true;
    $("shell").hidden = false;
    try {
      state.settings = await api("/settings");
    } catch (err) {
      reportError(err);
    }
    await refreshOverview();
    startPolling();
    route();
  }

  // ── Trạng thái tổng & huy hiệu điều hướng ───────────────────────
  function activeShopee(ov) {
    for (const key of ["shopee_openapi", "shopee_web"]) {
      const a = ov.adapters[key];
      if (a && a.active) return { key, ...a };
    }
    return null;
  }

  function health(ov) {
    const shopee = activeShopee(ov);
    const today = ov.today || {};
    if (!ov.bot_enabled) {
      return { level: "bad", icon: "pause", pill: "Bot đang tạm dừng", title: "Bot đang tạm dừng",
               text: "Khách gửi link sẽ nhận tin “bot đang bảo trì”. Bật lại khi bạn sẵn sàng." };
    }
    if (!shopee) {
      return { level: "bad", icon: "stop", pill: "Chưa kết nối Shopee", title: "Chưa kết nối Shopee",
               text: "Cấu hình Shopee Open API hoặc bật Phương án 2 trong file cấu hình.", link: "#shopee" };
    }
    if (shopee.state === "stopped") {
      return { level: "bad", icon: "stop", pill: "Kết nối Shopee bị chặn", title: "Kết nối Shopee bị chặn",
               text: `${shopee.reason || "Shopee từ chối phiên đăng nhập"}. Bot tạm ngừng đổi link cho tới khi bạn cập nhật.`,
               link: "#shopee" };
    }
    if (shopee.key === "shopee_web" && shopee.session && !shopee.session.configured) {
      return { level: "bad", icon: "stop", pill: "Chưa nhập cookie Shopee", title: "Chưa nhập cookie Shopee",
               text: "Phương án 2 cần cookie trang affiliate.shopee.vn để đổi link.", link: "#shopee" };
    }
    if (!ov.zalo_token.configured) {
      return { level: "bad", icon: "stop", pill: "Chưa kết nối Zalo", title: "Chưa kết nối Zalo OA",
               text: "Thiếu refresh token của Zalo Official Account trong cấu hình nên bot chưa gửi được tin." };
    }
    const warnings = [];
    if (shopee.state === "cooling") warnings.push(`Shopee đang tạm nghỉ sau nhiều lỗi liên tiếp, tự chạy lại sau ${fmtDuration(shopee.seconds_left)}.`);
    if (ov.dlq_len > 0) warnings.push(`Có ${fmtNum(ov.dlq_len)} việc bị lỗi cần xem.`);
    if (shopee.session && shopee.session.age_days > 7) warnings.push(`Cookie Shopee đã dùng ${fmtDec(shopee.session.age_days)} ngày, nên thay mới.`);
    if (warnings.length) {
      return { level: "warn", icon: "alert", pill: "Có việc cần xem", title: "Bot đang chạy, có việc cần xem",
               text: warnings.join(" ") };
    }
    const rate = today.success_rate === null || today.success_rate === undefined ? "" : `, tỉ lệ thành công ${fmtPct(today.success_rate)}`;
    return { level: "ok", icon: "check", pill: "Bot đang chạy tốt", title: "Bot đang chạy tốt",
             text: `Hôm nay đã đổi ${fmtNum(today.success || 0)} link${rate}.` };
  }

  function updateChrome(ov) {
    const hs = health(ov);
    const pill = $("health-pill");
    pill.className = `pill ${hs.level}`;
    pill.textContent = hs.pill;
    const dlq = $("nav-dlq-badge");
    dlq.hidden = !ov.dlq_len;
    dlq.textContent = ov.dlq_len > 99 ? "99+" : String(ov.dlq_len);
    const shopee = activeShopee(ov);
    $("nav-shopee-badge").hidden = !(shopee && (shopee.state === "stopped" ||
      (shopee.session && (!shopee.session.configured || shopee.session.age_days > 7))));
  }

  async function refreshOverview() {
    try {
      state.overview = await api("/overview");
      updateChrome(state.overview);
    } catch (err) {
      if (!(err instanceof ApiError && err.handled)) {
        const pill = $("health-pill");
        pill.className = "pill bad";
        pill.textContent = "Mất kết nối tới bot";
      }
    }
    return state.overview;
  }

  function startPolling() {
    stopPolling();
    state.pollTimer = setInterval(async () => {
      if (document.hidden) return;
      await refreshOverview();
      if (state.view === "tong-quan" && state.overview) renderOverview(state.overview);
    }, REFRESH_MS);
  }
  function stopPolling() {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
  document.addEventListener("visibilitychange", async () => {
    if (!document.hidden && state.csrf && state.view === "tong-quan") {
      await refreshOverview();
      if (state.overview) renderOverview(state.overview);
    }
  });

  // ── Điều hướng ─────────────────────────────────────────────────
  const VIEWS = {
    "tong-quan": () => (state.overview ? renderOverview(state.overview) : loadView(refreshOverview, renderOverview)),
    "thong-ke": () => loadView(() => api(`/stats?days=${state.statsDays}`), renderStats),
    "lich-su": () => loadView(() => api(`/conversions?limit=${state.historyLimit}`), renderHistory),
    "viec-loi": () => loadView(() => api("/dlq?limit=200"), renderDlq),
    "don-hang": () => loadView(() => api("/orders?limit=100"), renderOrders),
    "shopee": () => loadView(refreshOverview, renderShopee),
    "cai-dat": () => loadView(() => api("/audit-events?limit=50"), renderSettings),
  };

  function route() {
    const name = (location.hash || "#tong-quan").slice(1);
    const view = VIEWS[name] ? name : "tong-quan";
    state.view = view;
    for (const a of document.querySelectorAll(".nav a")) {
      if (a.dataset.view === view) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    }
    VIEWS[view]();
  }
  window.addEventListener("hashchange", () => {
    if (state.csrf) {
      route();
      $("view").focus({ preventScroll: true });
    }
  });

  function setView(...nodes) {
    $("view").replaceChildren(...nodes.flat().filter(Boolean));
  }

  async function loadView(fetcher, renderer) {
    const token = ++state.renderToken;
    setView(h("div", { class: "skeleton", "aria-label": "Đang tải" }), h("div", { class: "skeleton" }));
    try {
      const data = await fetcher();
      if (token !== state.renderToken) return;
      renderer(data);
    } catch (err) {
      if (token !== state.renderToken || (err instanceof ApiError && err.handled)) return;
      setView(h("div", { class: "card" },
        h("h2", { text: "Không tải được dữ liệu" }),
        h("p", { class: "muted", text: err.message }),
        h("div", {}, h("button", { class: "btn btn-ghost", type: "button", onclick: route }, icon("refresh"), "Thử lại"))));
    }
  }

  function head(title, subtitle, ...actions) {
    return h("div", { class: "view-head" },
      h("div", {}, h("h1", { text: title }), subtitle ? h("p", { text: subtitle }) : null),
      actions.length ? h("div", { class: "form-actions" }, actions) : null);
  }

  function kpi(label, value, sub) {
    return h("div", { class: "card kpi" },
      h("span", { class: "kpi-label", text: label }),
      h("span", { class: "kpi-value", text: value }),
      sub ? h("span", { class: "kpi-sub", text: sub }) : null);
  }

  function chip(text, kind) {
    return h("span", { class: `chip${kind ? ` ${kind}` : ""}`, text });
  }

  function emptyRow(cols, text) {
    return h("tr", {}, h("td", { colspan: cols, class: "empty", text }));
  }

  function productCell(itemKey) {
    const m = /^shp:(\d{1,15}):(\d{1,20})$/.exec(itemKey || "");
    if (!m) return itemKey ? h("span", { text: itemKey }) : h("span", { class: "muted", text: "—" });
    return h("a", { href: `https://shopee.vn/product/${m[1]}/${m[2]}`, target: "_blank", rel: "noopener noreferrer" },
      `Sản phẩm ${m[2]}`);
  }

  // ── Tổng quan ───────────────────────────────────────────────────
  function adapterChip(a) {
    if (a.state === "not_ready") return chip("Sắp có", "");
    if (a.state === "stopped") return chip("Bị chặn", "bad");
    if (a.state === "cooling") return chip(`Tạm nghỉ ${fmtDuration(a.seconds_left)}`, "warn");
    return chip(a.active ? "Đang dùng" : "Dự phòng", "ok");
  }

  function renderOverview(ov) {
    state.renderToken++;
    const hs = health(ov);
    const today = ov.today || {};

    const toggle = h("input", { type: "checkbox", id: "bot-switch", role: "switch" });
    toggle.checked = ov.bot_enabled;
    toggle.setAttribute("aria-checked", String(ov.bot_enabled));
    toggle.addEventListener("change", async () => {
      const enable = toggle.checked;
      if (!enable) {
        const ok = await confirmAction({
          title: "Tạm dừng bot?",
          text: "Khách gửi link sẽ nhận tin “bot đang bảo trì” và không được đổi link cho tới khi bạn bật lại.",
          ok: "Tạm dừng", danger: true,
        });
        if (!ok) { toggle.checked = true; return; }
      }
      toggle.disabled = true;
      try {
        await api("/bot", { method: "POST", body: { enabled: enable } });
        toast(enable ? "Đã bật bot" : "Đã tạm dừng bot");
        renderOverview(await refreshOverview());
      } catch (err) {
        toggle.checked = !enable;
        reportError(err);
      } finally {
        toggle.disabled = false;
      }
    });

    const hero = h("section", { class: `hero-status ${hs.level}`, "aria-live": "polite" },
      h("span", { class: "hero-icon" }, icon(hs.icon)),
      h("div", { class: "hero-text" },
        h("h2", { text: hs.title }),
        h("p", { text: hs.text }),
        hs.link ? h("p", {}, h("a", { href: hs.link, text: "Mở trang Kết nối Shopee →" })) : null),
      h("label", { class: "switch", for: "bot-switch" }, toggle, h("span", { class: "switch-track", "aria-hidden": "true" }),
        h("span", { text: ov.bot_enabled ? "Bot đang bật" : "Bot đang tắt" })));

    const kpis = h("div", { class: "grid grid-kpi" },
      kpi("Link đã đổi hôm nay", fmtNum(today.success || 0), `trên ${fmtNum(today.total || 0)} lượt khách gửi`),
      kpi("Tỉ lệ thành công", fmtPct(today.success_rate), "hôm nay"),
      kpi("Khách hôm nay", fmtNum(today.unique_users || 0), "số người khác nhau"),
      kpi("Thời gian xử lý", fmtSeconds(today.avg_latency_ms), "trung bình mỗi link"),
      kpi("Từ bộ nhớ đệm", fmtNum(today.cache_hits || 0), "không tốn lượt gọi Shopee"));

    const rows = [];
    for (const [key, a] of Object.entries(ov.adapters)) {
      let sub = a.state === "stopped" ? a.reason : a.state === "not_ready" ? "Sẽ hỗ trợ ở giai đoạn sau" :
        a.active ? "Đang dùng để đổi link" : "Không dùng";
      if (a.session && a.session.configured) sub = `Cookie cập nhật ${a.session.updated_at} · đã dùng ${fmtDec(a.session.age_days)} ngày`;
      rows.push(h("div", { class: "status-row" },
        h("div", { class: "grow" }, h("strong", { text: ADAPTER[key] || key }), h("span", { text: sub })), adapterChip(a)));
    }
    const zt = ov.zalo_token;
    rows.push(h("div", { class: "status-row" },
      h("div", { class: "grow" }, h("strong", { text: "Zalo Official Account" }),
        h("span", { text: !zt.configured ? "Thiếu refresh token trong cấu hình" :
          zt.expires_in_s > 0 ? `Mã truy cập còn hạn ${fmtDuration(zt.expires_in_s)}` : "Sẽ lấy mã truy cập mới ở lần gửi tin tới" })),
      chip(zt.configured ? "Đã kết nối" : "Chưa kết nối", zt.configured ? "ok" : "bad")));
    rows.push(h("div", { class: "status-row" },
      h("div", { class: "grow" }, h("strong", { text: "Hàng chờ xử lý" }),
        h("span", { text: `${fmtNum(ov.queue_len)} tin đang chờ · ${fmtNum(ov.dlq_len)} việc bị lỗi` })),
      ov.dlq_len ? h("a", { href: "#viec-loi", class: "chip warn", text: "Xem việc bị lỗi" }) : chip("Ổn định", "ok")));

    const connections = h("section", { class: "card" }, h("h2", { text: "Kết nối" }), rows);

    const alertItems = (ov.alerts || []).map((a) => h("li", {}, icon("bell"),
      h("div", {}, h("span", { text: a.text }), h("time", { text: fmtTime(a.ts) }))));
    const counters = Object.entries(ov.security_counters || {}).filter(([, n]) => n > 0);
    const alerts = h("section", { class: "card" },
      h("h2", { text: "Cảnh báo" }),
      alertItems.length ? h("ul", { class: "alert-list" }, alertItems) : h("p", { class: "muted", text: "Không có cảnh báo nào." }),
      h("h3", {}, "Bảo vệ đã chặn kể từ lần khởi động"),
      counters.length
        ? h("div", { class: "bars" }, counters.map(([k, n]) => h("div", { class: "status-row" },
            h("div", { class: "grow" }, h("span", { text: COUNTER[k] || k })), h("strong", { class: "num", text: fmtNum(n) }))))
        : h("p", { class: "muted" }, icon("shield"), " Chưa phát hiện truy cập bất thường."));

    setView(
      head("Tổng quan", `Cập nhật lúc ${fmtClock()} · tự làm mới mỗi 30 giây`),
      hero, kpis, h("div", { class: "grid grid-2" }, connections, alerts));
  }

  // ── Thống kê ───────────────────────────────────────────────────
  function lastDays(n) {
    const out = [];
    const d = new Date();
    for (let i = n - 1; i >= 0; i--) {
      const x = new Date(d.getFullYear(), d.getMonth(), d.getDate() - i);
      out.push(`${x.getFullYear()}-${String(x.getMonth() + 1).padStart(2, "0")}-${String(x.getDate()).padStart(2, "0")}`);
    }
    return out;
  }

  function niceMax(v) {
    if (v <= 5) return 5;
    const mag = 10 ** Math.floor(Math.log10(v));
    for (const m of [1, 2, 2.5, 5, 10]) if (m * mag >= v) return m * mag;
    return 10 * mag;
  }

  function dayChart(byDay, days) {
    const map = new Map(byDay.map((d) => [d.date, d]));
    const series = lastDays(days).map((date) => ({ date, ok: map.get(date)?.success || 0, bad: map.get(date)?.failed || 0 }));
    // Vẽ theo đúng độ rộng khung để chữ trục không bị thu nhỏ trên điện thoại
    const W = Math.round(Math.min(760, Math.max(320, ($("view").clientWidth || 760) - 80)));
    const H = W < 500 ? 200 : 240, padL = 34, padR = 6, padT = 12, padB = 28;
    const max = niceMax(Math.max(1, ...series.map((d) => d.ok + d.bad)));
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const step = plotW / series.length;
    const bw = Math.max(2, Math.min(28, step * 0.62));
    const y = (v) => padT + plotH - (v / max) * plotH;
    const g = [];
    for (const t of [0, max / 2, max]) {
      g.push(svg("line", { class: "grid-line", x1: padL, x2: W - padR, y1: y(t), y2: y(t) }));
      g.push(svg("text", { x: padL - 8, y: y(t) + 4, "text-anchor": "end" }, fmtNum(t)));
    }
    const every = Math.ceil(series.length / Math.max(4, Math.floor(W / 64)));
    series.forEach((d, i) => {
      const x = padL + i * step + (step - bw) / 2;
      const [, mm, dd] = d.date.split("-");
      const label = `${dd}/${mm}: ${fmtNum(d.ok)} thành công, ${fmtNum(d.bad)} không thành công`;
      const hOk = (d.ok / max) * plotH, hBad = (d.bad / max) * plotH;
      g.push(svg("rect", { class: "bar-ok", x, y: padT + plotH - hOk, width: bw, height: hOk, rx: 2 }, svg("title", {}, label)));
      g.push(svg("rect", { class: "bar-bad", x, y: padT + plotH - hOk - hBad, width: bw, height: hBad, rx: 2 }, svg("title", {}, label)));
      if (i % every === 0 || i === series.length - 1) {
        g.push(svg("text", { x: x + bw / 2, y: H - 8, "text-anchor": "middle" }, `${dd}/${mm}`));
      }
    });
    const total = series.reduce((s, d) => s + d.ok + d.bad, 0);
    return h("figure", { class: "chart", style: "margin:0" },
      svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
                   "aria-label": `Biểu đồ số lượt đổi link theo ngày trong ${days} ngày, tổng ${total} lượt` }, g));
  }

  function breakdown(entries, total, kindOf) {
    if (!entries.length) return h("p", { class: "muted", text: "Chưa có dữ liệu." });
    return h("div", { class: "bars" }, entries.map(([label, n, kind]) => h("div", { class: "bar-row" },
      h("span", { text: label }),
      h("div", { class: "bar-track" }, h("div", { class: `bar-fill ${kind || ""}`, style: `width:${total ? (n * 100) / total : 0}%` })),
      h("span", { class: "num", text: fmtNum(n) }))));
  }

  function renderStats(st) {
    state.lastStats = st;
    const seg = h("div", { class: "segmented", role: "group", "aria-label": "Khoảng thời gian" },
      [7, 30, 90].map((d) => h("button", {
        type: "button", "aria-pressed": String(state.statsDays === d), text: `${d} ngày`,
        onclick: () => { state.statsDays = d; route(); },
      })));
    const statusEntries = Object.entries(st.by_status).sort((a, b) => b[1] - a[1])
      .map(([k, n]) => [STATUS[k]?.[0] || k, n, STATUS[k]?.[1] === "ok" ? "ok" : "bad"]);
    const errors = st.top_errors.map((e) => h("tr", {},
      h("td", { class: "wrap", text: errorLabel(e.code) }), h("td", { class: "num", text: fmtNum(e.count) })));

    setView(
      head("Thống kê", "Số lượt khách gửi link và kết quả đổi link.", seg),
      h("div", { class: "grid grid-kpi" },
        kpi("Tổng lượt", fmtNum(st.total), `${st.days} ngày qua`),
        kpi("Đổi thành công", fmtNum(st.success), `${fmtNum(st.cache_hits)} lượt từ bộ nhớ đệm`),
        kpi("Tỉ lệ thành công", fmtPct(st.success_rate), "tính cả link khách gửi sai"),
        kpi("Số khách", fmtNum(st.unique_users), "người khác nhau")),
      h("section", { class: "card" },
        h("div", { class: "card-head" }, h("h2", { text: "Theo ngày" }),
          h("div", { class: "legend" }, h("span", {}, h("i", { class: "l-ok" }), "Thành công"),
            h("span", {}, h("i", { class: "l-bad" }), "Không thành công"))),
        dayChart(st.by_day, st.days)),
      h("div", { class: "grid grid-2" },
        h("section", { class: "card" }, h("h2", { text: "Kết quả" }), breakdown(statusEntries, st.total)),
        h("section", { class: "card" }, h("h2", { text: "Lý do không thành công hay gặp" }),
          h("div", { class: "table-wrap" }, h("table", {},
            h("thead", {}, h("tr", {}, h("th", { text: "Lý do" }), h("th", { text: "Số lượt" }))),
            h("tbody", {}, errors.length ? errors : emptyRow(2, "Không có lỗi nào.")))))));
  }

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (state.view === "thong-ke" && state.lastStats) renderStats(state.lastStats);
    }, 200);
  });

  // ── Lịch sử ────────────────────────────────────────────────────
  function renderHistory(data) {
    const filters = { all: "Tất cả", ok: "Thành công", problem: "Không thành công" };
    const items = data.items.filter((r) => state.historyFilter === "all" ||
      (state.historyFilter === "ok") === (r.status === "ok" || r.status === "cached"));

    const filterSel = h("select", { id: "history-filter", "aria-label": "Lọc theo kết quả",
      onchange: (e) => { state.historyFilter = e.target.value; renderHistory(data); } },
      Object.entries(filters).map(([v, t]) => h("option", { value: v, selected: state.historyFilter === v }, t)));
    const limitSel = h("select", { id: "history-limit", "aria-label": "Số dòng",
      onchange: (e) => { state.historyLimit = Number(e.target.value); route(); } },
      [50, 100, 200].map((n) => h("option", { value: n, selected: state.historyLimit === n }, `${n} lượt gần nhất`)));

    const rows = items.map((r) => {
      const [label, kind] = STATUS[r.status] || [r.status, ""];
      return h("tr", {},
        h("td", { class: "num", text: fmtTime(r.ts) }),
        h("td", {}, h("code", { title: "Mã khách đã được băm, không phải ID Zalo thật", text: shortHash(r.user_hash) })),
        h("td", {}, productCell(r.item_key)),
        h("td", {}, chip(label, kind)),
        h("td", { class: "wrap", text: r.error_code ? errorLabel(r.error_code) : "—" }),
        h("td", { class: "num", text: fmtSeconds(r.latency_ms) }),
        h("td", { text: SOURCE[r.source] || r.source || "—" }));
    });

    setView(
      head("Lịch sử đổi link", "Mỗi dòng là một link khách gửi. Bot không lưu nội dung tin nhắn hay ID Zalo của khách.",
        filterSel, limitSel),
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["Thời gian", "Khách", "Sản phẩm", "Kết quả", "Chi tiết", "Xử lý", "Nguồn"].map((t) => h("th", { text: t })))),
        h("tbody", {}, rows.length ? rows : emptyRow(7, "Chưa có lượt đổi link nào.")))));
  }

  // ── Đơn hàng ───────────────────────────────────────────────────
  function renderOrders(data) {
    const sm = data.summary;
    const cb = data.cashback_percent;
    const last = sm.last_sync;

    const fileInput = h("input", { type: "file", id: "orders-file", accept: ".csv,text/csv" });
    const importBtn = h("button", { class: "btn btn-primary", type: "submit" }, "Tải lên");
    const importMsg = h("p", { class: "hint", role: "status" });
    const importForm = h("form", { class: "card" },
      h("h2", { text: "Tải file báo cáo từ Shopee" }),
      h("p", { class: "muted", text: "Vào affiliate.shopee.vn → Báo cáo → Báo cáo chuyển đổi → Xuất dữ liệu (CSV), rồi chọn file ở đây. Tải lại file mới sẽ cập nhật trạng thái đơn cũ." }),
      h("label", { class: "field", for: "orders-file" }, h("span", { class: "field-label", text: "File CSV (tối đa 5 MB)" })),
      fileInput, importMsg,
      h("div", { class: "form-actions" }, importBtn));
    importForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const file = fileInput.files[0];
      if (!file) { importMsg.textContent = "Chọn file CSV trước."; return; }
      if (file.size > 5 * 1024 * 1024) { importMsg.textContent = "File lớn hơn 5 MB, hãy xuất theo khoảng thời gian ngắn hơn."; return; }
      await withBusy(importBtn, async () => {
        const text = await file.text();
        const r = await api("/orders/import", { method: "POST", body: { csv: text } });
        toast(`Đã cập nhật ${fmtNum(r.imported)} đơn, ${fmtNum(r.matched)} đơn khớp mã khách`);
        route();
      });
    });

    const cards = [importForm];
    if (data.can_sync) {
      const syncBtn = h("button", { class: "btn btn-primary", type: "button" }, icon("refresh"), "Đồng bộ ngay");
      syncBtn.addEventListener("click", () => withBusy(syncBtn, async () => {
        const r = await api("/orders/sync", { method: "POST" });
        toast(`Đã đồng bộ ${fmtNum(r.imported)} đơn`);
        route();
      }));
      cards.unshift(h("section", { class: "card" },
        h("h2", { text: "Đồng bộ từ Shopee Open API" }),
        h("p", { class: "muted", text: "Bot tự đồng bộ đơn 30 phút/lần. Bấm để lấy ngay." }),
        h("div", { class: "form-actions" }, syncBtn)));
    }
    cards.push(h("section", { class: "card" },
      h("h2", { text: "Trang tra đơn cho khách" }),
      data.lookup_page_url
        ? h("p", {}, h("a", { href: data.lookup_page_url, target: "_blank", rel: "noopener noreferrer", text: data.lookup_page_url }))
        : h("p", { class: "muted" }, "Chưa đặt LOOKUP_PAGE_URL. Trang nằm ở đường dẫn ", h("code", { text: "/tra-don/" }), " trên cổng public."),
      h("p", { class: "muted", text: "Khách nhắn /id cho OA để nhận mã tra cứu và link trang này." }),
      h("p", { class: "muted", text: cb ? `Đang hiển thị tiền hoàn cho khách: ${cb}% hoa hồng.` : "Không hiển thị tiền hoàn cho khách (CASHBACK_PERCENT = 0)." })));

    const rows = data.items.map((o) => {
      const [label, kind] = ORDER_STATUS[o.status] || [o.status, ""];
      const first = o.items[0] ? o.items[0].name : "—";
      const more = o.items.length > 1 ? ` +${o.items.length - 1}` : "";
      return h("tr", {},
        h("td", {}, h("code", { text: o.order_id })),
        h("td", {}, o.code ? h("code", { text: o.code }) : h("span", { class: "muted", text: "Chưa khớp" })),
        h("td", {}, chip(label, kind)),
        h("td", { class: "num", text: o.purchase_ts ? fmtTime(o.purchase_ts) : "—" }),
        h("td", { class: "wrap", title: o.items.map((i) => i.name).join(", "), text: `${first}${more}` }),
        h("td", { class: "num", text: o.order_value === null ? "—" : `${fmtNum(o.order_value)} ₫` }),
        h("td", { class: "num", text: o.commission === null ? "—" : `${fmtNum(o.commission)} ₫` }),
        cb ? h("td", { class: "num", text: o.status === "cancelled" || o.commission === null ? "—" : `${fmtNum(Math.floor(o.commission * cb / 100))} ₫` }) : null,
        h("td", { text: ORDER_SOURCE[o.source] || o.source || "—" }));
    });
    const headers = ["Mã đơn", "Mã khách", "Trạng thái", "Đặt lúc", "Sản phẩm", "Giá trị", "Hoa hồng", cb ? "Hoàn cho khách" : null, "Nguồn"].filter(Boolean);

    setView(
      head("Đơn hàng", last ? `Cập nhật lần cuối ${fmtTime(last.ts)} từ ${ORDER_SOURCE[last.source] || last.source}.` : "Chưa có dữ liệu đơn hàng."),
      h("div", { class: "grid grid-kpi" },
        kpi("Tổng đơn", fmtNum(sm.total), `${fmtNum(sm.by_status.completed || 0)} hoàn thành · ${fmtNum(sm.by_status.cancelled || 0)} hủy`),
        kpi("Khách có đơn", fmtNum(sm.customers), "đơn khớp mã tra cứu"),
        kpi("Chưa khớp mã", fmtNum(sm.unmatched), "đơn từ link không có mã khách"),
        kpi("Hoa hồng", `${fmtNum(sm.commission)} ₫`, "không tính đơn đã hủy")),
      h("div", { class: "grid grid-2" }, cards),
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, headers.map((t) => h("th", { text: t })))),
        h("tbody", {}, rows.length ? rows : emptyRow(headers.length, "Chưa có đơn hàng. Tải file báo cáo hoặc đồng bộ để bắt đầu.")))));
  }

  // ── Việc bị lỗi ────────────────────────────────────────────────
  function renderDlq(data) {
    const clearBtn = h("button", { class: "btn btn-ghost", type: "button", disabled: !data.count }, icon("trash"), "Xóa danh sách");
    clearBtn.addEventListener("click", async () => {
      const ok = await confirmAction({
        title: "Xóa danh sách việc bị lỗi?",
        text: "Chỉ xóa danh sách để theo dõi, không ảnh hưởng tới khách. Khách đã được báo thử lại sau.",
        ok: "Xóa danh sách", danger: true,
      });
      if (!ok) return;
      await withBusy(clearBtn, async () => {
        await api("/dlq/clear", { method: "POST" });
        toast("Đã xóa danh sách việc bị lỗi");
        await refreshOverview();
        route();
      });
    });
    const rows = data.items.map((r) => h("tr", {},
      h("td", { class: "num", text: fmtTime(r.ts) }),
      h("td", {}, h("code", { text: shortHash(r.user_hash) })),
      h("td", { text: STAGE[r.stage] || r.stage }),
      h("td", { class: "wrap", text: errorLabel(r.error) })));
    setView(
      head("Việc bị lỗi", `${fmtNum(data.count)} việc bot chưa hoàn thành. Khách đã nhận tin “hệ thống đang bận, gửi lại sau”.`, clearBtn),
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["Thời gian", "Khách", "Bước", "Lý do"].map((t) => h("th", { text: t })))),
        h("tbody", {}, rows.length ? rows : emptyRow(4, "Không có việc nào bị lỗi.")))));
  }

  // ── Kết nối Shopee ─────────────────────────────────────────────
  function resetButton(adapterKey) {
    const btn = h("button", { class: "btn btn-ghost", type: "button" }, icon("refresh"), "Chạy lại kết nối");
    btn.addEventListener("click", async () => {
      const ok = await confirmAction({
        title: "Chạy lại kết nối Shopee?",
        text: "Bot sẽ thử đổi link lại ngay. Nếu lỗi do cookie hết hạn, hãy cập nhật cookie trước.",
        ok: "Chạy lại",
      });
      if (!ok) return;
      await withBusy(btn, async () => {
        await api(`/adapters/${adapterKey}/reset`, { method: "POST" });
        toast("Đã chạy lại kết nối Shopee");
        renderShopee(await refreshOverview());
      });
    });
    return btn;
  }

  function renderShopee(ov) {
    const mode = state.settings?.shopee_mode || (ov.adapters.shopee_openapi ? "openapi" : "web_session");
    if (mode === "openapi") {
      const a = ov.adapters.shopee_openapi || {};
      setView(
        head("Kết nối Shopee", "Bot đổi link qua Shopee Affiliate Open API."),
        h("section", { class: "card" },
          h("div", { class: "card-head" }, h("h2", { text: "Shopee · Open API" }), adapterChip(a)),
          a.reason ? h("p", { text: a.reason }) : null,
          h("p", { class: "muted", text: "Không cần cookie. Khóa API được lưu trong cấu hình máy chủ." }),
          h("div", { class: "form-actions" }, resetButton("shopee_openapi"))));
      return;
    }

    const a = ov.adapters.shopee_web || {};
    const sess = a.session || { configured: false };
    const statusCard = h("section", { class: "card" },
      h("div", { class: "card-head" }, h("h2", { text: "Trạng thái" }), adapterChip(a)),
      a.state === "stopped" ? h("p", { class: "form-error", text: `${a.reason}. Hãy đăng nhập lại Shopee và dán cookie mới bên dưới.` }) : null,
      h("dl", { class: "kv" },
        h("dt", { text: "Cookie" }), h("dd", { text: sess.configured ? "Đã nhập (được mã hóa, không hiển thị lại)" : "Chưa nhập" }),
        h("dt", { text: "Cập nhật lúc" }), h("dd", { text: sess.updated_at || "—" }),
        h("dt", { text: "Đã dùng" }), h("dd", {},
          sess.configured ? `${fmtDec(sess.age_days)} ngày ` : "—",
          sess.age_days > 7 ? chip("Nên thay mới", "warn") : null)),
      h("div", { class: "form-actions" }, resetButton("shopee_web")));

    const cookie = h("textarea", { id: "shopee-cookie", autocomplete: "off", spellcheck: "false", required: true,
      placeholder: "Dán giá trị header cookie ở đây", "aria-describedby": "cookie-help", maxlength: 8000 });
    const csrf = h("input", { id: "shopee-csrf", type: "text", autocomplete: "off", spellcheck: "false", maxlength: 200,
      placeholder: "Có thể để trống" });
    const submit = h("button", { class: "btn btn-primary", type: "submit" }, "Lưu cookie mới");
    const formError = h("p", { class: "form-error", role: "alert", hidden: true });
    const form = h("form", { class: "card", autocomplete: "off" },
      h("h2", { text: "Cập nhật cookie" }),
      h("label", { class: "field", for: "shopee-cookie" }, h("span", { class: "field-label", text: "Cookie affiliate.shopee.vn" })),
      cookie,
      h("span", { class: "field-help", id: "cookie-help",
        text: "Cookie có toàn quyền tài khoản affiliate. Bot mã hóa ngay khi lưu và không bao giờ hiển thị lại." }),
      h("label", { class: "field" }, h("span", { class: "field-label", text: "csrf-token (nếu có)" }), csrf),
      formError,
      h("div", { class: "form-actions" }, submit));
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const value = cookie.value.trim();
      formError.hidden = true;
      if (value.length < 20 || /[\r\n]/.test(value)) {
        formError.hidden = false;
        formError.textContent = "Cookie không hợp lệ: cần ít nhất 20 ký tự và nằm trên một dòng.";
        return;
      }
      const ok = await confirmAction({
        title: "Lưu cookie Shopee mới?",
        text: "Cookie cũ sẽ bị thay thế và bot dùng cookie mới ngay cho các link tiếp theo.",
        ok: "Lưu cookie",
      });
      if (!ok) return;
      await withBusy(submit, async () => {
        try {
          await api("/shopee-web-session", { method: "POST", body: { cookie: value, csrf_token: csrf.value.trim() } });
        } finally {
          cookie.value = "";
          csrf.value = "";
        }
        toast("Đã lưu cookie mới, bot đã chạy lại kết nối");
        renderShopee(await refreshOverview());
      });
    });

    const guide = h("section", { class: "card" },
      h("h2", { text: "Cách lấy cookie" }),
      h("ol", { class: "steps" },
        h("li", { text: "Đăng nhập affiliate.shopee.vn trên trình duyệt máy tính của bạn." }),
        h("li", { text: "Mở trang Custom Link, nhấn F12 để mở công cụ nhà phát triển, chọn thẻ Network." }),
        h("li", { text: "Dán một link sản phẩm bất kỳ và bấm “Lấy link”." }),
        h("li", { text: "Chọn dòng có tên batchCustomLink, tìm phần Request Headers." }),
        h("li", { text: "Sao chép giá trị của cookie (và csrf-token nếu có), dán vào biểu mẫu bên cạnh." })),
      h("p", { class: "note warn", text: "Đây là phương án tạm. Đăng ký Shopee Affiliate Open API để bot không phụ thuộc cookie và không phải cập nhật định kỳ." }));

    setView(
      head("Kết nối Shopee", "Bot đang dùng Phương án 2: đổi link qua phiên đăng nhập trang affiliate.shopee.vn."),
      statusCard,
      ov.browser ? browserCard(ov.browser) : null,
      h("div", { class: "grid grid-2" }, form, guide));
  }

  function browserCard(b) {
    const [label, kind] = browserResult(b.last_result);
    const openBtn = h("button", { class: "btn btn-primary", type: "button" }, "Mở Chrome đăng nhập Shopee");
    const syncBtn = h("button", { class: "btn btn-ghost", type: "button" }, icon("refresh"), "Lấy phiên ngay");
    openBtn.addEventListener("click", () => withBusy(openBtn, async () => {
      const r = await api("/shopee-browser/open", { method: "POST" });
      toast(r.browser.browser === "open"
        ? "Đã mở Chrome. Đăng nhập affiliate.shopee.vn trong cửa sổ đó, bot sẽ tự lấy phiên."
        : "Chưa thấy Chrome phản hồi, thử lại sau vài giây.");
      renderShopee(await refreshOverview());
    }));
    syncBtn.addEventListener("click", () => withBusy(syncBtn, async () => {
      const r = await api("/shopee-browser/sync", { method: "POST" });
      toast(browserResult(r.result)[0]);
      renderShopee(await refreshOverview());
    }));
    const time = (ts) => (ts ? new Date(ts * 1000).toLocaleString("vi-VN") : "—");
    return h("section", { class: "card" },
      h("div", { class: "card-head" }, h("h2", { text: "Tự lấy phiên từ Chrome" }), chip(label, kind)),
      h("p", { class: "muted", text: "Bot mở một cửa sổ Chrome riêng tới affiliate.shopee.vn. Bạn đăng nhập trong cửa sổ đó một lần; "
        + "mỗi phút bot tự đọc phiên và cập nhật khi phiên đổi. Giữ cửa sổ mở (thu nhỏ được), không đăng xuất." }),
      h("dl", { class: "kv" },
        h("dt", { text: "Chrome" }), h("dd", { text: b.browser === "open" ? "Đang mở" : b.browser === "error" ? "Không đọc được" : "Đang đóng" }),
        h("dt", { text: "Đăng nhập Shopee" }), h("dd", { text: b.logged_in ? "Đã đăng nhập" : "Chưa" }),
        h("dt", { text: "Kiểm tra lần cuối" }), h("dd", { text: time(b.checked_at) }),
        h("dt", { text: "Lấy phiên mới lúc" }), h("dd", { text: time(b.synced_at) })),
      h("p", { class: "note", text: "Bot không lưu mật khẩu và không tự đăng nhập. Nếu Shopee hỏi OTP hoặc captcha, bạn tự xử lý trong cửa sổ Chrome." }),
      h("div", { class: "form-actions" }, openBtn, syncBtn));
  }

  // ── Cài đặt & nhật ký ──────────────────────────────────────────
  function renderSettings(events) {
    const s = state.settings || {};
    const rl = s.rate_limits || {};
    const kv = (items) => h("dl", { class: "kv" }, items.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })]));
    const rows = events.items.map((e) => h("tr", {},
      h("td", { class: "num", text: fmtTime(e.ts) }),
      h("td", { text: ACTION[e.action] || e.action }),
      h("td", {}, h("code", { text: e.ip || "—" }))));
    setView(
      head("Cài đặt & nhật ký", "Muốn đổi các thông số này: sửa file cấu hình rồi khởi động lại bot."),
      h("div", { class: "grid grid-2" },
        h("section", { class: "card" }, h("h2", { text: "Đổi link" }), kv([
          ["Kết nối Shopee", s.shopee_mode === "openapi" ? "Open API" : "Phương án 2 (cookie)"],
          ["Kênh thống kê (sub_id)", s.sub_id_channel || "—"],
          ["Chiến dịch (sub_id)", s.sub_id_campaign || "Không đặt"],
          ["Giữ nhật ký", s.log_retention_days ? `${s.log_retention_days} ngày` : "—"],
          ["Lưu trạng thái", s.redis ? "Redis" : "Bộ nhớ trong (mất khi khởi động lại)"],
        ])),
        h("section", { class: "card" }, h("h2", { text: "Giới hạn chống spam" }), kv([
          ["Mỗi khách / phút", `${fmtNum(rl.per_user_per_min)} link`],
          ["Mỗi khách / ngày", `${fmtNum(rl.per_user_per_day)} link`],
          ["Toàn hệ thống / phút", `${fmtNum(rl.global_per_min)} link`],
          ["Số link tối đa trong 1 tin", fmtNum(rl.max_links_per_msg)],
          ["Phương án 2 / phút", `${fmtNum(rl.web_fallback_per_min)} link`],
        ]))),
      h("section", { class: "card" },
        h("h2", { text: "Nhật ký thao tác quản trị" }),
        h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, h("tr", {}, ["Thời gian", "Thao tác", "Địa chỉ IP"].map((t) => h("th", { text: t })))),
          h("tbody", {}, rows.length ? rows : emptyRow(3, "Chưa có thao tác nào."))))));
  }

  // ── Khởi động ─────────────────────────────────────────────────
  (async () => {
    try {
      const sess = await api("/session");
      if (!sess.login_enabled) {
        showLogin();
        $("login-setup").hidden = false;
        $("login-password").disabled = true;
        $("login-submit").disabled = true;
        return;
      }
      if (sess.authenticated) {
        state.csrf = sess.csrf_token;
        await enterShell();
      } else {
        showLogin();
      }
    } catch (err) {
      showLogin(err.message);
    }
  })();
})();
