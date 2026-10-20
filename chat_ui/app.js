// Khung chat thử bot — JS thuần, không thư viện ngoài, tuân thủ CSP (không inline script, không innerHTML từ dữ liệu)
"use strict";

const SAMPLES = [
  { title: "Lấy mã tra cứu đơn", text: "/id" },
  { title: "Link rút gọn Shopee", text: "Shop ơi đổi giúp mình link này https://s.shopee.vn/AbC123 nhé" },
  { title: "Link đầy đủ Shopee", text: "https://shopee.vn/Tai-nghe-bluetooth-i.555444.2223334" },
  { title: "Một link thật + một trang lừa đảo", text: "https://s.shopee.vn/XyZ789 và https://shopee-giam-gia.evil.com/i.1.2" },
  { title: "Link độc trỏ vào máy chủ (SSRF)", text: "https://s.shopee.vn/Evil00" },
  { title: "Link gắn mã affiliate người khác", text: "https://shopee.vn/product/123456/7891011?affiliate_id=NGUOIKHAC&sub_id=hack" },
  { title: "Tin không có link", text: "Shop còn hàng không ạ?" },
];
const STATUS_LABEL = { ok: "thành công", cached: "từ cache", rejected: "từ chối", failed: "lỗi", rate_limited: "quá hạn mức" };

const $ = (id) => document.getElementById(id);
const messages = $("messages"), form = $("chat-form"), input = $("text"), sendBtn = $("send");
let productCounter = 0;

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

// Chuyển URL trong tin thành thẻ <a>, phần còn lại là chữ thường
function linkify(text) {
  const frag = document.createDocumentFragment();
  const re = /https?:\/\/[^\s<>"]+/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    frag.append(text.slice(last, m.index));
    const a = el("a", null, m[0]);
    a.href = m[0];
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    frag.append(a);
    last = m.index + m[0].length;
  }
  frag.append(text.slice(last));
  return frag;
}

function addMessage(who, text, metaNode) {
  const li = el("li", who);
  const bubble = el("div", "bubble");
  bubble.append(linkify(text));
  li.append(bubble);
  if (metaNode) li.append(metaNode);
  messages.append(li);
  messages.scrollTop = messages.scrollHeight;
  return li;
}

function addSystem(text, warn) {
  const li = el("li", warn ? "sys warn" : "sys", text);
  messages.append(li);
  messages.scrollTop = messages.scrollHeight;
}

function now() {
  return new Date().toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit" });
}

function currentUser() {
  return document.querySelector('input[name="user"]:checked').value;
}

async function send(text) {
  const fake = $("fake-sig").checked;
  const meta = el("div", "meta", now() + (fake ? " · " : ""));
  if (fake) meta.append(el("span", "bad", "chữ ký giả"));
  addMessage("me", text, meta);
  const typing = addMessage("bot typing", "Bot đang xử lý…");
  sendBtn.disabled = true;
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, user: currentUser(), fake_signature: fake }),
    });
    const data = await res.json();
    typing.remove();
    if (!res.ok) {
      addSystem(data.detail || `Lỗi máy chủ (${res.status})`, true);
      return;
    }
    if (data.webhook_status !== 200) {
      addSystem(`Webhook trả HTTP ${data.webhook_status}: bot từ chối tin này (chữ ký không hợp lệ).`, true);
    }
    for (const r of data.replies) addMessage("bot", r, el("div", "meta", now()));
    if (data.webhook_status === 200 && !data.replies.length) addSystem("Bot không trả lời tin này.");
  } catch {
    typing.remove();
    addSystem("Không kết nối được tới server. Kiểm tra lệnh python -m app.chatsim còn chạy không.", true);
  } finally {
    sendBtn.disabled = false;
    input.focus();
    refreshInfo();
  }
}

async function refreshInfo() {
  try {
    const res = await fetch("/api/info");
    if (!res.ok) return;
    const info = await res.json();
    $("limit").textContent = info.per_user_per_min;
    const s = info.stats;
    $("stats").textContent = s.total
      ? `${s.total} lượt · thành công ${s.success} (${s.success_rate}%) · từ cache ${s.cache_hits}` +
        (info.counters.webhook_bad_signature ? ` · chặn ${info.counters.webhook_bad_signature} chữ ký giả` : "")
      : "Chưa có lượt nào.";
    const log = $("log");
    log.replaceChildren(...info.recent.map((r) => {
      const li = el("li");
      li.append(el("code", null, r.item_key || r.error_code || "—"), el("span", `chip ${r.status}`, STATUS_LABEL[r.status] || r.status));
      return li;
    }));
  } catch { /* server tắt: giữ nguyên số liệu cũ */ }
}

function renderSamples() {
  const box = $("samples");
  const items = [...SAMPLES, { title: "Link sản phẩm mới (thử hạn mức)", text: null }];
  for (const s of items) {
    const b = el("button");
    b.type = "button";
    b.append(el("b", null, s.title));
    if (s.text) b.append(el("small", null, s.text));
    b.addEventListener("click", () => {
      // Mỗi lần bấm một sản phẩm khác nhau, để không trúng cache và chạm hạn mức nhanh
      send(s.text || `https://shopee.vn/product/1/${++productCounter}`);
    });
    box.append(b);
  }
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || sendBtn.disabled) return;
  input.value = "";
  autoGrow();
  send(text);
});

// Enter gửi, Shift+Enter xuống dòng
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    form.requestSubmit();
  }
});

function autoGrow() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 140) + "px";
}
input.addEventListener("input", autoGrow);

document.querySelectorAll('input[name="user"]').forEach((r) => r.addEventListener("change", () => {
  $("as-user").textContent = `Bạn đang chat với tư cách ${currentUser()}`;
  addSystem(`Đã chuyển sang ${r.parentElement.textContent.trim()} (${currentUser()})`);
}));
$("clear-btn").addEventListener("click", () => {
  messages.replaceChildren();
  addSystem("Đã xóa khung chat.");
});
$("refresh-btn").addEventListener("click", refreshInfo);

renderSamples();
refreshInfo();
input.focus();
