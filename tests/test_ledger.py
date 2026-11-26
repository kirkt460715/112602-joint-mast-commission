import json
import unittest
from pathlib import Path

from sitebooks import Conflict, Forbidden, Invalid, SiteBooks, split_amount, split_weighted
from sitebooks.ledger import SiteBooks as Books

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def register():
    return json.loads((SAMPLES / "register.json").read_text(encoding="utf-8"))


def intake():
    return json.loads((SAMPLES / "intake.json").read_text(encoding="utf-8"))


def books_with(*roles, shared_parts=False):
    books = SiteBooks()
    books.seed(register())
    for party, role in roles:
        books.add_party(party, role)
    books.prepare_charges()
    if shared_parts:
        books.register_part("feed-7", "outlet-east", usage="shared")
        books.register_part("feed-7", "outlet-west", usage="shared")
        books.prepare_charges()
    return books


class SplitTest(unittest.TestCase):
    def test_split_is_exact(self):
        parts = split_amount(240000, {"op-a": 6000, "op-b": 4000})
        self.assertEqual(parts, {"op-a": 144000, "op-b": 96000})
        parts = split_amount(1, {"op-a": 3333, "op-b": 3333, "op-c": 3334})
        self.assertEqual(sum(parts.values()), 1)
        weighted = split_weighted(100, {"a": 1, "b": 1, "c": 1})
        self.assertEqual(sum(weighted.values()), 100)
        self.assertEqual(weighted["a"], 34)

    def test_bad_shares_rejected(self):
        with self.assertRaises(Invalid):
            split_amount(100, {"op-a": 5000})


class RegisterTest(unittest.TestCase):
    def test_seed_and_prepare(self):
        books = books_with()
        charges = books.charges(invoice="inv-44", line="L1")
        self.assertEqual([(c["bearer"], c["cents"]) for c in charges],
                         [("op-a", 144000), ("op-b", 96000)])
        self.assertEqual(books.invoice_line("inv-44", "L2")["status"], "attributed")
        result = books.prepare_charges("inv-44", "L2")
        self.assertEqual(result["created"], 0)
        self.assertEqual([item["line"] for item in result["skipped"]], ["L2"])

    def test_seed_idempotent(self):
        books = books_with()
        books.seed(register())
        self.assertEqual(len(books.charges(invoice="inv-44", line="L1")), 2)

    def test_hold_unknown_line(self):
        books = books_with()
        line = books.hold_line("inv-44", "L2", "归属未定")
        self.assertEqual(line["status"], "held")
        self.assertIsNone(line["work"])
        with self.assertRaises(Conflict):
            books.create_claim("op-a", invoice="inv-44", line="L2")
        books.attribute_line("inv-44", "L2", "feed-8")
        self.assertEqual(books.invoice_line("inv-44", "L2")["status"], "attributed")


