"""HTTP 接口：产权方、两家经办、出纳与资金方按角色办理。

只监听回环地址；鉴权用 ``X-Actor`` 头标识经办方/角色账号。所有金额为分。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .errors import Forbidden, Invalid, NotFound, SiteBooksError
from .feed import PartnerClient, PartnerError
from .ledger import SiteBooks


def _int(value, name, default=None):
    if value in (None, ""):
        if default is None:
            raise Invalid(f"缺少参数 {name}", param=name)
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise Invalid(f"参数 {name} 应为整数", param=name) from exc


def _first(query, name, default=None):
    return query.get(name, [default])[0]


class Api:
    def __init__(self, books: SiteBooks, client: PartnerClient | None = None):
        self.books = books
        self.client = client

    # routing table: (method, regex, handler)
    def dispatch(self, method, path, query, body, actor):
        routes = [
            ("GET", "/health", self.health),
            ("GET", "/parties", self.list_parties),
            ("POST", "/parties", self.create_party),
            ("GET", "/register", self.get_register),
            ("POST", "/register/import", self.import_register),
            ("POST", "/intake/pull", self.pull_intake),
            ("POST", "/intake", self.push_intake),
            ("GET", "/intake", self.list_intake),
            ("GET", "/works", self.list_works),
            ("GET", "/works/{work}", self.get_work),
            ("POST", "/works", self.create_work),
            ("POST", "/works/{work}/accept", self.accept_work),
            ("POST", "/works/{work}/take", self.take_work),
            ("POST", "/works/{work}/deliver", self.deliver_work),
            ("POST", "/works/{work}/parts", self.register_part),
            ("POST", "/works/{work}/circuit-proof", self.apply_circuit_proof),
            ("GET", "/works/{work}/allocations", self.work_allocation),
            ("POST", "/works/{work}/allocations", self.propose_allocation),
            ("POST", "/allocations/{id}/confirm", self.confirm_allocation),
            ("GET", "/invoices/{invoice}", self.invoice),
            ("GET", "/invoices/{invoice}/trail", self.invoice_trail),
            ("POST", "/invoices/{invoice}/lines", self.add_line),
            ("POST", "/invoices/{invoice}/lines/{line}/hold", self.hold_line),
            ("POST", "/invoices/{invoice}/lines/{line}/attribute", self.attribute_line),
            ("POST", "/invoices/{invoice}/lines/{line}/withdraw", self.withdraw_line),
            ("DELETE", "/invoices/{invoice}/lines/{line}", self.withdraw_line),
            ("POST", "/charges/prepare", self.prepare_charges),
            ("GET", "/charges", self.list_charges),
            ("GET", "/charges/{id}", self.get_charge),
            ("GET", "/submissions", self.list_submissions),
            ("POST", "/submissions", self.create_submission),
            ("GET", "/submissions/{id}", self.get_submission),
            ("POST", "/submissions/{id}/confirm", self.confirm_submission),
            ("POST", "/submissions/{id}/reject", self.reject_submission),
            ("GET", "/claims", self.list_claims),
            ("POST", "/claims", self.create_claim),
            ("GET", "/claims/{id}", self.get_claim),
            ("POST", "/claims/{id}/confirm", self.confirm_claim),
            ("POST", "/claims/{id}/reject", self.reject_claim),
            ("GET", "/budgets", self.list_budgets),
            ("PUT", "/budgets", self.set_budget),
            ("GET", "/payments", self.list_payments),
            ("POST", "/payments", self.reserve_payment),
            ("GET", "/payments/{ref}", self.get_payment),
            ("POST", "/payments/{ref}/sync", self.sync_payment),
            ("POST", "/payments/{ref}/release", self.release_payment),
            ("POST", "/payments/{ref}/assign", self.assign_payment),
            ("POST", "/payments/{ref}/settle", self.settle_payment),
            ("GET", "/reviews", self.list_reviews),
            ("POST", "/reviews", self.open_review),
            ("POST", "/reviews/{id}/apply", self.apply_review),
            ("POST", "/reviews/{id}/dismiss", self.dismiss_review),
            ("GET", "/adjustments", self.list_adjustments),
            ("POST", "/adjustments/{id}/settle", self.settle_adjustment),
            ("GET", "/handovers", self.list_handovers),
            ("POST", "/works/{work}/handovers", self.request_handover),
            ("GET", "/handovers/{id}", self.get_handover),
            ("POST", "/handovers/{id}/confirm", self.confirm_handover),
            ("GET", "/views/owner", self.view_owner),
            ("GET", "/views/party/{party}", self.view_party),
            ("GET", "/views/cashier", self.view_cashier),
            ("GET", "/reports/period/{period}", self.period_report),
            ("POST", "/periods/{period}/close", self.close_period),
            ("GET", "/events", self.list_events),
        ]
        for route_method, pattern, handler in routes:
            params = _match(pattern, path)
            if params is None:
                continue
            if route_method != method:
                continue
            if not actor and handler.__name__ != "health":
                raise Forbidden("缺少 X-Actor 身份")
            return handler(query, body, actor, **params)
        raise NotFound("接口不存在", method=method, path=path)

    # -------------------------------------------------------------- helpers
    def _actor(self, actor, *roles):
        if not actor:
            raise Forbidden("缺少 X-Actor 身份")
        self.books.party(actor)
        if roles:
            self.books.require_role(actor, *roles)
        return actor

    # --------------------------------------------------------------- basics
    def health(self, query, body, actor):
        return 200, {"status": "ok"}

    def list_parties(self, query, body, actor):
        return 200, {"parties": self.books.parties()}

    def create_party(self, query, body, actor):
        party = _require(body, "party")
        role = _require(body, "role")
        if party == actor:
            raise Forbidden("不能自行登记身份")
        self._actor(actor, "owner", "cashier")
        return 201, self.books.add_party(party, role, body.get("name"))

    def get_register(self, query, body, actor):
        register = {
            "sites": {row["id"]: {"owner": row["owner"],
                                  "operators": self.books.site(row["id"])["operators"]}
                      for row in self.books._rows(
                          "SELECT id, owner FROM sites ORDER BY id")},
            "works": {w["id"]: {"type": w["type"], "user": w["user"]}
                      for w in self.books.works()},
            "invoice": [
                {"id": r["invoice"], "line": r["line"], "work": r["work"],
                 "part": r["part"], "cents": r["cents"]}
                for r in self.books._rows(
                    "SELECT * FROM invoice_lines ORDER BY invoice, line")],
            "shares": {},
        }
        for work in register["works"]:
            allocation = self.books.current_allocation(work)
            if allocation and allocation["part"] is None:
                register["shares"][work] = allocation["shares"]
        return 200, register

    def import_register(self, query, body, actor):
        if body:
            register = body
        elif self.client:
            register = self.client.register()
        else:
            raise Invalid("缺少台账内容或对端地址")
        self.books.seed(register)
        return 200, self.get_register(query, None, actor)[1]

    # --------------------------------------------------------------- intake
    def pull_intake(self, query, body, actor):
        if not self.client:
            raise Invalid("未配置对端资料端")
        since = _int(_first(query, "since"), "since", 0)
        limit = _int(_first(query, "limit"), "limit", 50)
        try:
            page = self.client.intake(since=since, limit=limit)
        except PartnerError as exc:
            raise SiteBooksError(str(exc)) from exc
        applied = self.books.apply_intake(page.get("items") or [])
        return 200, {"cursor": page.get("cursor"), "applied": applied,
                     "records": self.books.intake_records()}

    def push_intake(self, query, body, actor):
        items = body.get("items") if isinstance(body, dict) else body
        if not isinstance(items, list):
            raise Invalid("items 应为数组")
        return 200, {"applied": self.books.apply_intake(items)}

    def list_intake(self, query, body, actor):
        return 200, {"records": self.books.intake_records()}

    # ---------------------------------------------------------------- works
    def list_works(self, query, body, actor):
        return 200, {"works": self.books.works()}

    def get_work(self, query, body, actor, work):
        return 200, self.books.work(work)

    def create_work(self, query, body, actor):
        self._actor(actor, "owner")
        return 201, self.books.add_work(
            _require(body, "work"), site=body.get("site", ""),
            type=body.get("type", "unknown"), user=body.get("user"))

    def accept_work(self, query, body, actor, work):
        return 200, self.books.accept_work(
            work, party=actor, part=body.get("part") if body else None)

    def take_work(self, query, body, actor, work):
        party = body.get("party") if body else None
        if party and party != actor:
            self._actor(actor, "owner", "cashier")
        else:
            party = actor
        return 200, self.books.take(work, party, part=body.get("part") if body else None)

    def deliver_work(self, query, body, actor, work):
        party = (body or {}).get("party") or actor
        return 200, self.books.deliver(work, party, part=(body or {}).get("part"))

    def register_part(self, query, body, actor, work):
        return 200, self.books.register_part(
            work, _require(body, "part"), usage=body.get("usage", "unknown"),
            user=body.get("user"), weight=body.get("weight", 1),
            cents=body.get("cents"))

    def apply_circuit_proof(self, query, body, actor, work):
        evidence = (body or {}).get("evidence")
        if evidence is None:
            if not self.client:
                raise Invalid("缺少 evidence 或对端资料端")
            try:
                evidence = self.client.receipt("circuit-proof", "utility", work)
            except SiteBooksError as exc:
                if exc.code == "receipt_temporarily_unavailable":
                    return 202, {"work": work, "status": "pending",
                                 "reason": exc.code}
                raise
        return 200, self.books.apply_circuit_proof(work, evidence)

    def work_allocation(self, query, body, actor, work):
        return 200, {"current": self.books.current_allocation(
            work, _first(query, "part"))}

    def propose_allocation(self, query, body, actor, work):
        return 201, self.books.propose_allocation(
            work, body.get("part"), _require(body, "shares"), actor)

    def confirm_allocation(self, query, body, actor, id):
        return 200, self.books.confirm_allocation(_int(id, "id"), actor)

    # ------------------------------------------------------------- invoices
    def invoice(self, query, body, actor, invoice):
        return 200, {"invoice": invoice, "lines": self.books.invoice_lines(invoice),
                     "charges": self.books.charges(invoice=invoice)}

    def add_line(self, query, body, actor, invoice):
        return 201, self.books.add_invoice_line(
            invoice, _require(body, "line"), work=body.get("work"),
            part=body.get("part"), cents=_require(body, "cents"),
            period=body.get("period"))

    def hold_line(self, query, body, actor, invoice, line):
        return 200, self.books.hold_line(invoice, line, (body or {}).get("reason"))

    def attribute_line(self, query, body, actor, invoice, line):
        return 200, self.books.attribute_line(
            invoice, line, _require(body, "work"), body.get("part"), party=actor)

    def withdraw_line(self, query, body, actor, invoice, line):
        return 200, self.books.withdraw_line(invoice, line, (body or {}).get("reason"))

    def prepare_charges(self, query, body, actor):
        body = body or {}
        return 200, self.books.prepare_charges(body.get("invoice"), body.get("line"))

    def list_charges(self, query, body, actor):
        return 200, {"charges": self.books.charges(
            bearer=_first(query, "bearer"), invoice=_first(query, "invoice"),
            line=_first(query, "line"), status=_first(query, "status"),
            work=_first(query, "work"))}

    def get_charge(self, query, body, actor, id):
        return 200, self.books.charge(_int(id, "id"))

    # ---------------------------------------------------------- submissions
    def list_submissions(self, query, body, actor):
        return 200, {"submissions": self.books.submissions(
            kind=_first(query, "kind"), status=_first(query, "status"))}

    def create_submission(self, query, body, actor):
        return 201, self.books.submit(
            _require(body, "kind"), actor, site=body.get("site"),
            work=body.get("work"), part=body.get("part"),
            payload=body.get("payload") or body.get("data"),
            idempotency_key=body.get("idempotency_key"))

    def get_submission(self, query, body, actor, id):
        return 200, self.books.submission(_int(id, "id"))

    def confirm_submission(self, query, body, actor, id):
        return 200, self.books.confirm_submission(_int(id, "id"), actor)

    def reject_submission(self, query, body, actor, id):
        return 200, self.books.reject_submission(
            _int(id, "id"), actor, (body or {}).get("reason"))

    # --------------------------------------------------------------- claims
    def list_claims(self, query, body, actor):
        return 200, {"claims": self.books.claims(
            bearer=_first(query, "bearer"), period=_first(query, "period"),
            status=_first(query, "status"))}

    def create_claim(self, query, body, actor):
        party = body.get("party") or actor
        if party != actor:
            self._actor(actor, "owner", "cashier")
        charge_ids = body.get("charge_ids")
        if charge_ids:
            charge_ids = [_int(v, "charge_id") for v in charge_ids]
        return 201, self.books.create_claim(
            party, period=body.get("period"), charge_ids=charge_ids,
            invoice=body.get("invoice"), line=body.get("line"),
            idempotency_key=body.get("idempotency_key"), note=body.get("note"))

    def get_claim(self, query, body, actor, id):
        return 200, self.books.claim(_int(id, "id"))

    def confirm_claim(self, query, body, actor, id):
        return 200, self.books.confirm_claim(_int(id, "id"), actor)

    def reject_claim(self, query, body, actor, id):
        return 200, self.books.reject_claim(
            _int(id, "id"), actor, (body or {}).get("reason"))

    # -------------------------------------------------------------- budgets
    def list_budgets(self, query, body, actor):
        period = _first(query, "period")
        if not period:
            raise Invalid("缺少参数 period")
        return 200, self.books.budget_state(period, _first(query, "bearer"))

    def set_budget(self, query, body, actor):
        self._actor(actor, "cashier", "owner")
        return 200, self.books.set_budget(
            _require(body, "period"), _require(body, "party"),
            _require(body, "cents"))

    # ------------------------------------------------------------- payments
    def list_payments(self, query, body, actor):
        return 200, {"payments": self.books.payments(
            period=_first(query, "period"), bearer=_first(query, "bearer"),
            status=_first(query, "status")),
            "pending": self.books.pending_payments(_first(query, "period"))}

    def reserve_payment(self, query, body, actor):
        self._actor(actor, "cashier")
        return 201, self.books.reserve_payment(
            _require(body, "ref"), claim_id=body.get("claim_id"),
            period=body.get("period"), desk=body.get("desk", "procurement"),
            amount_cents=body.get("amount_cents"),
            destination=body.get("destination", "operator"),
            bearer=body.get("bearer"), invoice=body.get("invoice"),
            line=body.get("line"), work=body.get("work"))

    def get_payment(self, query, body, actor, ref):
        return 200, self.books.payment(ref)

    def sync_payment(self, query, body, actor, ref):
        evidence = (body or {}).get("evidence")
        if evidence is None and self.client:
            payment = self.books.payment(ref)
            try:
                evidence = self.client.receipt("payment", "procurement", ref)
            except SiteBooksError as exc:
                if exc.code == "receipt_temporarily_unavailable":
                    return 202, self.books.mark_payment_pending(ref)
                raise
        return 200, self.books.sync_payment(ref, evidence or {})

    def release_payment(self, query, body, actor, ref):
        self._actor(actor, "cashier", "owner")
        return 200, self.books.release_payment(ref, (body or {}).get("reason"))

    def assign_payment(self, query, body, actor, ref):
        self._actor(actor, "cashier")
        return 200, self.books.assign_payment(
            ref, _int(body.get("claim_id"), "claim_id"),
            destination=body.get("destination"))

    def settle_payment(self, query, body, actor, ref):
        self._actor(actor, "cashier", "owner")
        return 200, self.books.settle_payment(ref, destination=(body or {}).get("destination"))

    # -------------------------------------------------- reviews/adjustments
    def list_reviews(self, query, body, actor):
        return 200, {"reviews": self.books.reviews(status=_first(query, "status"))}

    def open_review(self, query, body, actor):
        return 201, self.books.open_review(
            _require(body, "work"), body.get("part"),
            invoice=body.get("invoice"), line=body.get("line"),
            reason=body.get("reason"), party=actor)

    def apply_review(self, query, body, actor, id):
        return 200, self.books.apply_review(
            _int(id, "id"), actor, new_bearer=(body or {}).get("new_bearer"),
            reason=(body or {}).get("reason"))

    def dismiss_review(self, query, body, actor, id):
        return 200, self.books.dismiss_review(
            _int(id, "id"), actor, (body or {}).get("reason"))

    def list_adjustments(self, query, body, actor):
        return 200, {"adjustments": self.books.adjustments(
            status=_first(query, "status"), party=_first(query, "party"))}

    def settle_adjustment(self, query, body, actor, id):
        self._actor(actor, "cashier", "owner")
        body = body or {}
        return 200, self.books.settle_adjustment(
            _int(id, "id"), destination=body.get("destination", "operator"),
            payment_ref=body.get("payment_ref"))

    # ------------------------------------------------------------- handovers
    def list_handovers(self, query, body, actor):
        return 200, {"handovers": self.books.handovers(status=_first(query, "status"))}

    def request_handover(self, query, body, actor, work):
        party = (body or {}).get("party") or actor
        if party != actor:
            self._actor(actor, "owner", "cashier")
        return 201, self.books.request_handover(
            work, party, mode=(body or {}).get("mode", "transfer"),
            to_party=(body or {}).get("to_party"), part=(body or {}).get("part"),
            revision=(body or {}).get("revision"), period=(body or {}).get("period"))

    def get_handover(self, query, body, actor, id):
        return 200, self.books.handover(_int(id, "id"))

    def confirm_handover(self, query, body, actor, id):
        return 200, self.books.confirm_handover(_int(id, "id"), actor)

    # ----------------------------------------------------------------- views
    def view_owner(self, query, body, actor):
        self._actor(actor, "owner", "cashier")
        return 200, self.books.view_owner()

    def view_party(self, query, body, actor, party):
        if party != actor:
            self._actor(actor, "owner", "cashier", "funder")
        return 200, self.books.view_party(party)

    def view_cashier(self, query, body, actor):
        self._actor(actor, "cashier", "owner", "funder")
        return 200, self.books.view_cashier(_first(query, "period"))

    def invoice_trail(self, query, body, actor, invoice):
        self._actor(actor, "funder", "cashier", "owner")
        return 200, self.books.invoice_trail(invoice, _first(query, "line"))

    def period_report(self, query, body, actor, period):
        self._actor(actor, "cashier", "owner", "funder")
        return 200, self.books.period_report(period)

    def close_period(self, query, body, actor, period):
        return 200, self.books.close_period(period, actor)

    def list_events(self, query, body, actor):
        self._actor(actor, "owner", "cashier")
        return 200, {"events": self.books.events()}


def _require(body, key):
    if not isinstance(body, dict) or body.get(key) in (None, ""):
        raise Invalid(f"缺少字段 {key}", field=key)
    return body[key]


def _match(pattern, path):
    if pattern == path:
        return {}
    pattern_parts = pattern.strip("/").split("/")
    path_parts = path.strip("/").split("/")
    if len(pattern_parts) != len(path_parts):
        return None
    params = {}
    for expected, actual in zip(pattern_parts, path_parts):
        if expected.startswith("{") and expected.endswith("}"):
            params[expected[1:-1]] = actual
        elif expected != actual:
            return None
    return params


class Server(ThreadingHTTPServer):
    daemon_threads = True


def make_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, status, payload):
            raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise Invalid("请求体不是合法 JSON") from exc

        def _handle(self, method):
            url = urlsplit(self.path)
            actor = self.headers.get("X-Actor") or None
            try:
                body = self._body() if method in ("POST", "PUT", "PATCH") else {}
                status, payload = api.dispatch(method, url.path,
                                               parse_qs(url.query), body, actor)
            except SiteBooksError as exc:
                self._send(exc.status, exc.as_dict())
            except PartnerError as exc:
                self._send(502, {"error": "partner_error", "message": str(exc)})
            except Exception as exc:  # pragma: no cover - 最后兜底
                self._send(500, {"error": "internal", "message": str(exc)})
            else:
                self._send(status, payload)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

        def do_DELETE(self):
            self._handle("DELETE")

        def log_message(self, *args):
            pass

    return Handler


def serve(books, client=None, port=8770):
    api = Api(books, client)
    return Server(("127.0.0.1", port), make_handler(api))
