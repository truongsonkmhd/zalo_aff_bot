"use strict";
/* Trang tra đơn cho khách. Không dùng innerHTML; mã tra cứu chỉ gửi qua POST, không nằm trên URL. */
(() => {
  const $ = (id) => document.getElementById(id);
  const nf = new Intl.NumberFormat("vi-VN");
  const money = (v) => (v === null || v === undefined ? "—" : `${nf.format(v)} ₫`);
  const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString("vi-VN", {
    hour: "2-digit", minute: "2-digit", day: "2-digit", month: "2-digit", year: "numeric" }) : "Chưa rõ thời gian");

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, String(v));
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }

  function show(...nodes) {
    $("result").replaceChildren(...nodes.flat().filter(Boolean));
  }

  function copyButton(text) {
    const btn = h("button", { type: "button", class: "copy", text: "Sao chép" });
    btn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(text);
        btn.textContent = "Đã chép";
      } catch {
        btn.textContent = "Không chép được";
      }
      setTimeout(() => { btn.textContent = "Sao chép"; }, 1800);
    });
    return btn;
  }

  function orderCard(o, cashback) {
    return h("article", { class: "order" },
      h("div", { class: "order-top" },
        h("div", {},
          h("div", { class: "order-id" }, o.order_id, copyButton(o.order_id)),
          h("div", { class: "order-time", text: `Đặt lúc ${when(o.purchase_ts)}` })),
        h("span", { class: `chip ${o.status}`, text: o.status_label })),
      o.items.length
        ? h("ul", { class: "items" }, o.items.map((i) => h("li", {}, h("span", { text: i.name || "Sản phẩm" }), h("span", { text: `x${i.qty}` }))))
        : null,
      h("div", { class: "order-foot" },
        h("span", {}, "Giá trị đơn: ", h("b", { text: money(o.order_value) })),
        cashback && o.cashback !== undefined
          ? h("span", { class: "cashback" }, "Tiền hoàn dự kiến: ", h("b", { text: money(o.cashback) }))
          : null));
  }

  function render(data) {
    const orders = data.orders || [];
    if (!orders.length) {
      show(h("p", { class: "msg info", text: data.kind === "code"
        ? "Chưa có đơn nào gắn với mã này. Đơn thường hiện sau vài giờ kể từ lúc đặt, và chỉ các đơn mua qua link shop gửi trên Zalo mới được ghi nhận."
        : "Không tìm thấy đơn hàng này trong danh sách đơn mua qua link của shop." }));
      return;
    }
    const totalCashback = orders.reduce((s, o) => s + (o.cashback || 0), 0);
    show(
      h("div", { class: "summary" },
        h("strong", { text: `Tìm thấy ${nf.format(orders.length)} đơn` }),
        data.cashback_enabled ? h("span", {}, "Tổng tiền hoàn dự kiến: ", h("span", { class: "total", text: money(totalCashback) })) : null),
      orders.map((o) => orderCard(o, data.cashback_enabled)));
  }

  $("lookup-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const input = $("q");
    const q = input.value.replace(/[\s\-_.]/g, "").toUpperCase();
    if (!q) {
      show(h("p", { class: "msg err", role: "alert", text: "Nhập mã tra cứu hoặc mã đơn hàng." }));
      input.focus();
      return;
    }
    const btn = $("submit");
    btn.disabled = true;
    btn.textContent = "Đang tra…";
    try {
      const res = await fetch("/api/tra-cuu", {
        method: "POST", credentials: "omit", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ q }),
      });
      let data = null;
      try { data = await res.json(); } catch { /* không phải JSON */ }
      if (!res.ok) {
        show(h("p", { class: "msg err", role: "alert",
          text: (data && typeof data.detail === "string") ? data.detail : "Không tra cứu được, bạn thử lại sau nhé." }));
        return;
      }
      render(data);
    } catch {
      show(h("p", { class: "msg err", role: "alert", text: "Không kết nối được. Kiểm tra mạng rồi thử lại." }));
    } finally {
      btn.disabled = false;
      btn.textContent = "Tra cứu";
    }
  });
})();