class ClaimTest(unittest.TestCase):
    def test_shared_claim_needs_both(self):
        books = books_with()
        claim = books.create_claim("op-a", invoice="inv-44", line="L1")
        self.assertEqual(claim["mode"], "bilateral")
        self.assertEqual(claim["required"], ["op-a", "op-b"])
        self.assertEqual(claim["status"], "pending_confirmation")
        claim = books.confirm_claim(claim["id"], "op-b")
        self.assertEqual(claim["status"], "approved")
        with self.assertRaises(Conflict):
            books.confirm_claim(claim["id"], "op-b")

    def test_dedicated_claim_is_single(self):
        books = books_with()
        books.add_invoice_line("inv-50", "L1", work="cab-1", cents=9000)
        books.prepare_charges("inv-50")
        charge = books.charges(invoice="inv-50", line="L1")[0]
        self.assertEqual((charge["bearer"], charge["basis"]), ("op-a", "dedicated"))
        claim = books.create_claim("op-a", charge_ids=[charge["id"]])
        self.assertEqual(claim["mode"], "single")
        self.assertEqual(claim["status"], "approved")
        self.assertEqual(claim["required"], ["op-a"])
        with self.assertRaises(Forbidden):
            books.create_claim("op-b", charge_ids=[charge["id"]])

    def test_same_share_not_claimed_twice(self):
        books = books_with()
        claim = books.create_claim("op-a", invoice="inv-44", line="L1")
        with self.assertRaises(Conflict):
            books.create_claim("op-a", invoice="inv-44", line="L1")
        with self.assertRaises(Forbidden):
            books.create_claim("op-b", charge_ids=claim["charges"][0]["id"])

    def test_idempotency(self):
        books = books_with()
        first = books.create_claim("op-a", invoice="inv-44", line="L1",
                                   idempotency_key="k1")
        again = books.create_claim("op-a", invoice="inv-44", line="L1",
                                   idempotency_key="k1")
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(len(books.claims()), 1)

    def test_reject_releases_share(self):
        books = books_with()
        claim = books.create_claim("op-a", invoice="inv-44", line="L1")
        books.reject_claim(claim["id"], "op-b", "金额待核")
        self.assertEqual(books.charges(invoice="inv-44", line="L1")[0]["status"], "open")
        again = books.create_claim("op-a", invoice="inv-44", line="L1")
        self.assertEqual(again["status"], "pending_confirmation")


class BudgetPaymentTest(unittest.TestCase):
    def setUp(self):
        self.books = books_with(("cash-1", "cashier"), ("fund-1", "funder"),
                                shared_parts=True)

    def approved_claim(self, party="op-a", part="outlet-west"):
        charge = [c for c in self.books.charges(invoice="inv-44", line="L1")
                  if c["bearer"] == party and c["part"] == part][0]
        claim = self.books.create_claim(party, charge_ids=[charge["id"]],
                                        period="2026-11")
        for name in claim["required"]:
            if name not in claim["confirmations"]:
                self.books.confirm_claim(claim["id"], name)
        return self.books.claim(claim["id"])

    def test_receipt_unavailable_keeps_budget(self):
        claim = self.approved_claim()
        self.books.set_budget("2026-11", "op-a", claim["amount_cents"] + 100)
        payment = self.books.reserve_payment("pay-14", claim_id=claim["id"])
        self.assertEqual(payment["status"], "reserved")
        pending = self.books.mark_payment_pending("pay-14")
        self.assertEqual(pending["status"], "reserved")
        state = self.books.budget_state("2026-11", "op-a")
        self.assertEqual(state["op-a"]["available_cents"], 100)
        with self.assertRaises(Conflict):
            self.books.reserve_payment("pay-15", period="2026-11", bearer="op-a",
                                       amount_cents=200)
        self.assertEqual([p["ref"] for p in self.books.pending_payments()], ["pay-14"])

    def test_amount_mismatch_stays_held(self):
        claim = self.approved_claim()
        self.books.reserve_payment("pay-14", claim_id=claim["id"])
        payment = self.books.sync_payment("pay-14", {"state": "paid",
                                                     "amount_cents": 1})
        self.assertEqual(payment["status"], "failed")
        self.assertIn("不一致", payment["last_error"])
        self.assertEqual(self.books.budget_state("2026-11")["op-a"]["committed_cents"],
                         claim["amount_cents"])

    def test_settle_and_trail(self):
        claim = self.approved_claim()
        self.books.reserve_payment("pay-14", claim_id=claim["id"])
        self.books.sync_payment("pay-14", {"state": "paid",
                                           "amount_cents": claim["amount_cents"]})
        payment = self.books.settle_payment("pay-14", destination="operator")
        self.assertEqual(payment["status"], "settled")
        self.assertEqual(self.books.claim(claim["id"])["status"], "settled")
        trail = self.books.invoice_trail("inv-44", "L1")
        settled = [m for m in trail["lines"][0]["movements"]
                   if m["settlements"]]
        self.assertEqual(settled[0]["settlements"][0]["destination"], "operator")
        self.assertEqual(trail["lines"][0]["work_detail"]["id"], "feed-7")

    def test_unassigned_payment_needs_assignment(self):
        claim = self.approved_claim()
        payment = self.books.record_submitted_payment("pay-14", invoice="inv-44",
                                                      line="L1", period="2026-11")
        self.assertEqual(payment["status"], "reserved")
        with self.assertRaises(Conflict):
            self.books.reserve_payment("pay-14b", claim_id=claim["id"])
        self.assertEqual(self.books.pending_payments()[0]["ref"], "pay-14")
        with self.assertRaises(Conflict):
            self.books.settle_payment("pay-14")
        self.books.sync_payment("pay-14", {"state": "paid",
                                           "amount_cents": claim["amount_cents"]})
        settled = self.books.assign_payment("pay-14", claim["id"])
        self.assertEqual(settled["status"], "settled")

    def test_close_period_blocks_pending(self):
        claim = self.approved_claim()
        self.books.reserve_payment("pay-14", claim_id=claim["id"])
        with self.assertRaises(Conflict):
            self.books.close_period("2026-11", "cash-1")
        self.books.release_payment("pay-14", "撤销")
        with self.assertRaises(Conflict):
            self.books.close_period("2026-11", "cash-1")
        self.books.reject_claim(claim["id"], "op-a", "取消")
        report = self.books.close_period("2026-11", "cash-1")
        self.assertEqual(report["period"], "2026-11")
        with self.assertRaises(Conflict):
            self.books.set_budget("2026-11", "op-a", 1)


