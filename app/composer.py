"""
Soạn tin gửi khách từ mẫu cố định. Chỉ chèn link đã qua is_safe_output_link;
không bao giờ chèn lại nội dung hay link khách gửi.
"""

from .url_guard import is_safe_output_link

DISCLOSURE = "Đây là link tiếp thị liên kết của shop, giá và ưu đãi giữ nguyên như trên sàn."

REJECT_TEXT = {
    "host_not_allowed": "không phải link Shopee hoặc TikTok Shop",
    "not_a_product": "không phải link một sản phẩm cụ thể",
    "too_long": "link quá dài",
    "tiktok_not_ready": "bot mới hỗ trợ Shopee, TikTok Shop sẽ sớm có",
}
DEFAULT_REJECT = "chưa đổi được, bạn kiểm tra lại link giúp shop"

NOTICES = {
    "help": "Bạn gửi link sản phẩm Shopee vào đây, shop sẽ gửi lại link mua hàng ngay. "
            "Nhắn /id để lấy mã tra cứu đơn hàng.",
    "rate_limited": "Bạn gửi hơi nhanh, vui lòng thử lại sau ít phút nhé.",
    "busy": "Hệ thống đang bận, bạn vui lòng gửi lại link sau ít phút nhé.",
    "paused": "Bot đang bảo trì, bạn vui lòng quay lại sau nhé.",
}


def lookup_text(code: str, lookup_url: str = "") -> str:
    """Tin trả lời lệnh /id: mã tra cứu và 3 bước tra đơn."""
    lines = [f"Mã tra cứu đơn hàng của bạn: {code}", ""]
    if lookup_url:
        lines += ["Cách tra đơn:",
                  f"1. Mở trang tra đơn: {lookup_url}",
                  "2. Nhập mã tra cứu ở trên (hoặc mã đơn hàng Shopee) rồi bấm Tra cứu",
                  "3. Xem các đơn bạn đã đặt qua link của shop", ""]
    lines.append("Mã này chỉ dành cho bạn, đừng chia sẻ cho người khác.")
    return "\n".join(lines)


def compose(results, lookup_code: str = ""):
    """results: danh sách (link_affiliate | None, mã_lỗi | None) theo thứ tự link khách gửi."""
    ok_links = [link for link, _ in results if link and is_safe_output_link(link)]
    multi = len(results) > 1
    lines = []
    if ok_links:
        lines.append("Link mua hàng của bạn đây:" if len(ok_links) == 1 else "Các link mua hàng của bạn đây:")
        lines.extend(f"• {link}" for link in ok_links)
    for idx, (link, error_code) in enumerate(results, 1):
        if link and is_safe_output_link(link):
            continue
        reason = REJECT_TEXT.get(error_code, DEFAULT_REJECT)
        lines.append(f"• Link thứ {idx}: {reason}." if multi else f"Link bạn gửi {reason}.")
    if ok_links:
        lines.append("")
        lines.append(DISCLOSURE)
        if lookup_code:
            lines.append(f"Đơn mua qua link trên sẽ hiện khi tra cứu bằng mã {lookup_code} (nhắn /id để xem cách tra).")
    return "\n".join(lines)


def notice(kind: str) -> str:
    return NOTICES[kind]
