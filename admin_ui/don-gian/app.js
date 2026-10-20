// Trang quản trị đơn giản — JS thuần, tuân thủ CSP, dùng chung API /admin/api/* với bản đầy đủ
"use strict";

const $ = (id) => document.getElementById(id);
const REFRESH_MS = 30000;
const LINK_STATUS = { ok: "Thành công", cached: "Thành công", rejected: "Từ chối", failed: "Lỗi", rate_limited: "Quá hạn mức" };
const ORDER_STATUS = { completed: "Hoàn thành", pending: "Đang chờ", cancelled: "Đã hủy" };
const ERROR_TEXT = {
  user_minute: "Khách gửi quá nhanh", user_day: "Khách vượt hạn mức ngày", global_minute: "Bot đang quá tải",
  host_not_allowed: "Link không phải Shopee", not_a_product: "Không phải link sản phẩm", ip_literal: "Link độc (trỏ vào IP)",
  no_link: "Tin không có link",
};
const ADAPTER_NAME ={ shopee_openapi: "Shopee Open API", shopee_web: "Shopee (cookie)" };
const money = new Intl.NumberFormat("vi-VN");

let csrf = null;
let timer = null;
let botEnabled = null;

// ── Tiện ích ─────────────────────────────────────────────────────
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}
const vnd = (n) => (n == null ? "–" : money.format(n) + " đ");
const dateOf = (ts) => (ts ? new Date(ts * 1000).toLocaleDateString("vi-VN") : "–");
const timeOf = (ts) => new Date(ts * 1000).toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit" });

let toastTimer;
function toast(text, bad) {
  const t = $("toast");
  t.textContent = text;
  t.className = bad ? "toast bad" : "toast";
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 3500);
}

class AuthError extends Error {}

async function api(path, body) {
  const opts = { method: body === undefined ? "GET" : "POST", headers: {}, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.headers["x-csrf-token"] = csrf || "";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch("/admin/api/" + path, opts);
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && path !== "login") {
    showLogin();
    throw new AuthError(data.detail);
  }
  if (!res.ok) throw new Error(data.detail || `Lỗi máy chủ (${res.status})`);
  return data;
}

// Chạy một thao tác từ nút: khóa nút, báo kết quả, tải lại số liệu
async function act(btn, fn, okText) {
  btn.disabled = true;
  try {
    await fn();
    if (okText) toast(okText);
    await load();
  } catch (e) {
    if (!(e instanceof AuthError)) toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

// ── Đăng nhập ────────────────────────────────────────────────────
function showLogin(setupNeeded) {
  clearInterval(timer);
  $("app").hidden = true;
  $("login-view").hidden = false;
  $("login-setup").hidden = !setupNeeded;
  $("login-password").focus();
}

function showApp() {
  $("login-view").hidden = true;
  $("app").hidden = false;
  load();
  clearInterval(timer);
  timer = setInterval(load, REFRESH_MS);
}

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("login-error");
  err.hidden = true;
  try {
    const data = await api("login", { password: $("login-password").value });
    csrf = data.csrf_token;
    $("login-password").value = "";
    showApp();
  } catch (ex) {
    err.textContent = ex.message;
    err.hidden = false;
  }
});

$("logout-btn").addEventListener("click", async () => {
  try { await api("logout", {}); } catch { /* phiên đã hết thì vẫn về màn đăng nhập */ }
  csrf = null;
  showLogin();
});

// ── Hiển thị ─────────────────────────────────────────────────────
function renderStatus(ov) {
  botEnabled = ov.bot_enabled;
  $("status-dot").className = "dot " + (botEnabled ? "on" : "off");
  $("status-title").textContent = botEnabled ? "Bot đang chạy" : "Bot đang tạm dừng";
  $("status-sub").textContent = botEnabled
    ? `Khách nhắn link vào Zalo OA sẽ được trả link affiliate ngay. Đang chờ xử lý: ${ov.queue_len}.`
    : "Bot không trả lời khách cho tới khi bạn bật lại.";
  const btn = $("toggle-btn");
  btn.disabled = false;
  btn.textContent = botEnabled ? "Tạm dừng bot" : "Bật bot";
  btn.className = "btn big " + (botEnabled ? "danger" : "go");
}