class ReviewTest(unittest.TestCase):
    def test_shared_becomes_dedicated(self):
        books = books_with(("cash-1", "cashier"), shared_parts=True)
        review = books.open_review("feed-7", "outlet-east", invoice="inv-44",
                                   line="L1", party="op-a")
        self.assertEqual(review["new_bearer"], "op-a")
        applied = books.apply_review(review["id"], "cash-1")
        self.assertEqual(applied["status"], "applied")
        east = [c for c in books.charges(invoice="inv-44", line="L1")
                if c["part"] == "outlet-east"]
        self.assertEqual(sum(c["cents"] for c in east if c["status"] != "reversed"),
                         120000)
        self.assertEqual({c["bearer"] for c in east if c["status"] == "open"},
                         {"op-a"})

    def test_paid_share_becomes_adjustment(self):
        books = books_with(("cash-1", "cashier"), shared_parts=True)
        charge = [c for c in books.charges(invoice="inv-44", line="L1")
                  if c["bearer"] == "op-b" and c["part"] == "outlet-east"][0]
        claim = books.create_claim("op-b", charge_ids=[charge["id"]],
                                   period="2026-11")
        for name in claim["required"]:
            if name not in books.claim(claim["id"])["confirmations"]:
                books.confirm_claim(claim["id"], name)
        books.reserve_payment("payE", claim_id=claim["id"])
        books.sync_payment("payE", {"state": "paid", "amount_cents": 48000})
        books.settle_payment("payE")
        review = books.open_review("feed-7", "outlet-east", invoice="inv-44",
                                   line="L1", party="op-a")
        applied = books.apply_review(review["id"], "cash-1")
        self.assertEqual([(a["from_party"], a["to_party"], a["cents"])
                          for a in applied["adjustments"]],
                         [("op-b", "op-a", 48000)])
        original = books.charge(charge["id"])
        self.assertEqual(original["paid_cents"], 48000)
        self.assertEqual(original["status"], "settled")
        remaining = [c for c in books.charges(invoice="inv-44", line="L1")
                     if c["part"] == "outlet-east" and c["status"] == "open"]
        self.assertEqual([(c["bearer"], c["cents"]) for c in remaining],
                         [("op-a", 72000)])
        settled = books.settle_adjustment(applied["adjustments"][0]["id"],
                                          payment_ref="payA")
        self.assertEqual(settled["status"], "settled")

    def test_unpaid_claim_unwound(self):
        books = books_with(("cash-1", "cashier"), shared_parts=True)
        charge = [c for c in books.charges(invoice="inv-44", line="L1")
                  if c["bearer"] == "op-b" and c["part"] == "outlet-east"][0]
        claim = books.create_claim("op-b", charge_ids=[charge["id"]],
                                   period="2026-11")
        review = books.open_review("feed-7", "outlet-east", invoice="inv-44",
                                   line="L1", party="op-a")
        books.apply_review(review["id"], "cash-1")
        self.assertEqual(books.claim(claim["id"])["status"], "cancelled")
        self.assertEqual(books.charge(charge["id"])["status"], "reversed")


