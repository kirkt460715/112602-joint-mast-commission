import json
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

from partner_feed import PartnerFeed
from sitebooks import PartnerClient, SiteBooks
from sitebooks.api import Api, Server, make_handler

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.feed = PartnerFeed(0)
        feed_thread = threading.Thread(target=self.feed.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        feed_thread.start()
        self.addCleanup(feed_thread.join, 2)
        self.addCleanup(self.feed.server_close)
        self.addCleanup(self.feed.shutdown)
        self.books = SiteBooks()
        self.books.add_party("tower-a", "owner")
        self.books.add_party("cash-1", "cashier")
        self.books.add_party("fund-1", "funder")
        client = PartnerClient(f"http://127.0.0.1:{self.feed.server_port}")
        self.server = Server(("127.0.0.1", 0), make_handler(Api(self.books, client)))
        api_thread = threading.Thread(target=self.server.serve_forever,
                                      kwargs={"poll_interval": 0.02}, daemon=True)
        api_thread.start()
        self.addCleanup(api_thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None, actor=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if actor:
            headers["X-Actor"] = actor
        raw = json.dumps(body).encode("utf-8") if body is not None else None
        try:
            connection.request(method, path, body=raw, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def bootstrap(self):
        self.assertEqual(self.call("POST", "/register/import", actor="tower-a")[0], 200)
        status, page = self.call("POST", "/intake/pull?limit=10", actor="tower-a")
        self.assertEqual(status, 200)
        self.assertEqual(page["cursor"], 4)
        return page

    def test_register_and_intake(self):
        page = self.bootstrap()
        self.assertEqual([item["status"] for item in page["applied"]],
                         ["applied"] * 4)
        status, works = self.call("GET", "/works", actor="tower-a")
        self.assertEqual(status, 200)
        statuses = {work["id"]: work["status"] for work in works["works"]}
        self.assertEqual(statuses["cab-1"], "taken")
        status, register = self.call("GET", "/register", actor="tower-a")
        self.assertEqual(register["shares"], {"feed-7": {"op-a": 6000, "op-b": 4000}})

    def test_claim_and_payment_flow(self):
        self.bootstrap()
        # 采购平台已报来 pay-14，但尚未明确归属，仍占用该发票行的预算。
        pending = self.call("GET", "/payments?period=2026-11", actor="cash-1")[1]
        self.assertEqual([p["ref"] for p in pending["pending"]], ["pay-14"])
        # 由 op-a 发起共享支出认领，需要 op-b 确认才获批。
        status, claim = self.call("POST", "/claims", {
            "period": "2026-11", "invoice": "inv-44", "line": "L1"},
            actor="op-a")
        self.assertEqual(status, 201)
        self.assertEqual(claim["mode"], "bilateral")
        self.assertEqual(claim["status"], "pending_confirmation")
        status, missing = self.call("POST", f"/claims/{claim['id']}/confirm",
                                    actor="tower-a")
        self.assertEqual(status, 403)
        status, claim = self.call("POST", f"/claims/{claim['id']}/confirm",
                                  actor="op-b")
        self.assertEqual(claim["status"], "approved")
        # 同一发票行已有归属未明的付款占用时，不能再出下一笔。
        status, blocked = self.call("POST", "/payments", {
            "ref": "pay-15", "claim_id": claim["id"]}, actor="cash-1")
        self.assertEqual(status, 409)
        self.assertEqual(blocked["error"], "conflict")
        # 对端首次返回 503：保持占用，不下放预算。
        status, pending = self.call("POST", "/payments/pay-14/sync", actor="cash-1")
        self.assertEqual(status, 202)
        self.assertEqual(pending["status"], "reserved")
        self.assertIn("receipt_temporarily_unavailable", pending["last_error"])
        # 第二次读取回执：金额一致，确认到账。
        status, confirmed = self.call("POST", "/payments/pay-14/sync", actor="cash-1")
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["amount_cents"], 144000)
        # 归属到认领并按份额结算。
        status, settled = self.call("POST", "/payments/pay-14/assign", {
            "claim_id": claim["id"], "destination": "op-a"}, actor="cash-1")
        self.assertEqual(status, 200)
        self.assertEqual(settled["status"], "settled")
        self.assertEqual(settled["claim_id"], claim["id"])
        trail = self.call("GET", "/invoices/inv-44/trail?line=L1", actor="fund-1")[1]
        legs = [leg for leg in trail["lines"][0]["movements"] if leg["settlements"]]
        self.assertEqual(legs[0]["settlements"][0]["destination"], "op-a")
        self.assertEqual(legs[0]["charge"]["bearer"], "op-a")

    def test_view_permissions(self):
        self.bootstrap()
        self.assertEqual(self.call("GET", "/views/party/op-a")[0], 403)
        status, view = self.call("GET", "/views/party/op-a", actor="op-a")
        self.assertEqual(status, 200)
        self.assertEqual({work["id"] for work in view["using"]}, {"cab-1", "feed-7"})
        self.assertEqual(self.call("GET", "/views/cashier", actor="op-a")[0], 403)
        self.assertEqual(self.call("GET", "/invoices/inv-44/trail", actor="op-a")[0], 403)

    def test_role_guard(self):
        self.bootstrap()
        self.assertEqual(self.call("POST", "/payments", {"ref": "x"},
                                   actor="op-a")[0], 403)
        self.assertEqual(self.call("GET", "/claims")[0], 403)


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.books = SiteBooks()
        self.books.seed(json.loads((SAMPLES / "register.json").read_text("utf-8")))
        self.books.prepare_charges()

    def test_simultaneous_claims_only_one_wins(self):
        barrier = threading.Barrier(2)
        outcomes = []

        def attempt():
            barrier.wait()
            try:
                outcomes.append(self.books.create_claim(
                    "op-a", invoice="inv-44", line="L1"))
            except Exception as exc:  # 冲突是预期结果
                outcomes.append(exc)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len([o for o in outcomes if not isinstance(o, Exception)]), 1)
        self.assertEqual(len(self.books.claims()), 1)

    def test_concurrent_payments_never_overcommit(self):
        self.books.add_party("cash-1", "cashier")
        self.books.set_budget("2026-11", "op-a", 100000)
        barrier = threading.Barrier(4)
        errors = []

        def attempt(index):
            barrier.wait()
            try:
                self.books.reserve_payment(
                    f"pay-{index}", period="2026-11", bearer="op-a",
                    amount_cents=60000)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        committed = self.books.budget_state("2026-11", "op-a")["op-a"]["committed_cents"]
        self.assertLessEqual(committed, 100000)
        self.assertEqual(len(errors), 3)


if __name__ == "__main__":
    unittest.main()