function todoItem(kind, text, btnText, onClick) {
  const li = el("li", kind);
  li.append(el("span", null, text));
  if (btnText) {
    const b = el("button", "btn small", btnText);
    b.type = "button";
    b.addEventListener("click", () => onClick(b));
    li.append(b);
  }
  return li;
}

function renderTodo(ov) {
  const items = [];
  for (const [name, a] of Object.entries(ov.adapters)) {
    if (!a.active) continue;
    const label = ADAPTER_NAME[name] || name;
    if (a.state === "stopped") {
      items.push(todoItem("bad", `${label} đã dừng (${a.reason}). Cập nhật kết nối Shopee ở cuối trang.`,
        "Thử lại", (b) => act(b, () => api(`adapters/${name}/reset`, {}), "Đã cho chạy lại")));
    } else if (a.state === "cooling") {
      items.push(todoItem("warn", `${label} đang tạm nghỉ do lỗi liên tiếp, tự chạy lại sau ${a.seconds_left} giây.`,
        "Chạy lại ngay", (b) => act(b, () => api(`adapters/${name}/reset`, {}), "Đã cho chạy lại")));
    }
    if (a.session && !a.session.configured) {
      items.push(todoItem("bad", "Chưa có cookie Shopee. Dán cookie ở mục Kết nối Shopee bên dưới."));
    } else if (a.session && a.session.age_days >= 7) {
      items.push(todoItem("warn", `Cookie Shopee đã dùng ${a.session.age_days} ngày, nên thay mới.`));
    }
  }
  if (ov.zalo_token && !ov.zalo_token.configured) {
    items.push(todoItem("bad", "Chưa có token Zalo OA (ZALO_REFRESH_TOKEN), bot chưa gửi được tin cho khách."));
  }
  if (ov.dlq_len > 0) {
    items.push(todoItem("warn", `${ov.dlq_len} tin không gửi được cho khách (lỗi Zalo hoặc Shopee).`,
      "Đã xem, xóa", (b) => {
        if (confirm("Xóa danh sách việc bị lỗi? Khách sẽ không được gửi lại tự động.")) {
          act(b, () => api("dlq/clear", {}), "Đã xóa danh sách lỗi");
        }
      }));
  }
  for (const a of ov.alerts.slice(0, 3)) items.push(todoItem("warn", `${dateOf(a.ts)} ${timeOf(a.ts)} · ${a.text}`));
  if (!items.length) items.push(todoItem("ok", "Mọi thứ đều ổn, không có việc gì cần làm."));
  $("todo").replaceChildren(...items);
}

function renderStats(today, orders) {
  $("s-total").textContent = today.total;
  $("s-rate").textContent = today.total ? today.success_rate + "%" : "–";
  $("s-users").textContent = today.unique_users;
  $("s-commission").textContent = vnd(orders.summary.commission);
}

function renderOrders(o) {
  const s = o.summary;
  const by = s.by_status || {};
  const last = s.last_sync ? ` · Cập nhật lần cuối ${dateOf(s.last_sync.ts)} ${timeOf(s.last_sync.ts)}` : "";
  $("orders-summary").textContent =
    `${s.total} đơn: ${by.completed || 0} hoàn thành, ${by.pending || 0} đang chờ, ${by.cancelled || 0} đã hủy${last}`;
  $("sync-btn").hidden = !o.can_sync;
  const rows = o.items.slice(0, 10).map((x) => {
    const tr = el("tr");
    const st = el("td");
    st.append(el("span", "chip " + x.status, ORDER_STATUS[x.status] || x.status));
    tr.append(el("td", null, x.order_id), el("td", null, dateOf(x.purchase_ts)),
      el("td", "num", vnd(x.order_value)), el("td", "num", vnd(x.commission)), st);
    return tr;
  });
  if (!rows.length) {
    const tr = el("tr");
    const td = el("td", "empty", "Chưa có đơn nào. Tải file CSV báo cáo từ affiliate.shopee.vn lên để xem.");
    td.colSpan = 5;
    tr.append(td);
    rows.push(tr);
  }
  $("orders").replaceChildren(...rows);
}