class HandoverTest(unittest.TestCase):
    def setUp(self):
        self.books = books_with(("cash-1", "cashier"))
        self.books.accept_work("cab-1")
        self.books.accept_work("feed-7")
        self.books.take("cab-1", "op-a")
        self.books.take("feed-7", "op-a")
        self.books.take("feed-7", "op-b")

    def test_withdraw_keeps_verified_for_other(self):
        self.books.deliver("cab-1", "op-a")
        handover = self.books.request_handover("cab-1", "op-a", mode="withdraw")
        self.assertTrue(handover["snapshot"]["delivered"])
        self.books.confirm_handover(handover["id"], "tower-a")
        applied = self.books.handover(handover["id"])
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(applied["result"]["delivered"], True)
        self.assertNotIn("cab-1", [w["id"] for w in self.books.view_party("op-a")["using"]])
        # 留在场内的一方仍可使用与争议无关的 feed-7。
        self.assertIn("feed-7", [w["id"] for w in self.books.view_party("op-b")["using"]])

    def test_transfer_to_other_operator(self):
        handover = self.books.request_handover(
            "feed-7", "op-b", mode="transfer", to_party="op-a")
        self.assertEqual(handover["required"], ["op-a", "op-b", "tower-a"])
        for party in handover["required"]:
            self.books.confirm_handover(handover["id"], party)
        applied = self.books.handover(handover["id"])
        self.assertEqual(applied["status"], "applied")
        allocation = self.books.current_allocation("feed-7")
        self.assertEqual(allocation["shares"], {"op-a": 10000})
        self.assertNotIn("feed-7", [w["id"] for w in self.books.view_party("op-b")["using"]])
        self.assertIn("feed-7", [w["id"] for w in self.books.view_party("op-a")["using"]])

    def test_undelivered_uses_revision(self):
        handover = self.books.request_handover(
            "feed-7", "op-b", mode="transfer", to_party="op-a",
            revision={"shares": {"op-a": 7000, "tower-a": 3000}})
        self.assertFalse(handover["snapshot"]["delivered"])
        for party in handover["required"]:
            self.books.confirm_handover(handover["id"], party)
        self.books.prepare_charges()
        self.assertEqual(self.books.current_allocation("feed-7")["shares"],
                         {"op-a": 7000, "tower-a": 3000})
        charges = self.books.charges(invoice="inv-44", line="L1")
        self.assertEqual({(c["bearer"], c["cents"]) for c in charges},
                         {("op-a", 168000), ("tower-a", 72000)})

    def test_transfer_requires_confirmation(self):
        handover = self.books.request_handover(
            "feed-7", "op-b", mode="transfer", to_party="op-a")
        self.books.confirm_handover(handover["id"], "tower-a")
        self.assertEqual(self.books.handover(handover["id"])["status"], "pending")
        with self.assertRaises(Forbidden):
            self.books.confirm_handover(handover["id"], "cash-1")


