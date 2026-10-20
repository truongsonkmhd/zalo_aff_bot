import tempfile
import unittest

from app import orders as o

KEY = "k" * 40
CSV_VI = (
    "﻿Mã đơn hàng,Trạng thái đặt hàng,Thời gian đặt hàng,Tên Item,Số lượng,Giá(₫),Hoa hồng ròng(₫),Sub_id1,Sub_id2,Sub_id3\n"
    "241018ABCD1234,Đang chờ xử lý,2024-10-18 10:15:00,Áo thun nam,2,\"150.000\",\"9.000\",zalo,demo,KH{code}\n"
    "241018ABCD1234,Đang chờ xử lý,2024-10-18 10:15:00,Tất cổ ngắn,1,\"30.000\",\"1.500\",zalo,demo,KH{code}\n"
    "241019WXYZ9876,Hoàn thành,19/10/2024 08:00,Tai nghe,1,\"399.000\",\"20.000\",zalo,demo,\n"
    "khong-phai-ma-don,Hoàn thành,,X,1,1,1,,,\n"
)


class CodeTest(unittest.TestCase):
    def test_code_shape_and_privacy(self):
        c = o.customer_code("2578938647871172479", KEY)
        self.assertEqual(len(c), 10)
        self.assertTrue(set(c) <= set(o.CODE_ALPHABET))
        self.assertEqual(c, o.customer_code("2578938647871172479", KEY))          # ổn định
        self.assertNotEqual(c, o.customer_code("2578938647871172479", "z" * 40))  # phụ thuộc khóa bí mật
        self.assertNotIn("2578", c)

    def test_normalize_query(self):
        c = o.customer_code("u1", KEY)
        self.assertEqual(o.normalize_query(f" {c[:5].lower()}-{c[5:]} "), ("code", c))
        self.assertEqual(o.normalize_query("241018abcd1234"), ("order", "241018ABCD1234"))
        for bad in ("", "abc", "<script>", "x" * 50, "' OR 1=1 --"):
            self.assertEqual(o.normalize_query(bad), (None, None), bad)

    def test_find_code_only_with_prefix(self):
        c = o.customer_code("u1", KEY)
        self.assertEqual(o.find_code(["zalo", "SUMMERSALE", f"KH{c}"]), c)
        self.assertEqual(o.find_code(["zalo-demo-KH" + c]), c)
        self.assertEqual(o.find_code(["SUMMERSALE", "ABCDEFGHJK"]), "")       # tên chiến dịch không bị nhầm


class CsvTest(unittest.TestCase):
    def test_vietnamese_export_aggregates_items(self):
        c = o.customer_code("u1", KEY)
        found, info = o.parse_conversion_csv(CSV_VI.format(code=c))
        by_id = {x["order_id"]: x for x in found}
        self.assertEqual(info["skipped"], 1)
        a = by_id["241018ABCD1234"]
        self.assertEqual(a["code"], c)
        self.assertEqual(a["status"], "pending")
        self.assertEqual(len(a["items"]), 2)
        self.assertEqual(a["order_value"], 150000 * 2 + 30000)
        self.assertEqual(a["commission"], 10500)
        self.assertIsNotNone(a["purchase_ts"])
        self.assertEqual(by_id["241019WXYZ9876"]["status"], "completed")
        self.assertEqual(by_id["241019WXYZ9876"]["code"], "")

    def test_english_headers_and_semicolon(self):
        text = "Order id;Order Status;Purchase Time;Item Name;Qty;Item Price;Net Commission;Sub_id\n" \
               "2410AAAA1111;Cancelled;1729240000;Bag;1;100000;5000;KHABCDEFGHJK\n"
        found, _ = o.parse_conversion_csv(text)
        self.assertEqual(found[0]["status"], "cancelled")
        self.assertEqual(found[0]["code"], "ABCDEFGHJK")
        self.assertEqual(found[0]["purchase_ts"], 1729240000)

    def test_missing_columns_reported(self):
        _, info = o.parse_conversion_csv("Tên,Giá\nA,1\n")
        self.assertIn("order_id", info["missing"])


class ApiNodesTest(unittest.TestCase):
    def test_parse_nodes(self):
        nodes = [{"purchaseTime": 1729240000, "utmContent": "zalo-demo-KHABCDEFGHJK",
                  "orders": [{"orderId": "2410AAAA1111", "orderStatus": "COMPLETED",
                              "items": [{"itemName": "Bag", "itemPrice": "100000", "qty": 2, "itemTotalCommission": "8000"}]}]}]
        [x] = o.parse_conversion_nodes(nodes)
        self.assertEqual((x["code"], x["status"], x["order_value"], x["commission"]), ("ABCDEFGHJK", "completed", 200000, 8000))


class StoreTest(unittest.TestCase):
    def test_upsert_keeps_code_and_updates_status(self):
        with tempfile.TemporaryDirectory() as d:
            st = o.OrderStore(f"{d}/o.db")
            base = {"order_id": "2410AAAA1111", "status": "pending", "purchase_ts": 1, "items": [], "order_value": 1, "commission": 1}
            st.upsert([{**base, "code": "ABCDEFGHJK"}], "csv")
            st.upsert([{**base, "code": "", "status": "completed"}], "csv")   # file sau thiếu sub_id
            [row] = st.by_code("ABCDEFGHJK")
            self.assertEqual(row["status"], "completed")
            self.assertEqual(st.summary()["customers"], 1)
            st.close()

    def test_public_view_hides_internal_fields(self):
        order = {"order_id": "X1234567", "code": "ABCDEFGHJK", "status": "completed", "purchase_ts": 1,
                 "items": [{"name": "A", "qty": 1}], "order_value": 100000, "commission": 8000, "source": "csv"}
        v = o.public_view(order, 50)
        self.assertNotIn("code", v)
        self.assertNotIn("commission", v)
        self.assertEqual(v["cashback"], 4000)
        self.assertNotIn("cashback", o.public_view(order, 0))
        self.assertNotIn("cashback", o.public_view({**order, "status": "cancelled"}, 50))


if __name__ == "__main__":
    unittest.main()