function renderHistory(items) {
  const rows = items.slice(0, 12).map((r) => {
    const li = el("li");
    const t = el("time", null, timeOf(r.ts));
    const what = r.item_key || ERROR_TEXT[r.error_code] || r.error_code || "Tin không có link";
    li.append(t, el(r.item_key ? "code" : "span", null, what),
      el("span", "chip " + r.status, LINK_STATUS[r.status] || r.status));
    return li;
  });
  if (!rows.length) rows.push(el("li", "hint", "Chưa có khách nào gửi link."));
  $("history").replaceChildren(...rows);
}

function renderShopee(settings, ov) {
  const web = ov.adapters.shopee_web;
  if (settings.shopee_mode === "openapi") {
    $("shopee-mode").textContent = "Đang dùng Shopee Affiliate Open API (Phương án 1). Không cần làm gì thêm.";
  } else {
    const age = web && web.session && web.session.configured
      ? `Cookie hiện tại cập nhật lúc ${web.session.updated_at} (${web.session.age_days} ngày trước).`
      : "Chưa có cookie.";
    $("shopee-mode").textContent = `Đang dùng cookie affiliate.shopee.vn (Phương án 2). ${age}`;
  }
  $("cookie-form").hidden = !web;
}

async function load() {
  try {
    const [ov, conv, orders, settings] = await Promise.all([
      api("overview"), api("conversions?limit=12"), api("orders?limit=10"), api("settings"),
    ]);
    renderStatus(ov);
    renderTodo(ov);
    renderStats(ov.today, orders);
    renderOrders(orders);
    renderHistory(conv.items);
    renderShopee(settings, ov);
    $("updated").textContent = "Cập nhật " + new Date().toLocaleTimeString("vi-VN");
  } catch (e) {
    if (!(e instanceof AuthError)) toast("Không tải được dữ liệu: " + e.message, true);
  }
}

// ── Thao tác ─────────────────────────────────────────────────────
$("refresh-btn").addEventListener("click", (e) => act(e.currentTarget, async () => {}, "Đã làm mới"));

$("toggle-btn").addEventListener("click", (e) => {
  const enable = !botEnabled;
  if (!enable && !confirm("Tạm dừng bot? Khách nhắn link sẽ không được trả lời cho tới khi bật lại.")) return;
  act(e.currentTarget, () => api("bot", { enabled: enable }), enable ? "Đã bật bot" : "Đã tạm dừng bot");
});

$("sync-btn").addEventListener("click", (e) => act(e.currentTarget, async () => {
  const r = await api("orders/sync", {});
  toast(`Đã đồng bộ ${r.imported} đơn`);
}));

$("csv-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  if (file.size > 4.5 * 1024 * 1024) return toast("File quá lớn (tối đa khoảng 4,5 MB)", true);
  const label = document.querySelector('label[for="csv-file"]');
  label.textContent = "Đang tải…";
  try {
    const r = await api("orders/import", { csv: await file.text() });
    toast(`Đã nhập ${r.imported} đơn, khớp khách ${r.matched} đơn`);
    await load();
  } catch (ex) {
    if (!(ex instanceof AuthError)) toast(ex.message, true);
  } finally {
    label.textContent = "Tải file CSV";
  }
});

$("cookie-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const cookie = $("cookie").value.trim();
  if (cookie.length < 20) return toast("Cookie quá ngắn, hãy dán đầy đủ", true);
  act(e.submitter, async () => {
    await api("shopee-web-session", { cookie, csrf_token: $("csrf").value.trim() });
    $("cookie").value = "";
    $("csrf").value = "";
  }, "Đã lưu cookie mới");
});

// ── Khởi động ────────────────────────────────────────────────────
(async () => {
  try {
    const s = await api("session");
    if (s.authenticated) {
      csrf = s.csrf_token;
      showApp();
    } else {
      showLogin(!s.login_enabled);
    }
  } catch {
    showLogin();
  }
})();