class IntakeTest(unittest.TestCase):
    def test_apply_in_order_and_idempotent(self):
        books = SiteBooks()
        books.seed(register())
        results = books.apply_intake(intake())
        self.assertTrue(all(item["status"] in ("applied", "deferred")
                            for item in results))
        again = books.apply_intake(intake())
        self.assertTrue(all(item["status"] == "duplicate" for item in again))
        self.assertEqual(books.work("cab-1")["status"], "taken")
        self.assertIn("op-a", books.work("feed-7")["takers"])

    def test_circuit_proof_opens_review_for_shared_history(self):
        books = SiteBooks()
        books.seed(register())
        books.prepare_charges()
        result = books.apply_circuit_proof("feed-7", {
            "points": ["outlet-east", "outlet-west"],
            "east_customer": "op-a",
            "west_usage": "joint",
        })
        parts = {item["part"]: item for item in result["parts"]}
        self.assertEqual(parts["outlet-east"]["usage"], "dedicated")
        self.assertEqual(parts["outlet-west"]["usage"], "shared")
        self.assertIsNotNone(parts["outlet-east"]["review"])

    def test_payment_fact_recorded_unassigned(self):
        books = SiteBooks()
        books.seed(register())
        books.apply_intake([{"sequence": 1, "source": "finance", "facts": {
            "payment": "pay-14", "line": "L1", "state": "submitted"}}])
        payment = books.payment("pay-14")
        self.assertEqual(payment["status"], "reserved")
        self.assertEqual(payment["unassigned"], 1)
        self.assertEqual(payment["invoice"], "inv-44")


class SubmissionTest(unittest.TestCase):
    def test_space_and_expense_need_confirmation(self):
        books = books_with()
        submission = books.submit(
            "space", "op-a", site="base-9", work="feed-7", part="outlet-north",
            payload={"usage": "shared", "weight": 2},
            idempotency_key="s1")
        self.assertEqual(submission["status"], "submitted")
        self.assertIn("op-b", submission["required"])
        books.confirm_submission(submission["id"], "tower-a")
        state = books.submission(submission["id"])
        self.assertEqual(state["status"], "submitted")
        books.confirm_submission(submission["id"], "op-b")
        state = books.submission(submission["id"])
        self.assertEqual(state["status"], "confirmed")
        part = books.part("feed-7", "outlet-north")
        self.assertEqual((part["usage"], part["weight"]), ("shared", 2))
        again = books.submit("space", "op-a", site="base-9", work="feed-7",
                             part="outlet-north", payload={"usage": "shared"},
                             idempotency_key="s1")
        self.assertEqual(again["id"], submission["id"])

    def test_expense_submission_registers_line(self):
        books = books_with()
        books.add_party("builder-1", "builder")
        with self.assertRaises(Conflict):
            books.submit("expense", "builder-1", work="feed-8",
                         payload={"invoice": "inv-45", "line": "L1",
                                  "cents": 50000})
        books.accept_work("feed-8")
        submission = books.submit(
            "expense", "builder-1", work="feed-8",
            payload={"invoice": "inv-45", "line": "L1", "cents": 50000,
                     "period": "2026-11"},
            idempotency_key="e1")
        for party in submission["required"]:
            if party not in books.submission(submission["id"])["confirmations"]:
                books.confirm_submission(submission["id"], party)
        line = books.invoice_line("inv-45", "L1")
        self.assertEqual((line["work"], line["cents"]), ("feed-8", 50000))

    def test_allocation_submission(self):
        books = books_with()
        submission = books.submit(
            "allocation", "op-a", work="feed-8",
            payload={"shares": {"op-a": 5000, "op-b": 5000}})
        self.assertEqual(submission["required"], ["op-a", "op-b"])
        for party in ("op-b",):
            books.confirm_submission(submission["id"], party)
        self.assertEqual(books.current_allocation("feed-8")["shares"],
                         {"op-a": 5000, "op-b": 5000})
        books.prepare_charges()
        self.assertEqual([(c["bearer"], c["cents"]) for c in
                          books.charges(invoice="inv-44", line="L2")],
                         [("op-a", 35000), ("op-b", 35000)])


if __name__ == "__main__":
    unittest.main()
