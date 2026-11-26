"""跨角色、跨期的端到端场景，对应办公室关心的几类结账口径。"""

import json
import unittest
from pathlib import Path

from sitebooks import Conflict, SiteBooks

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def setup():
    books = SiteBooks()
    books.seed(json.loads((SAMPLES / "register.json").read_text("utf-8")))
    for party, role in (("cash-1", "cashier"), ("fund-1", "funder"),
                        ("build-1", "builder")):
        books.add_party(party, role)
    return books


class MixedTimingTest(unittest.TestCase):
    def setUp(self):
        self.books = setup()
        self.books.accept_work("cab-1")
        self.books.accept_work("feed-7")
        # op-a 先接地面机柜，op-b 仍在等进线。
        self.books.take("cab-1", "op-a")
        self.books.add_invoice_line("inv-60", "L1", work="cab-1", cents=88000,
                                    period="2026-11")

    def test_cabinet_settles_alone_but_shared_feed_waits(self):
        books = self.books
        books.prepare_charges("inv-60")
        cabinet = books.create_claim("op-a", invoice="inv-60", line="L1",
                                     period="2026-11")
        self.assertEqual((cabinet["mode"], cabinet["status"]), ("single", "approved"))
        books.reserve_payment("pay-60", claim_id=cabinet["id"])
        books.sync_payment("pay-60", {"state": "paid", "amount_cents": 88000})
        books.settle_payment("pay-60", destination="op-a")
        self.assertEqual(books.claim(cabinet["id"])["status"], "settled")
        # 另一家的共用进线尚未接用，认领必须双边确认。
        shared = books.create_claim("op-a", invoice="inv-44", line="L1",
                                    period="2026-11")
        self.assertEqual(shared["mode"], "bilateral")
        self.assertEqual(shared["status"], "pending_confirmation")
        self.assertIn(shared["id"],
                      [c["id"] for c in books.view_party("op-b")["to_confirm"]])
        self.assertEqual({w["id"] for w in books.view_party("op-a")["using"]},
                         {"cab-1"})

    def test_builder_cannot_report_unaccepted_work(self):
        books = self.books
        books.add_party("build-1", "builder")
        with self.assertRaises(Conflict):
            books.submit("expense", "build-1", work="feed-8",
                         payload={"invoice": "inv-61", "line": "L1",
                                  "cents": 5000})
        books.accept_work("feed-8")
        submission = books.submit(
            "expense", "build-1", work="feed-8",
            payload={"invoice": "inv-61", "line": "L1", "cents": 5000},
            idempotency_key="exp-1")
        self.assertEqual(submission["status"], "submitted")
        self.assertIn("tower-a", submission["required"])
        for party in submission["required"]:
            if party not in books.submission(submission["id"])["confirmations"]:
                books.confirm_submission(submission["id"], party)
        self.assertEqual(books.invoice_line("inv-61", "L1")["work"], "feed-8")


class DeliveredHandoverTest(unittest.TestCase):
    def setUp(self):
        self.books = setup()
        for work in ("feed-7",):
            self.books.accept_work(work)
        self.books.take("feed-7", "op-a")
        self.books.take("feed-7", "op-b")
        self.books.prepare_charges()
        self.books.add_party("cash-1", "cashier")

    def test_undelivered_transfer_uses_revision(self):
        books = self.books
        handover = books.request_handover(
            "feed-7", "op-b", mode="transfer", to_party="op-a",
            revision={"shares": {"op-a": 7000, "tower-a": 3000}},
            period="2026-11")
        self.assertFalse(handover["snapshot"]["delivered"])
        for party in handover["required"]:
            books.confirm_handover(handover["id"], party)
        books.prepare_charges()
        rows = books.charges(invoice="inv-44", line="L1")
        self.assertEqual({(r["bearer"], r["cents"]) for r in rows},
                         {("op-a", 168000), ("tower-a", 72000)})
        self.assertEqual(books.current_allocation("feed-7")["shares"],
                         {"op-a": 7000, "tower-a": 3000})

    def test_delivered_transfer_restores_and_bills_difference(self):
        books = self.books
        rows = books.charges(invoice="inv-44", line="L1")
        op_b = [r for r in rows if r["bearer"] == "op-b"][0]
        claim = books.create_claim("op-b", charge_ids=[op_b["id"]],
                                   period="2026-11")
        for party in claim["required"]:
            if party not in books.claim(claim["id"])["confirmations"]:
                books.confirm_claim(claim["id"], party)
        books.reserve_payment("pay-70", claim_id=claim["id"])
        books.sync_payment("pay-70", {"state": "paid", "amount_cents": op_b["cents"]})
        books.settle_payment("pay-70")
        books.deliver("feed-7", "op-a")
        handover = books.request_handover(
            "feed-7", "op-b", mode="transfer", to_party="op-a",
            period="2026-11")
        self.assertTrue(handover["snapshot"]["delivered"])
        for party in handover["required"]:
            books.confirm_handover(handover["id"], party)
        applied = books.handover(handover["id"])
        self.assertEqual(applied["status"], "applied")
        self.assertTrue(applied["result"]["restored"])
        self.assertEqual(applied["result"]["restored"][0]["claim_id"], claim["id"])
        # 已付部分保留原结算，转接差额另立。
        self.assertEqual([(a["from_party"], a["to_party"], a["cents"])
                          for a in applied["result"]["adjustments"]],
                         [("op-b", "op-a", op_b["cents"])])
        original = books.charge(op_b["id"])
        self.assertEqual((original["status"], original["paid_cents"]),
                         ("settled", op_b["cents"]))
        # 另一方仍能使用已验收、与争议无关的设施部位。
        self.assertEqual([w["id"] for w in books.view_party("op-a")["using"]],
                         ["feed-7"])


class InvoiceTrailTest(unittest.TestCase):
    def test_every_leg_is_traceable(self):
        books = setup()
        books.prepare_charges()
        claim = books.create_claim("op-a", invoice="inv-44", line="L1",
                                   period="2026-11")
        for party in claim["required"]:
            if party not in books.claim(claim["id"])["confirmations"]:
                books.confirm_claim(claim["id"], party)
        books.reserve_payment("pay-14", claim_id=claim["id"])
        books.sync_payment("pay-14", {"state": "paid",
                                      "amount_cents": claim["amount_cents"]})
        books.settle_payment("pay-14", destination="op-a")
        trail = books.invoice_trail("inv-44", "L1")
        line = trail["lines"][0]
        self.assertEqual(line["work_detail"]["id"], "feed-7")
        legs = {leg["charge"]["bearer"]: leg for leg in line["movements"]}
        self.assertEqual(legs["op-a"]["settlements"][0]["destination"], "op-a")
        self.assertEqual(legs["op-a"]["payments"][0]["ref"], "pay-14")
        self.assertEqual(legs["op-b"]["settlements"], [])
        self.assertEqual(line["total_charged"], 240000)
        self.assertEqual(line["total_paid"], 144000)

    def test_unknown_line_is_visible_but_not_claimable(self):
        books = setup()
        books.prepare_charges()
        books.hold_line("inv-44", "L2", "归属未定")
        with self.assertRaises(Conflict):
            books.create_claim("op-a", invoice="inv-44", line="L2")
        line = books.invoice_lines("inv-44")[1]
        self.assertEqual((line["status"], line["held_reason"]), ("held", "归属未定"))
        books.attribute_line("inv-44", "L2", "feed-8")
        submission = books.submit(
            "allocation", "op-a", work="feed-8",
            payload={"shares": {"op-a": 5000, "op-b": 5000}})
        for party in submission["required"]:
            if party not in books.submission(submission["id"])["confirmations"]:
                books.confirm_submission(submission["id"], party)
        books.prepare_charges("inv-44", "L2")
        self.assertEqual([(c["bearer"], c["cents"]) for c in
                          books.charges(invoice="inv-44", line="L2")],
                         [("op-a", 35000), ("op-b", 35000)])


if __name__ == "__main__":
    unittest.main()
