"""乡村共建站点的分阶段接用与共用支出认领领域服务。

记账约定
--------
* 金额全部以“分”保存；分摊比例以万分比（bps）保存，合计必须为 10000。
* 发票行经确认归属后，按“经批准的可用空间 + 分摊约定”拆成各负担方的
  独立份额（``charges``）。``(invoice, line, part, bearer, origin_group)``
  唯一，故同一发票行上同一负担方在同一来源批次只能取得一份份额。
* 认领（``claims``）只能绑定 ``open`` 的份额；付款再绑定认领中的份额。
  两层唯一约束使同时到达的两笔请求也只能有一笔占用同一份额。
* 单边结账（``mode='single'``）只适用于份额只牵涉一个负担方、且没有待
  复核或未结差额的情形；其余一律 ``bilateral``，需要相关方确认。
* 付款先占用预算（``reserved``），收到对端回执后才 ``confirmed``；回执
  未到或金额不符时继续占用预算，不放出下一笔。
* 复核/移交不改写历史：已交付并支付的部分保留原始份额、认领与结算，
  差异另立 ``adjustments``，后续以差额付款办结。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from .errors import Conflict, Forbidden, Invalid, NotFound
from .schema import SCHEMA

ROLES = ("owner", "operator", "cashier", "funder", "builder")
LIVE_PAYMENT = ("reserved", "confirmed")
OCCUPYING_PAYMENT = ("reserved", "confirmed", "settled", "failed")


def now_ms():
    return int(time.time() * 1000)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(value):
    return json.loads(value)


def parse_period(value):
    if value in (None, ""):
        return datetime.now(timezone.utc).strftime("%Y-%m")
    if not isinstance(value, str):
        raise Invalid("期间应为 YYYY-MM", period=value)
    try:
        datetime.strptime(value, "%Y-%m")
    except ValueError as exc:
        raise Invalid("期间应为 YYYY-MM", period=value) from exc
    return value


def split_amount(cents, shares):
    """按 bps 精确拆分，余数按最大余数法稳定分配。"""
    if not isinstance(cents, int) or cents < 0:
        raise Invalid("金额应为非负整数（分）", cents=cents)
    clean = {party: bps for party, bps in shares.items() if bps}
    if not clean:
        raise Invalid("分摊不能为空")
    total = sum(clean.values())
    if total != 10000:
        raise Invalid("分摊比例合计必须为 10000 bps", total=total)
    ordered = sorted(clean)
    result = {party: cents * clean[party] // 10000 for party in ordered}
    remainder = cents - sum(result.values())
    ranked = sorted(ordered, key=lambda p: (-(cents * clean[p] % 10000), p))
    for party in ranked[:remainder]:
        result[party] += 1
    return result


def split_weighted(cents, weights):
    """按整数权重精确拆分；weights 为 {key: 正整数}。"""
    if not isinstance(cents, int) or cents < 0:
        raise Invalid("金额应为非负整数（分）", cents=cents)
    if not weights or any(not isinstance(w, int) or w <= 0 for w in weights.values()):
        raise Invalid("权重应为正整数")
    total = sum(weights.values())
    keys = sorted(weights)
    result = {key: cents * weights[key] // total for key in keys}
    remainder = cents - sum(result.values())
    ranked = sorted(keys, key=lambda k: (-(cents * weights[k] % total), k))
    for key in ranked[:remainder]:
        result[key] += 1
    return result


class SiteBooks:
    def __init__(self, path=":memory:", clock=now_ms):
        self.clock = clock
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------- plumbing
    def close(self):
        self.conn.close()

    @contextmanager
    def write(self):
        with self._lock:
            if self.conn.in_transaction:
                yield
                return
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def _row(self, sql, params=()):
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _rows(self, sql, params=()):
        return [dict(row) for row in self.conn.execute(sql, params)]

    def _insert(self, sql, params=()):
        return self.conn.execute(sql, params).lastrowid

    def _event(self, topic, ref=None, **detail):
        self._insert(
            "INSERT INTO events (at, kind, ref, detail) VALUES (?, ?, ?, ?)",
            (self.clock(), topic, ref, dumps(detail) if detail else None))

    # -------------------------------------------------------------- parties
    def add_party(self, party, role, name=None):
        if not isinstance(party, str) or not party:
            raise Invalid("party 不能为空")
        if role not in ROLES:
            raise Invalid("role 不合法", role=role)
        with self.write():
            self.conn.execute(
                "INSERT INTO parties (id, role, name) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET role = excluded.role, name = excluded.name",
                (party, role, name))
        return self.party(party)

    def party(self, party):
        row = self._row("SELECT * FROM parties WHERE id = ?", (party,))
        if not row:
            raise NotFound("未登记经办方", party=party)
        return row

    def parties(self):
        return self._rows("SELECT * FROM parties ORDER BY id")

    def require_role(self, party, *roles):
        row = self.party(party)
        if row["role"] not in roles:
            raise Forbidden("该操作不属于此角色", party=party, role=row["role"])
        return row

    def _ensure_operator(self, party):
        row = self.party(party)
        if row["role"] != "operator":
            raise Forbidden("该操作需要经办方身份", party=party, role=row["role"])
        return row

    # ---------------------------------------------------------------- sites
    def add_site(self, site, owner, operators=()):
        self.add_party(owner, "owner")
        for operator in operators:
            self.add_party(operator, "operator")
        with self.write():
            self.conn.execute(
                "INSERT INTO sites (id, owner) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET owner = excluded.owner", (site, owner))
            for operator in operators:
                self.conn.execute(
                    "INSERT OR IGNORE INTO site_operators (site, party) VALUES (?, ?)",
                    (site, operator))
        return self.site(site)

    def site(self, site):
        row = self._row("SELECT * FROM sites WHERE id = ?", (site,))
        if not row:
            raise NotFound("未登记站点", site=site)
        row["operators"] = self._site_operators(site)
        return row

    def _site_operators(self, site):
        return [r["party"] for r in self._rows(
            "SELECT party FROM site_operators WHERE site = ? ORDER BY party", (site,))]

    def _owner_of_work(self, work):
        row = self._row(
            "SELECT s.owner FROM sites s JOIN works w ON w.site = s.id WHERE w.id = ?",
            (work,))
        return row["owner"] if row else None

    # ------------------------------------------------------- register import
    def seed(self, register):
        """从资料端台账导入基础事实，重复导入保持幂等。"""
        if not isinstance(register, dict):
            raise Invalid("台账格式不正确")
        sites = register.get("sites") or {}
        works = register.get("works") or {}
        invoice = register.get("invoice") or []
        shares = register.get("shares") or {}
        with self.write():
            for site_id, site in sites.items():
                self.conn.execute(
                    "INSERT INTO sites (id, owner) VALUES (?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET owner = excluded.owner",
                    (site_id, site["owner"]))
                self.conn.execute(
                    "INSERT INTO parties (id, role) VALUES (?, 'owner') "
                    "ON CONFLICT(id) DO NOTHING", (site["owner"],))
                for operator in site.get("operators", []):
                    self.conn.execute(
                        "INSERT INTO parties (id, role) VALUES (?, 'operator') "
                        "ON CONFLICT(id) DO NOTHING", (operator,))
                    self.conn.execute(
                        "INSERT OR IGNORE INTO site_operators (site, party) VALUES (?, ?)",
                        (site_id, operator))
            default_site = next(iter(sites), "")
            for work_id, work in works.items():
                self.conn.execute(
                    "INSERT INTO works (id, site, type, user) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET type = excluded.type, user = excluded.user",
                    (work_id, default_site, work.get("type", "unknown"), work.get("user")))
                if work.get("user"):
                    self.conn.execute(
                        "INSERT INTO parties (id, role) VALUES (?, 'operator') "
                        "ON CONFLICT(id) DO NOTHING", (work["user"],))
            for item in invoice:
                if item.get("work") not in works:
                    raise Invalid("发票行引用了未登记的设施", work=item.get("work"))
                self.conn.execute(
                    "INSERT INTO invoices (id) VALUES (?) ON CONFLICT(id) DO NOTHING",
                    (item["id"],))
                self.conn.execute(
                    "INSERT INTO invoice_lines (invoice, line, work, part, cents, status) "
                    "VALUES (?, ?, ?, ?, ?, 'attributed') "
                    "ON CONFLICT(invoice, line) DO NOTHING",
                    (item["id"], item["line"], item.get("work"), item.get("part"),
                     item["cents"]))
            for work_id, work_shares in shares.items():
                if work_id not in works:
                    raise Invalid("分摊引用了未登记的设施", work=work_id)
                bps = self._normalise_share_input(work_shares)
                existing = self._row(
                    "SELECT id FROM allocations WHERE work = ? AND part IS NULL "
                    "AND status = 'confirmed'", (work_id,))
                if existing:
                    continue
                self._write_allocation(work_id, None, bps, "register", "register")
            self._event("seed", detail={"sites": len(sites), "works": len(works),
                                        "lines": len(invoice)})

    def _normalise_share_input(self, shares):
        if not isinstance(shares, dict) or not shares:
            raise Invalid("分摊不能为空")
        if all(isinstance(v, int) and v > 0 for v in shares.values()) \
                and sum(shares.values()) == 100:
            shares = {party: bps * 100 for party, bps in shares.items()}
        for party, bps in shares.items():
            self.party(party)
            if not isinstance(bps, int) or bps <= 0:
                raise Invalid("分摊比例应为正整数 bps", party=party)
        if sum(shares.values()) != 10000:
            raise Invalid("分摊比例合计必须为 10000 bps", total=sum(shares.values()))
        return dict(shares)

    # ---------------------------------------------------------------- works
    def add_work(self, work, *, site="", type="unknown", user=None):
        if type not in ("dedicated", "shared", "unknown"):
            raise Invalid("设施类型不合法", type=type)
        if user:
            self.party(user)
        if site and not self._row("SELECT 1 FROM sites WHERE id = ?", (site,)):
            raise NotFound("未登记站点", site=site)
        with self.write():
            if self._row("SELECT 1 FROM works WHERE id = ?", (work,)):
                raise Conflict("设施已登记", work=work)
            self._insert("INSERT INTO works (id, site, type, user) VALUES (?, ?, ?, ?)",
                         (work, site, type, user))
            self._event("work.added", work)
        return self.work(work)

    def work(self, work):
        row = self._row("SELECT * FROM works WHERE id = ?", (work,))
        if not row:
            raise NotFound("未登记设施", work=work)
        row["parts"] = self._rows(
            "SELECT * FROM parts WHERE work = ? ORDER BY id", (work,))
        row["takers"] = [r["party"] for r in self._rows(
            "SELECT party FROM takes WHERE work = ? ORDER BY party", (work,))]
        row["site_detail"] = self.site(row["site"]) if row["site"] else None
        return row

    def works(self):
        return [self.work(row["id"]) for row in self._rows("SELECT id FROM works ORDER BY id")]

    def part(self, work, part):
        row = self._row("SELECT * FROM parts WHERE work = ? AND id = ?", (work, part))
        if not row:
            raise NotFound("未登记接用部位", work=work, part=part)
        return row

    def register_part(self, work, part, *, usage="unknown", user=None, weight=1, cents=None):
        work_row = self.work(work)
        if usage not in ("dedicated", "shared", "unknown"):
            raise Invalid("部位用途不合法", usage=usage)
        if not isinstance(weight, int) or weight <= 0:
            raise Invalid("权重应为正整数")
        if user:
            self.party(user)
        if usage == "dedicated" and not user and not work_row["user"]:
            raise Invalid("独立使用部位需给出承担方")
        with self.write():
            self.conn.execute(
                "INSERT INTO parts (id, work, usage, user, weight, cents) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(work, id) DO UPDATE SET "
                "usage = excluded.usage, user = excluded.user, weight = excluded.weight, "
                "cents = COALESCE(excluded.cents, parts.cents)",
                (part, work, usage, user, weight, cents))
            self._event("part.registered", f"{work}:{part}", usage=usage, user=user)
        return self.work(work)

    def accept_work(self, work, *, party=None, part=None, at=None):
        with self.write():
            if not self._row("SELECT 1 FROM works WHERE id = ?", (work,)):
                raise NotFound("未登记设施", work=work)
            at = at if at is not None else self.clock()
            self.conn.execute(
                "UPDATE works SET status = CASE WHEN status = 'registered' THEN 'accepted' "
                "ELSE status END, accepted_at = COALESCE(accepted_at, ?) WHERE id = ?",
                (at, work))
            if part:
                if not self._row("SELECT 1 FROM parts WHERE work = ? AND id = ?",
                                 (work, part)):
                    self._insert("INSERT INTO parts (id, work) VALUES (?, ?)", (part, work))
                self.conn.execute(
                    "UPDATE parts SET delivered_at = COALESCE(delivered_at, ?) "
                    "WHERE work = ? AND id = ?", (at, work, part))
            self._event("work.accepted", work, party=party, part=part, at=at)
        return self.work(work)

    def take(self, work, party, *, part=None, at=None):
        self._ensure_operator(party)
        with self.write():
            row = self._row("SELECT * FROM works WHERE id = ?", (work,))
            if not row:
                raise NotFound("未登记设施", work=work)
            if row["status"] == "registered":
                raise Conflict("设施尚未验收，不能接用", work=work, status=row["status"])
            if part and not self._row("SELECT 1 FROM parts WHERE work = ? AND id = ?",
                                      (work, part)):
                raise NotFound("未登记接用部位", work=work, part=part)
            at = at if at is not None else self.clock()
            self.conn.execute(
                "INSERT OR IGNORE INTO takes (work, party, at) VALUES (?, ?, ?)",
                (work, party, at))
            if part:
                self.conn.execute(
                    "INSERT OR IGNORE INTO part_takes (work, part, party, at) "
                    "VALUES (?, ?, ?, ?)", (work, part, party, at))
            self.conn.execute(
                "UPDATE works SET status = CASE WHEN status = 'accepted' THEN 'taken' "
                "ELSE status END WHERE id = ?", (work,))
            self._event("work.taken", work, party=party, part=part, at=at)
        return self.work(work)

    def deliver(self, work, party, *, part=None, at=None):
        self._ensure_operator(party)
        with self.write():
            if not self._row("SELECT 1 FROM takes WHERE work = ? AND party = ?",
                             (work, party)):
                raise Conflict("该方尚未接用，不能确认交付", work=work, party=party)
            at = at if at is not None else self.clock()
            self.conn.execute(
                "UPDATE works SET status = 'delivered', delivered_at = ? WHERE id = ?",
                (at, work))
            if part:
                self.conn.execute(
                    "UPDATE parts SET delivered_at = ? WHERE work = ? AND id = ?",
                    (at, work, part))
            self._event("work.delivered", work, party=party, part=part, at=at)
        return self.work(work)

    # ---------------------------------------------------------- allocations
    def _write_allocation(self, work, part, shares, basis, proposed_by):
        with self.write():
            self.conn.execute(
                "UPDATE allocations SET status = 'superseded' WHERE work = ? "
                "AND COALESCE(part, '') = COALESCE(?, '') AND status IN ('proposed', 'confirmed')",
                (work, part))
            allocation_id = self._insert(
                "INSERT INTO allocations (work, part, status, basis, proposed_by, "
                "created_at, confirmed_at) VALUES (?, ?, 'confirmed', ?, ?, ?, ?)",
                (work, part, basis, proposed_by, self.clock(), self.clock()))
            for party, bps in sorted(shares.items()):
                self._insert(
                    "INSERT INTO allocation_shares (allocation_id, party, bps) "
                    "VALUES (?, ?, ?)", (allocation_id, party, bps))
            self._event("allocation.confirmed", work, part=part, basis=basis,
                        shares=shares)
        return self._allocation_row(allocation_id)

    def propose_allocation(self, work, part, shares, party):
        self._ensure_operator(party)
        self.work(work)
        shares = self._normalise_share_input(shares)
        with self.write():
            self.conn.execute(
                "UPDATE allocations SET status = 'superseded' WHERE work = ? "
                "AND COALESCE(part, '') = COALESCE(?, '') AND status IN ('proposed', 'confirmed')",
                (work, part))
            allocation_id = self._insert(
                "INSERT INTO allocations (work, part, status, basis, proposed_by, created_at) "
                "VALUES (?, ?, 'proposed', 'proposal', ?, ?)",
                (work, part, party, self.clock()))
            for p, bps in sorted(shares.items()):
                self._insert(
                    "INSERT INTO allocation_shares (allocation_id, party, bps) VALUES (?, ?, ?)",
                    (allocation_id, p, bps))
            self._event("allocation.proposed", work, part=part, by=party)
        return self._allocation_row(allocation_id)

    def _allocation_row(self, allocation_id):
        row = self._row("SELECT * FROM allocations WHERE id = ?", (allocation_id,))
        if not row:
            raise NotFound("未找到分摊约定", allocation=allocation_id)
        row["shares"] = {
            r["party"]: r["bps"] for r in self._rows(
                "SELECT party, bps FROM allocation_shares WHERE allocation_id = ? "
                "ORDER BY party", (allocation_id,))}
        row["confirmed_by"] = [r["party"] for r in self._rows(
            "SELECT party FROM allocation_confirmations WHERE allocation_id = ? ORDER BY party",
            (allocation_id,))]
        return row

    def confirm_allocation(self, allocation_id, party):
        self.party(party)
        with self.write():
            row = self._allocation_row(allocation_id)
            if row["status"] != "proposed":
                raise Conflict("分摊约定不在待确认状态", allocation=allocation_id,
                               status=row["status"])
            if party not in row["shares"]:
                raise Forbidden("只由约定中的负担方确认", allocation=allocation_id,
                                party=party)
            self.conn.execute(
                "INSERT OR IGNORE INTO allocation_confirmations (allocation_id, party, at) "
                "VALUES (?, ?, ?)", (allocation_id, party, self.clock()))
            confirmed = {r["party"] for r in self._rows(
                "SELECT party FROM allocation_confirmations WHERE allocation_id = ?",
                (allocation_id,))}
            if set(row["shares"]).issubset(confirmed):
                self.conn.execute(
                    "UPDATE allocations SET status = 'confirmed', confirmed_at = ? WHERE id = ?",
                    (self.clock(), allocation_id))
                self._event("allocation.confirmed", row["work"], part=row["part"],
                            shares=row["shares"])
            return self._allocation_row(allocation_id)

    def current_allocation(self, work, part=None):
        row = None
        if part is not None:
            row = self._row(
                "SELECT * FROM allocations WHERE work = ? AND part = ? "
                "AND status = 'confirmed' ORDER BY id DESC LIMIT 1", (work, part))
        if row is None:
            row = self._row(
                "SELECT * FROM allocations WHERE work = ? AND part IS NULL "
                "AND status = 'confirmed' ORDER BY id DESC LIMIT 1", (work,))
        return self._allocation_row(row["id"]) if row else None

    # -------------------------------------------------------------- invoices
    def add_invoice_line(self, invoice, line, *, work=None, part=None, cents=None,
                         period=None):
        if not isinstance(cents, int) or cents <= 0:
            raise Invalid("发票行金额应为正整数（分）", cents=cents)
        if work and not self._row("SELECT 1 FROM works WHERE id = ?", (work,)):
            raise NotFound("未登记设施", work=work)
        with self.write():
            self.conn.execute(
                "INSERT INTO invoices (id, period) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET period = CASE WHEN invoices.period = '' "
                "THEN excluded.period ELSE invoices.period END",
                (invoice, parse_period(period) if period else ""))
            existing = self._row(
                "SELECT * FROM invoice_lines WHERE invoice = ? AND line = ?", (invoice, line))
            if existing:
                if existing["cents"] != cents:
                    raise Conflict("同一发票行的金额与已登记不一致",
                                   invoice=invoice, line=line)
                return existing
            self._insert(
                "INSERT INTO invoice_lines (invoice, line, work, part, cents, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (invoice, line, work, part, cents, "attributed" if work else "held"))
        return self.invoice_line(invoice, line)

    def invoice_line(self, invoice, line):
        row = self._row(
            "SELECT * FROM invoice_lines WHERE invoice = ? AND line = ?", (invoice, line))
        if not row:
            raise NotFound("未登记发票行", invoice=invoice, line=line)
        return row

    def invoice_lines(self, invoice):
        return self._rows(
            "SELECT * FROM invoice_lines WHERE invoice = ? ORDER BY line", (invoice,))

    def hold_line(self, invoice, line, reason=None):
        with self.write():
            row = self.invoice_line(invoice, line)
            if row["status"] == "withdrawn":
                raise Conflict("已撤回的发票行不能重新挂起", invoice=invoice, line=line)
            active = self._row(
                "SELECT 1 FROM charges WHERE invoice = ? AND line = ? AND status != 'reversed'",
                (invoice, line))
            if active:
                raise Conflict("发票行已有份额，不能重新挂起", invoice=invoice, line=line)
            self.conn.execute(
                "UPDATE invoice_lines SET status = 'held', held_reason = ?, work = NULL, "
                "part = NULL WHERE invoice = ? AND line = ?", (reason, invoice, line))
            self._event("line.held", f"{invoice}:{line}", reason=reason)
        return self.invoice_line(invoice, line)

    def attribute_line(self, invoice, line, work, part=None, *, party=None):
        with self.write():
            row = self.invoice_line(invoice, line)
            if row["status"] == "withdrawn":
                raise Conflict("已撤回的发票行不能恢复", invoice=invoice, line=line)
            if not self._row("SELECT 1 FROM works WHERE id = ?", (work,)):
                raise NotFound("未登记设施", work=work)
            if part and not self._row("SELECT 1 FROM parts WHERE work = ? AND id = ?",
                                      (work, part)):
                raise NotFound("未登记接用部位", work=work, part=part)
            self.conn.execute(
                "UPDATE invoice_lines SET work = ?, part = ?, status = 'attributed', "
                "held_reason = NULL WHERE invoice = ? AND line = ?",
                (work, part, invoice, line))
            self._event("line.attributed", f"{invoice}:{line}", work=work, part=part,
                        by=party)
        return self.invoice_line(invoice, line)

    def withdraw_line(self, invoice, line, reason=None):
        with self.write():
            self.invoice_line(invoice, line)
            active = self._row(
                "SELECT 1 FROM charges WHERE invoice = ? AND line = ? "
                "AND status IN ('claimed', 'settled')", (invoice, line))
            if active:
                raise Conflict("发票行已有认领或结算，不能撤回", invoice=invoice, line=line)
            self.conn.execute(
                "DELETE FROM charges WHERE invoice = ? AND line = ? AND status = 'open'",
                (invoice, line))
            self.conn.execute(
                "UPDATE invoice_lines SET status = 'withdrawn', work = NULL, part = NULL, "
                "held_reason = ? WHERE invoice = ? AND line = ?", (reason, invoice, line))
            self._event("line.withdrawn", f"{invoice}:{line}", reason=reason)
        return self.invoice_line(invoice, line)

    # ----------------------------------------------------------------- stock
    def stock(self, invoice, line):
        """按经确认的可用空间与分摊约定，返回该发票行的应负担份额。"""
        line_row = self.invoice_line(invoice, line)
        if line_row["status"] != "attributed" or not line_row["work"]:
            raise Conflict("发票行尚未归属，不能拆分份额", invoice=invoice, line=line,
                           status=line_row["status"])
        shares = self._stock_for_line(line_row)
        if not shares:
            raise Conflict("没有可用的分摊约定", invoice=invoice, line=line)
        total = sum(item["cents"] for item in shares)
        if total != line_row["cents"]:
            raise Conflict("份额合计与发票行金额不一致", invoice=invoice, line=line,
                           cents=line_row["cents"], allocated=total)
        return shares

    def _stock_for_line(self, line_row):
        work = self._row("SELECT * FROM works WHERE id = ?", (line_row["work"],))
        if not work:
            raise NotFound("未登记设施", work=line_row["work"])
        cents = line_row["cents"]
        if line_row["part"]:
            part = self.part(work["id"], line_row["part"])
            return self._part_stock(work, part, cents)
        parts = self._rows(
            "SELECT * FROM parts WHERE work = ? AND status = 'active' ORDER BY id",
            (work["id"],))
        if len(parts) < 2:
            # 只有一个（或没有）已登记部位时，无法判断整项支出的部位构成，
            # 回到整项设施的约定，避免把全部分额压到单一回路。
            return self._whole_stock(work, cents)
        amounts = split_weighted(cents, {p["id"]: p["weight"] for p in parts})
        result = []
        for part in parts:
            result.extend(self._part_stock(work, part, amounts[part["id"]]))
        return result

    def _whole_stock(self, work, cents):
        if work["type"] == "dedicated" or work["user"]:
            if not work["user"]:
                raise Conflict("专用设施缺少承担方", work=work["id"])
            return [{"bearer": work["user"], "cents": cents, "basis": "dedicated",
                     "part": ""}]
        allocation = self.current_allocation(work["id"])
        if not allocation:
            raise Conflict("缺少经确认的分摊约定", work=work["id"])
        return [{"bearer": party, "cents": amount, "basis": "shared", "part": ""}
                for party, amount in sorted(split_amount(cents, allocation["shares"]).items())]

    def _allocation_exact(self, work, part):
        """只取该部位自己的约定，不回落到整项设施。"""
        row = self._row(
            "SELECT * FROM allocations WHERE work = ? AND part = ? "
            "AND status = 'confirmed' ORDER BY id DESC LIMIT 1", (work, part))
        return self._allocation_row(row["id"]) if row else None

    def _part_stock(self, work, part, cents):
        if not cents:
            return []
        if part["usage"] == "dedicated" or work["type"] == "dedicated":
            user = part["user"] or work["user"]
            if not user:
                raise Conflict("独立使用部位缺少承担方", work=work["id"], part=part["id"])
            exact = self._allocation_exact(work["id"], part["id"])
            if exact and set(exact["shares"]) != {user}:
                raise Conflict("独立使用部位的分摊与承担方不一致",
                               work=work["id"], part=part["id"])
            return [{"bearer": user, "cents": cents, "basis": "dedicated",
                     "part": part["id"]}]
        allocation = self._allocation_exact(work["id"], part["id"]) \
            or self.current_allocation(work["id"])
        if not allocation:
            raise Conflict("部位缺少经确认的分摊约定", work=work["id"], part=part["id"])
        return [{"bearer": party, "cents": amount, "basis": "shared", "part": part["id"]}
                for party, amount in sorted(split_amount(cents, allocation["shares"]).items())]

    def prepare_charges(self, invoice=None, line=None, work=None):
        """把已归属的发票行落成各自份额；尚未归属的行保持待认领。"""
        made, skipped = 0, []
        with self.write():
            if invoice and line:
                lines = [self.invoice_line(invoice, line)]
            else:
                sql = "SELECT * FROM invoice_lines WHERE status = 'attributed'"
                params = ()
                if invoice:
                    sql += " AND invoice = ?"
                    params += (invoice,)
                if work:
                    sql += " AND work = ?"
                    params += (work,)
                lines = self._rows(sql + " ORDER BY invoice, line", params)
            for line_row in lines:
                try:
                    shares = self.stock(line_row["invoice"], line_row["line"])
                except (Conflict, NotFound) as exc:
                    skipped.append({"invoice": line_row["invoice"],
                                    "line": line_row["line"], "reason": exc.message})
                    continue
                # 已由复核/移交重算过的部位不再生成基础份额，避免同一部位
                # 同时存在两套份额。
                adjusted = {row["part"] for row in self._rows(
                    "SELECT DISTINCT part FROM charges WHERE invoice = ? AND line = ? "
                    "AND origin_group != 'base' AND status != 'reversed'",
                    (line_row["invoice"], line_row["line"]))}
                existing = self._rows(
                    "SELECT * FROM charges WHERE invoice = ? AND line = ? "
                    "AND origin_group = 'base' ORDER BY id",
                    (line_row["invoice"], line_row["line"]))
                wanted = {(item["part"], item["bearer"]): item
                          for item in shares if item["part"] not in adjusted}
                have = {(row["part"], row["bearer"]): row for row in existing}
                blocked = False
                for key, row in have.items():
                    if key in wanted:
                        continue
                    if row["status"] == "open":
                        self.conn.execute("DELETE FROM charges WHERE id = ?",
                                          (row["id"],))
                    elif row["part"] in adjusted:
                        # 该部位已由复核/移交接管，保留其专属份额。
                        continue
                    else:
                        blocked = True
                if blocked:
                    skipped.append({"invoice": line_row["invoice"],
                                    "line": line_row["line"],
                                    "reason": "已认领/已结份额与现行约定不一致，需复核"})
                    continue
                for key, item in wanted.items():
                    row = have.get(key)
                    if row is None:
                        self._insert_charge(line_row["invoice"], line_row["line"],
                                            line_row["work"], item["part"], item["bearer"],
                                            item["cents"], item["basis"])
                        made += 1
                    elif row["status"] == "open" and row["cents"] != item["cents"]:
                        self.conn.execute("UPDATE charges SET cents = ? WHERE id = ?",
                                          (item["cents"], row["id"]))
            if skipped:
                self._event("charges.skipped", detail={"lines": skipped})
        return {"created": made, "skipped": skipped}

    def _insert_charge(self, invoice, line, work, part, bearer, cents, basis,
                       origin_group="base", status="open", claim_id=None, paid_cents=0):
        return self._insert(
            "INSERT INTO charges (invoice, line, work, part, bearer, cents, paid_cents, "
            "basis, origin_group, status, claim_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (invoice, line, work, part or "", bearer, cents, paid_cents, basis,
             origin_group, status, claim_id, self.clock()))

    def charge(self, charge_id):
        row = self._row("SELECT * FROM charges WHERE id = ?", (charge_id,))
        if not row:
            raise NotFound("未找到应负担份额", charge=charge_id)
        return row

    def charges(self, *, bearer=None, invoice=None, line=None, status=None, work=None):
        clauses, params = [], []
        for column, value in (("bearer", bearer), ("invoice", invoice), ("line", line),
                              ("status", status), ("work", work)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        sql = "SELECT * FROM charges"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return self._rows(sql + " ORDER BY id", params)

    # ---------------------------------------------------------------- claims
    def create_claim(self, party, *, period=None, charge_ids=None, invoice=None, line=None,
                     idempotency_key=None, note=None):
        """登记认领。单边 / 双边由份额牵涉的负担方自动判定。"""
        self._ensure_operator(party)
        period = parse_period(period)
        with self.write():
            self._ensure_open_period(period)
            if idempotency_key:
                prior = self._row(
                    "SELECT id FROM claims WHERE idempotency_key = ?", (idempotency_key,))
                if prior:
                    return self.claim(prior["id"])
            chosen = self._select_charges(party, charge_ids, invoice, line)
            total = sum(c["cents"] - c["paid_cents"] for c in chosen)
            if total <= 0:
                raise Conflict("所选份额没有未付金额")
            required = self._claim_required(party, chosen)
            mode = "single" if required == [party] else "bilateral"
            claim_id = self._insert(
                "INSERT INTO claims (period, bearer, amount_cents, status, mode, required, "
                "idempotency_key, created_at, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (period, party, total,
                 "pending_confirmation" if mode == "bilateral" else "approved",
                 mode, dumps(required), idempotency_key, self.clock(), note))
            for charge in chosen:
                self._bind_charge(claim_id, charge["id"])
            if mode == "bilateral":
                self.conn.execute(
                    "INSERT INTO claim_confirmations (claim_id, party, at) VALUES (?, ?, ?)",
                    (claim_id, party, self.clock()))
            self._event("claim.created", str(claim_id), party=party, mode=mode,
                        cents=total, required=required)
            return self.claim(claim_id)

    def _select_charges(self, party, charge_ids, invoice, line):
        rows = []
        if charge_ids:
            if isinstance(charge_ids, int):
                charge_ids = [charge_ids]
            for charge_id in charge_ids:
                row = self.charge(charge_id)
                if row["bearer"] != party:
                    raise Forbidden("只能认领本人应负担的份额", charge=charge_id)
                if row["status"] != "open":
                    raise Conflict("该份额已被占用", charge=charge_id, status=row["status"])
                if row["cents"] <= row["paid_cents"]:
                    raise Conflict("该份额已付清", charge=charge_id)
                rows.append(row)
            keys = [(r["invoice"], r["line"], r["part"]) for r in rows]
            if len(set(keys)) != len(keys):
                raise Conflict("同一发票行的同一部位不能在一次认领中重复取得")
            return rows
        if invoice and line:
            self.prepare_charges(invoice, line)
            rows = self._rows(
                "SELECT * FROM charges WHERE invoice = ? AND line = ? AND bearer = ? "
                "AND status = 'open' ORDER BY part, id", (invoice, line, party))
            if not rows:
                raise Conflict("该发票行没有本人未认领的份额", invoice=invoice, line=line)
            return rows
        raise Invalid("请给出 charge_ids 或 invoice+line")

    def _claim_required(self, party, charges):
        required = {party}
        for charge in charges:
            if charge["basis"] == "dedicated":
                continue
            allocation = self.current_allocation(charge["work"], charge["part"] or None)
            if allocation:
                required.update(allocation["shares"])
            else:
                work = self._row("SELECT * FROM works WHERE id = ?", (charge["work"],))
                if work and work["site"]:
                    required.update(self._site_operators(work["site"]))
            owner = self._owner_of_work(charge["work"])
            if owner and (charge["basis"] in ("recovery", "review", "handover")
                          or self._line_contested(charge["invoice"], charge["line"])):
                required.add(owner)
        return sorted(required)

    def _line_contested(self, invoice, line):
        return bool(self._row(
            "SELECT 1 FROM adjustments WHERE status = 'open' AND invoice = ? AND line = ?",
            (invoice, line))) or bool(self._row(
            "SELECT 1 FROM reviews WHERE status = 'pending' AND invoice = ? AND line = ?",
            (invoice, line)))

    def _bind_charge(self, claim_id, charge_id):
        charge = self.charge(charge_id)
        if charge["status"] != "open":
            raise Conflict("份额已被占用", charge=charge_id, status=charge["status"])
        try:
            self._insert("INSERT INTO claim_charges (claim_id, charge_id) VALUES (?, ?)",
                         (claim_id, charge_id))
        except sqlite3.IntegrityError as exc:
            raise Conflict("同一份额不能重复认领", charge=charge_id) from exc
        cursor = self.conn.execute(
            "UPDATE charges SET status = 'claimed', claim_id = ? "
            "WHERE id = ? AND status = 'open'", (claim_id, charge_id))
        if cursor.rowcount != 1:
            raise Conflict("份额已被并发占用", charge=charge_id)

    def claim(self, claim_id):
        row = self._row("SELECT * FROM claims WHERE id = ?", (claim_id,))
        if not row:
            raise NotFound("未找到认领", claim=claim_id)
        row["required"] = loads(row["required"]) if row["required"] else []
        row["charges"] = self._rows(
            "SELECT c.* FROM charges c JOIN claim_charges cc ON cc.charge_id = c.id "
            "WHERE cc.claim_id = ? ORDER BY c.id", (claim_id,))
        row["confirmations"] = [r["party"] for r in self._rows(
            "SELECT party FROM claim_confirmations WHERE claim_id = ? ORDER BY party",
            (claim_id,))]
        row["payments"] = self._rows(
            "SELECT ref, status, amount_cents, destination FROM payments WHERE claim_id = ? "
            "ORDER BY ref", (claim_id,))
        row["settlements"] = self._rows(
            "SELECT * FROM settlements WHERE claim_id = ? ORDER BY id", (claim_id,))
        return row

    def claims(self, *, bearer=None, period=None, status=None):
        clauses, params = [], []
        for column, value in (("bearer", bearer), ("period", period), ("status", status)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        sql = "SELECT id FROM claims"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return [self.claim(r["id"]) for r in self._rows(sql + " ORDER BY id", params)]

    def confirm_claim(self, claim_id, party):
        self.party(party)
        with self.write():
            claim = self.claim(claim_id)
            if claim["status"] != "pending_confirmation":
                raise Conflict("认领不在待确认状态", claim=claim_id, status=claim["status"])
            if party not in claim["required"]:
                raise Forbidden("该方不在确认范围", claim=claim_id, party=party)
            self.conn.execute(
                "INSERT OR IGNORE INTO claim_confirmations (claim_id, party, at) "
                "VALUES (?, ?, ?)", (claim_id, party, self.clock()))
            confirmed = {r["party"] for r in self._rows(
                "SELECT party FROM claim_confirmations WHERE claim_id = ?", (claim_id,))}
            if set(claim["required"]).issubset(confirmed):
                self.conn.execute(
                    "UPDATE claims SET status = 'approved', decided_at = ? WHERE id = ?",
                    (self.clock(), claim_id))
                self._event("claim.approved", str(claim_id))
            return self.claim(claim_id)

    def reject_claim(self, claim_id, party, reason=None):
        row = self.party(party)
        with self.write():
            claim = self.claim(claim_id)
            if claim["status"] not in ("pending_confirmation", "approved"):
                raise Conflict("认领不可驳回", claim=claim_id, status=claim["status"])
            if claim["status"] == "approved" and party != claim["bearer"] \
                    and row["role"] != "owner":
                raise Forbidden("需产权方或认领方才能撤销已批准认领", claim=claim_id)
            if self._row("SELECT 1 FROM payments WHERE claim_id = ? AND status IN "
                         "('reserved', 'confirmed', 'settled')", (claim_id,)):
                raise Conflict("认领已有付款，不能直接驳回", claim=claim_id)
            self._release_claim(claim_id, reason)
            self._event("claim.rejected", str(claim_id), party=party, reason=reason)
            return self.claim(claim_id)

    def _release_claim(self, claim_id, reason=None):
        self.conn.execute(
            "UPDATE claims SET status = 'cancelled', decided_at = ?, "
            "note = COALESCE(?, note) WHERE id = ?", (self.clock(), reason, claim_id))
        self.conn.execute(
            "UPDATE charges SET status = 'open', claim_id = NULL WHERE claim_id = ? "
            "AND status = 'claimed'", (claim_id,))
        self.conn.execute("DELETE FROM claim_charges WHERE claim_id = ?", (claim_id,))

    def _recalc_claim(self, claim_id):
        row = self._row(
            "SELECT COALESCE(SUM(c.cents - c.paid_cents), 0) AS cents, COUNT(*) AS n "
            "FROM charges c JOIN claim_charges cc ON cc.charge_id = c.id "
            "WHERE cc.claim_id = ?", (claim_id,))
        if not row["n"] or row["cents"] <= 0:
            self._release_claim(claim_id, "份额已调整")
            return
        self.conn.execute("UPDATE claims SET amount_cents = ? WHERE id = ?",
                          (row["cents"], claim_id))

    # -------------------------------------------------------------- budgets
    def set_budget(self, period, party, cents):
        period = parse_period(period)
        if not isinstance(cents, int) or cents < 0:
            raise Invalid("预算应为非负整数（分）")
        self.party(party)
        with self.write():
            self._ensure_open_period(period)
            self.conn.execute(
                "INSERT INTO budgets (period, party, cents) VALUES (?, ?, ?) "
                "ON CONFLICT(period, party) DO UPDATE SET cents = excluded.cents",
                (period, party, cents))
        return self.budget_state(period, party)

    def _committed(self, period, bearer):
        if bearer is None:
            return self._row(
                "SELECT COALESCE(SUM(COALESCE(amount_cents, expected_cents, 0)), 0) AS cents "
                "FROM payments WHERE period = ? AND bearer IS NULL AND unassigned = 1 "
                "AND status IN ('reserved', 'confirmed', 'failed')", (period,))["cents"]
        return self._row(
            "SELECT COALESCE(SUM(COALESCE(amount_cents, expected_cents, 0)), 0) AS cents "
            "FROM payments WHERE period = ? AND bearer = ? AND status IN "
            "('reserved', 'confirmed', 'settled', 'failed')", (period, bearer))["cents"]

    def budget_state(self, period, bearer=None):
        period = parse_period(period)
        budgets = self._rows(
            "SELECT party, cents FROM budgets WHERE period = ?"
            + (" AND party = ?" if bearer else "") + " ORDER BY party",
            (period, bearer) if bearer else (period,))
        state = {}
        for row in budgets:
            committed = self._committed(period, row["party"])
            state[row["party"]] = {"budget_cents": row["cents"],
                                   "committed_cents": committed,
                                   "available_cents": row["cents"] - committed}
        if not bearer:
            # 已发生占用但未设预算的负担方也要可见，避免预算外承诺被忽略。
            for row in self._rows(
                    "SELECT DISTINCT bearer FROM payments WHERE period = ? "
                    "AND bearer IS NOT NULL", (period,)):
                state.setdefault(row["bearer"], {
                    "budget_cents": 0,
                    "committed_cents": self._committed(period, row["bearer"]),
                    "available_cents": -self._committed(period, row["bearer"])})
        unassigned = self._committed(period, None)
        if unassigned:
            state["__unassigned__"] = {"budget_cents": 0,
                                       "committed_cents": unassigned,
                                       "available_cents": -unassigned}
        return state

    # -------------------------------------------------------------- payments
    def reserve_payment(self, ref, *, claim_id=None, period=None, desk="procurement",
                        amount_cents=None, destination="operator", bearer=None,
                        invoice=None, line=None, work=None):
        """占用一笔待付款；对端回执未到前不释放预算。"""
        if not isinstance(ref, str) or not ref:
            raise Invalid("付款编号不能为空")
        with self.write():
            if self._row("SELECT 1 FROM payments WHERE ref = ?", (ref,)):
                raise Conflict("付款编号已存在", ref=ref)
            claim = None
            if claim_id is not None:
                claim = self.claim(claim_id)
                if claim["status"] != "approved":
                    raise Conflict("认领尚未获批，不能付款", claim=claim_id,
                                   status=claim["status"])
                if not claim["charges"]:
                    raise Conflict("认领没有可付份额", claim=claim_id)
                bearer = bearer or claim["bearer"]
                period = period or claim["period"]
                outstanding = sum(max(c["cents"] - c["paid_cents"], 0)
                                  for c in claim["charges"])
                amount_cents = outstanding if amount_cents is None else amount_cents
                if amount_cents > outstanding:
                    raise Conflict("付款超过认领未付金额", claim=claim_id,
                                   outstanding=outstanding)
                first = claim["charges"][0]
                invoice = invoice or first["invoice"]
                line = line or first["line"]
                work = work or first["work"]
                if self._row("SELECT 1 FROM payments WHERE claim_id = ? AND status IN "
                             "('reserved', 'confirmed')", (claim_id,)):
                    raise Conflict("该认领已有在途付款", claim=claim_id)
            period = parse_period(period)
            self._ensure_open_period(period)
            if amount_cents is not None and (not isinstance(amount_cents, int)
                                             or amount_cents <= 0):
                raise Invalid("付款金额应为正整数（分）")
            if bearer:
                self.party(bearer)
            if invoice and line and self._row(
                    "SELECT 1 FROM payments WHERE invoice = ? AND line = ? AND unassigned = 1 "
                    "AND status IN ('reserved', 'confirmed', 'failed')",
                    (invoice, line)):
                raise Conflict("该发票行已有归属未明的付款占用，不能重复使用同一份预算",
                               invoice=invoice, line=line)
            budget = self._row(
                "SELECT cents FROM budgets WHERE period = ? AND party = ?",
                (period, bearer))
            if budget and amount_cents is not None:
                committed = self._committed(period, bearer)
                if committed + amount_cents > budget["cents"]:
                    raise Conflict("预算不足，不能超额承诺", period=period,
                                   bearer=bearer, budget_cents=budget["cents"],
                                   committed_cents=committed)
            unassigned = 1 if invoice and line and claim is None else 0
            self._insert(
                "INSERT INTO payments (ref, claim_id, period, bearer, desk, invoice, line, "
                "work, amount_cents, expected_cents, status, unassigned, created_at, destination) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?)",
                (ref, claim_id, period, bearer, desk, invoice, line, work, amount_cents,
                 amount_cents, unassigned, self.clock(), destination))
            if claim:
                self._bind_payment_charges(ref, claim["charges"])
            self._event("payment.reserved", ref, claim=claim_id, bearer=bearer,
                        cents=amount_cents, unassigned=bool(unassigned))
            return self.payment(ref)

    def _bind_payment_charges(self, ref, charges):
        for charge in charges:
            if self._row("SELECT 1 FROM payment_charges WHERE charge_id = ?",
                         (charge["id"],)):
                raise Conflict("份额已在另一笔付款中占用", charge=charge["id"])
        for charge in charges:
            self._insert(
                "INSERT INTO payment_charges (payment_ref, charge_id) VALUES (?, ?)",
                (ref, charge["id"]))

    def record_submitted_payment(self, ref, *, period=None, invoice=None, line=None,
                                 work=None, desk="procurement"):
        """采购平台报来的付款（尚未明确占用），先占位、不拆份额。"""
        with self.write():
            if self._row("SELECT 1 FROM payments WHERE ref = ?", (ref,)):
                return self.payment(ref)
            period = parse_period(period)
            self._ensure_open_period(period)
            if invoice and line and self._row(
                    "SELECT 1 FROM payments WHERE invoice = ? AND line = ? AND unassigned = 1 "
                    "AND status IN ('reserved', 'confirmed', 'failed')",
                    (invoice, line)):
                raise Conflict("该发票行已有归属未明的付款占用，不能重复使用同一份预算",
                               invoice=invoice, line=line)
            self._insert(
                "INSERT INTO payments (ref, period, desk, invoice, line, work, unassigned, "
                "status, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, 'reserved', ?)",
                (ref, period, desk, invoice, line, work, self.clock()))
            self._event("payment.recorded", ref, unassigned=True)
            return self.payment(ref)

    def payment(self, ref):
        row = self._row("SELECT * FROM payments WHERE ref = ?", (ref,))
        if not row:
            raise NotFound("未找到付款", ref=ref)
        if row["receipt"]:
            row["receipt"] = loads(row["receipt"])
        row["charges"] = [r["charge_id"] for r in self._rows(
            "SELECT charge_id FROM payment_charges WHERE payment_ref = ? ORDER BY charge_id",
            (ref,))]
        return row

    def payments(self, *, period=None, bearer=None, status=None):
        clauses, params = [], []
        for column, value in (("period", period), ("bearer", bearer), ("status", status)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        sql = "SELECT ref FROM payments"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return [self.payment(r["ref"]) for r in self._rows(sql + " ORDER BY ref", params)]

    def pending_payments(self, period=None):
        """回执未到或未明确归属、仍占用预算的付款。"""
        sql = "SELECT ref FROM payments WHERE status IN ('reserved', 'failed')"
        params = ()
        if period:
            sql += " AND period = ?"
            params = (parse_period(period),)
        return [self.payment(r["ref"]) for r in self._rows(sql + " ORDER BY ref", params)]

    def mark_payment_pending(self, ref, error="receipt_temporarily_unavailable"):
        """对端暂时不可查：保持占用，仅记录原因。"""
        with self.write():
            row = self.payment(ref)
            if row["status"] not in ("reserved", "failed"):
                raise Conflict("付款状态已确定", ref=ref, status=row["status"])
            self.conn.execute(
                "UPDATE payments SET last_error = ?, synced_at = ? WHERE ref = ?",
                (error, self.clock(), ref))
            self._event("payment.pending", ref, error=error)
            return self.payment(ref)

    def sync_payment(self, ref, evidence=None):
        """按对端回执推进付款；未到账/金额不符时保持占用。"""
        evidence = evidence or {}
        with self.write():
            row = self.payment(ref)
            if row["status"] in ("settled", "released"):
                raise Conflict("付款已结清或已释放", ref=ref, status=row["status"])
            state = evidence.get("state")
            if evidence.get("paid") is True and state is None:
                state = "paid"
            amount = evidence.get("amount_cents")
            if state != "paid":
                self.conn.execute(
                    "UPDATE payments SET status = 'failed', last_error = ?, synced_at = ?, "
                    "receipt = ? WHERE ref = ?",
                    (f"对端状态 {state!r}", self.clock(), dumps(evidence), ref))
                self._event("payment.not_paid", ref, state=state)
                return self.payment(ref)
            if amount is not None and (not isinstance(amount, int) or amount <= 0):
                raise Invalid("回执金额应为正整数（分）", amount=amount)
            if not row["unassigned"] and row["expected_cents"] is not None \
                    and amount is not None and amount != row["expected_cents"]:
                # 不回写 amount_cents：原有占用金额继续计入预算，直到人工处置。
                self.conn.execute(
                    "UPDATE payments SET status = 'failed', last_error = ?, synced_at = ?, "
                    "receipt = ? WHERE ref = ?",
                    ("回执金额与占用金额不一致", self.clock(), dumps(evidence), ref))
                self._event("payment.held", ref, reason="amount_mismatch",
                            expected=row["expected_cents"], actual=amount)
                return self.payment(ref)
            new_amount = amount if amount is not None else row["expected_cents"]
            self.conn.execute(
                "UPDATE payments SET status = 'confirmed', receipt = ?, last_error = NULL, "
                "synced_at = ?, amount_cents = COALESCE(?, amount_cents) WHERE ref = ?",
                (dumps(evidence), self.clock(), new_amount, ref))
            self._event("payment.confirmed", ref, cents=new_amount)
            return self.payment(ref)

    def release_payment(self, ref, reason=None):
        with self.write():
            row = self.payment(ref)
            if row["status"] not in ("reserved", "failed"):
                raise Conflict("只有未确认的占用可以释放", ref=ref, status=row["status"])
            self.conn.execute(
                "UPDATE payments SET status = 'released', last_error = COALESCE(?, last_error) "
                "WHERE ref = ?", (reason, ref))
            self.conn.execute("DELETE FROM payment_charges WHERE payment_ref = ?", (ref,))
            self._event("payment.released", ref, reason=reason)
            return self.payment(ref)

    def assign_payment(self, ref, claim_id, *, destination=None):
        """把回执已确认但归属未明的付款划到认领，并按份额入账。"""
        with self.write():
            row = self.payment(ref)
            if row["status"] != "confirmed":
                raise Conflict("只有已确认到账的付款可以归属", ref=ref, status=row["status"])
            claim = self.claim(claim_id)
            if claim["status"] != "approved":
                raise Conflict("认领尚未获批，不能归属付款", claim=claim_id,
                               status=claim["status"])
            if row["claim_id"] not in (None, claim_id):
                raise Conflict("付款已归属其他认领", ref=ref, claim=row["claim_id"])
            if row["claim_id"] is None:
                if not row["unassigned"]:
                    raise Conflict("付款已归属其他用途", ref=ref)
                self._bind_payment_charges(ref, claim["charges"])
                self.conn.execute(
                    "UPDATE payments SET claim_id = ?, unassigned = 0, bearer = ?, "
                    "work = COALESCE(work, ?) WHERE ref = ?",
                    (claim_id, claim["bearer"],
                     claim["charges"][0]["work"] if claim["charges"] else None, ref))
                self._event("payment.assigned", ref, claim=claim_id)
            return self._settle_to_claim(self.payment(ref), self.claim(claim_id),
                                         destination)

    def settle_payment(self, ref, *, destination=None):
        with self.write():
            row = self.payment(ref)
            if row["status"] != "confirmed":
                raise Conflict("只有已确认到账的付款可以结算", ref=ref, status=row["status"])
            if not row["claim_id"]:
                raise Conflict("付款尚未归属认领，先办理归属", ref=ref)
            return self._settle_to_claim(row, self.claim(row["claim_id"]), destination)

    def _settle_to_claim(self, payment_row, claim, destination):
        destination = destination or payment_row["destination"] or "operator"
        amount = payment_row["amount_cents"] or payment_row["expected_cents"] or 0
        if amount <= 0:
            raise Conflict("付款没有金额，不能结算", ref=payment_row["ref"])
        remaining = amount
        for charge in claim["charges"]:
            if remaining <= 0:
                break
            outstanding = charge["cents"] - charge["paid_cents"]
            if outstanding <= 0 or charge["status"] == "reversed":
                continue
            applied = min(remaining, outstanding)
            self.conn.execute(
                "UPDATE charges SET paid_cents = paid_cents + ?, status = "
                "CASE WHEN paid_cents + ? >= cents THEN 'settled' ELSE status END "
                "WHERE id = ?", (applied, applied, charge["id"]))
            remaining -= applied
        applied_total = amount - remaining
        if applied_total <= 0:
            raise Conflict("付款没有可用于该认领的金额", ref=payment_row["ref"])
        self.conn.execute(
            "UPDATE payments SET status = 'settled', destination = ?, settled_at = ? "
            "WHERE ref = ?", (destination, self.clock(), payment_row["ref"]))
        self._insert(
            "INSERT INTO settlements (claim_id, payment_ref, destination, cents, at) "
            "VALUES (?, ?, ?, ?, ?)",
            (claim["id"], payment_row["ref"], destination, applied_total, self.clock()))
        unpaid = self._row(
            "SELECT COUNT(*) AS n FROM charges c JOIN claim_charges cc "
            "ON cc.charge_id = c.id WHERE cc.claim_id = ? AND c.status = 'claimed' "
            "AND c.paid_cents < c.cents", (claim["id"],))["n"]
        status = "settled" if not unpaid else "approved"
        self.conn.execute(
            "UPDATE claims SET status = ?, decided_at = COALESCE(decided_at, ?) WHERE id = ?",
            (status, self.clock(), claim["id"]))
        self._event("payment.settled", payment_row["ref"], claim=claim["id"],
                    destination=destination, cents=applied_total)
        return self.payment(payment_row["ref"])

    # --------------------------------------------------------------- reviews
    def open_review(self, work, part, *, invoice=None, line=None, reason=None, party=None):
        """登记“原本按共用支付、实际为独立使用”的复核（待处理）。"""
        with self.write():
            self.work(work)
            if part and not self._row("SELECT 1 FROM parts WHERE work = ? AND id = ?",
                                      (work, part)):
                self._insert("INSERT INTO parts (id, work) VALUES (?, ?)", (part, work))
            if invoice and line:
                line_row = self.invoice_line(invoice, line)
            else:
                line_row = self._row(
                    "SELECT * FROM invoice_lines WHERE work = ? AND status = 'attributed' "
                    "AND (? IS NULL OR part = ?) ORDER BY invoice, line LIMIT 1",
                    (work, part, part))
                if line_row is None and part:
                    line_row = self._row(
                        "SELECT * FROM invoice_lines WHERE work = ? "
                        "AND status = 'attributed' ORDER BY invoice, line LIMIT 1",
                        (work,))
            if not line_row or line_row["work"] != work:
                raise Conflict("该回路尚无已归属的支出可复核", work=work, part=part)
            old = self._rows(
                "SELECT * FROM charges WHERE invoice = ? AND line = ?"
                + (" AND part = ?" if part else "") + " ORDER BY id",
                (line_row["invoice"], line_row["line"], part) if part
                else (line_row["invoice"], line_row["line"]))
            review_id = self._insert(
                "INSERT INTO reviews (work, part, invoice, line, old_detail, new_bearer, "
                "amount_cents, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (work, part, line_row["invoice"], line_row["line"], dumps(old), party,
                 self._part_amount(line_row, part), reason or "回路实际独立使用",
                 self.clock()))
            # 复核待办期间不改写部位用途与原份额，未涉争议的一方继续照用。
            self._event("review.opened", str(review_id), work=work, part=part,
                        invoice=line_row["invoice"], line=line_row["line"])
            return self.review(review_id)

    def review(self, review_id):
        row = self._row("SELECT * FROM reviews WHERE id = ?", (review_id,))
        if not row:
            raise NotFound("未找到复核", review=review_id)
        row["old_detail"] = loads(row["old_detail"]) if row["old_detail"] else []
        row["adjustments"] = self._rows(
            "SELECT * FROM adjustments WHERE review_id = ? ORDER BY id", (review_id,))
        return row

    def reviews(self, *, status=None):
        sql = "SELECT id FROM reviews"
        params = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        return [self.review(r["id"]) for r in self._rows(sql + " ORDER BY id", params)]

    def apply_review(self, review_id, party, *, new_bearer=None, reason=None):
        """按复核结论重核应负担方。

        已支付的份额不改写：保留原认领、原结算，另立差额由应付方补给实付
        方；未支付的份额按新的独立使用范围重算；已付金额仍计在本行已付款
        内，故新的可认领份额为“目标金额 − 已付金额”。
        """
        self.require_role(party, "owner", "cashier")
        with self.write():
            review = self.review(review_id)
            if review["status"] != "pending":
                raise Conflict("复核已处理", review=review_id, status=review["status"])
            invoice, line, part = review["invoice"], review["line"], review["part"]
            line_row = self.invoice_line(invoice, line)
            related = [a for a in self.adjustments(status="open")
                       if a["invoice"] == invoice and a["line"] == line]
            if related:
                raise Conflict("该发票行仍有未结差额，先办结再复核",
                               invoice=invoice, line=line,
                               adjustments=[a["id"] for a in related])
            if not new_bearer:
                new_bearer = review["new_bearer"]
            if not new_bearer and part:
                candidate = self._row(
                    "SELECT user FROM parts WHERE work = ? AND id = ?",
                    (review["work"], part))
                new_bearer = candidate["user"] if candidate else None
            if not new_bearer:
                raise Invalid("复核需指定新的应负担方", review=review_id)
            self._ensure_operator(new_bearer)
            if part:
                self.conn.execute(
                    "UPDATE parts SET usage = 'dedicated', user = ? WHERE work = ? AND id = ?",
                    (new_bearer, review["work"], part))
                self._write_allocation(review["work"], part, {new_bearer: 10000},
                                       "review", party)
            target = self._scope_target(line_row, part)
            if not target:
                raise Conflict("复核后没有可负担份额", review=review_id)
            basis = f"part:{part}" if part else "review"
            affected = self._rows(
                "SELECT * FROM charges WHERE invoice = ? AND line = ? AND origin_group = 'base'"
                + (" AND part = ?" if part else "") + " ORDER BY id",
                (invoice, line, part) if part else (invoice, line))
            paid_by = {}
            scope_paid = 0
            for charge in affected:
                if charge["status"] == "open":
                    self.conn.execute("DELETE FROM charges WHERE id = ?", (charge["id"],))
                    continue
                if charge["status"] == "claimed" and charge["paid_cents"] == 0:
                    self._void_unpaid_claim_charge(charge)
                    continue
                if charge["paid_cents"] > 0:
                    scope_paid += charge["paid_cents"]
                    if charge["bearer"] == new_bearer:
                        paid_by[new_bearer] = paid_by.get(new_bearer, 0) \
                            + charge["paid_cents"]
                    else:
                        self._make_adjustment(
                            review["work"], part or charge["part"], invoice, line,
                            charge["bearer"], new_bearer, charge["paid_cents"],
                            reason or review["reason"], review_id=review_id)
                    if charge["status"] == "claimed":
                        self._unbind_charge(charge["claim_id"], charge["id"])
                        self._recalc_claim(charge["claim_id"])
                    # 已付份额保留原受理与结算，不再生成新份额。
                    continue
                self.conn.execute(
                    "UPDATE charges SET status = 'reversed' WHERE id = ?", (charge["id"],))
            target_total = sum(target.values())
            remaining = max(target_total - scope_paid, 0)
            outstanding = {bearer: max(cents - paid_by.get(bearer, 0), 0)
                           for bearer, cents in target.items()}
            outstanding = self._cap_total(outstanding, remaining)
            for bearer, cents in sorted(target.items()):
                amount = outstanding.get(bearer, 0)
                existing = self._row(
                    "SELECT * FROM charges WHERE invoice = ? AND line = ? AND part = ? "
                    "AND bearer = ? AND origin_group = 'review'",
                    (invoice, line, part or "", bearer))
                if amount <= 0:
                    if existing and existing["status"] == "open":
                        self.conn.execute("DELETE FROM charges WHERE id = ?",
                                          (existing["id"],))
                    continue
                if not existing:
                    self._insert_charge(invoice, line, review["work"], part, bearer,
                                        amount, basis, origin_group="review")
                elif existing["status"] == "open" and existing["cents"] != amount:
                    self.conn.execute("UPDATE charges SET cents = ? WHERE id = ?",
                                      (amount, existing["id"]))
            self.conn.execute(
                "UPDATE reviews SET status = 'applied', decided_at = ?, new_bearer = ?, "
                "reason = COALESCE(?, reason) WHERE id = ?",
                (self.clock(), new_bearer, reason, review_id))
            self._event("review.applied", str(review_id), bearer=new_bearer,
                        target=target, paid=scope_paid)
            return self.review(review_id)

    def _cap_total(self, amounts, cap):
        total = sum(amounts.values())
        if total <= cap:
            return amounts
        excess = total - cap
        result = dict(amounts)
        for key in sorted(result, key=lambda k: (-result[k], k)):
            if excess <= 0:
                break
            take = min(result[key], excess)
            result[key] -= take
            excess -= take
        return {key: value for key, value in result.items() if value > 0}

    def _scope_target(self, line_row, part):
        items = self._stock_for_line(line_row)
        if part:
            scoped = [item for item in items if item["part"] == part]
            if scoped:
                items = scoped
        target = {}
        for item in items:
            target[item["bearer"]] = target.get(item["bearer"], 0) + item["cents"]
        return target

    def _part_amount(self, line_row, part):
        """某回路应承担的发票行金额；单一部位或整行归属时即为整行。"""
        if not part or line_row["part"] == part:
            return line_row["cents"]
        parts = self._rows(
            "SELECT * FROM parts WHERE work = ? AND status = 'active' ORDER BY id",
            (line_row["work"],))
        if len(parts) < 2:
            return line_row["cents"]
        amounts = split_weighted(line_row["cents"], {p["id"]: p["weight"] for p in parts})
        return amounts.get(part, line_row["cents"])

    def _unbind_charge(self, claim_id, charge_id):
        if not claim_id:
            return
        self.conn.execute(
            "DELETE FROM claim_charges WHERE claim_id = ? AND charge_id = ?",
            (claim_id, charge_id))
        self.conn.execute(
            "UPDATE charges SET status = 'open', claim_id = NULL WHERE id = ? "
            "AND status = 'claimed'", (charge_id,))

    def _void_unpaid_claim_charge(self, charge):
        """复核/移交撤回未付份额时保留审计痕迹：标记 reversed 而非删除。"""
        claim_id = charge["claim_id"]
        self.conn.execute(
            "UPDATE charges SET status = 'reversed', claim_id = NULL WHERE id = ?",
            (charge["id"],))
        self._unbind_charge(claim_id, charge["id"])
        self.conn.execute(
            "UPDATE charges SET status = 'reversed' WHERE id = ?", (charge["id"],))
        self._recalc_claim(claim_id)

    def dismiss_review(self, review_id, party, reason=None):
        self.require_role(party, "owner", "cashier")
        with self.write():
            review = self.review(review_id)
            if review["status"] != "pending":
                raise Conflict("复核已处理", review=review_id)
            if review["part"]:
                self.conn.execute(
                    "UPDATE parts SET usage = 'unknown' WHERE work = ? AND id = ?",
                    (review["work"], review["part"]))
            self.conn.execute(
                "UPDATE reviews SET status = 'dismissed', decided_at = ?, "
                "reason = COALESCE(?, reason) WHERE id = ?",
                (self.clock(), reason, review_id))
            self._event("review.dismissed", str(review_id))
            return self.review(review_id)

    # ----------------------------------------------------------- adjustments
    def _make_adjustment(self, work, part, invoice, line, from_party, to_party, cents,
                         reason, review_id=None, handover_id=None):
        return self._insert(
            "INSERT INTO adjustments (work, part, invoice, line, from_party, to_party, "
            "cents, reason, review_id, handover_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (work, part, invoice, line, from_party, to_party, cents, reason, review_id,
             handover_id, self.clock()))

    def adjustments(self, *, status=None, party=None):
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if party:
            clauses.append("(from_party = ? OR to_party = ?)")
            params.extend([party, party])
        sql = "SELECT * FROM adjustments"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return self._rows(sql + " ORDER BY id", params)

    def settle_adjustment(self, adjustment_id, *, destination="operator", payment_ref=None):
        """差额单独办结，原已付部分不改写。"""
        with self.write():
            adj = self._row("SELECT * FROM adjustments WHERE id = ?", (adjustment_id,))
            if not adj:
                raise NotFound("未找到差额", adjustment=adjustment_id)
            if adj["status"] != "open":
                raise Conflict("差额已处理", adjustment=adjustment_id, status=adj["status"])
            self._insert(
                "INSERT INTO settlements (adjustment_id, payment_ref, destination, cents, at) "
                "VALUES (?, ?, ?, ?, ?)",
                (adjustment_id, payment_ref, destination, adj["cents"], self.clock()))
            self.conn.execute("UPDATE adjustments SET status = 'settled' WHERE id = ?",
                              (adjustment_id,))
            self._event("adjustment.settled", str(adjustment_id), cents=adj["cents"])
            return self._row("SELECT * FROM adjustments WHERE id = ?", (adjustment_id,))

    # -------------------------------------------------------------- handover
    def request_handover(self, work, party, *, mode="transfer", to_party=None, part=None,
                         revision=None, period=None):
        """接用方撤出或转接；冻结当时的受理与支付范围。"""
        if mode not in ("withdraw", "transfer"):
            raise Invalid("mode 应为 withdraw 或 transfer", mode=mode)
        self._ensure_operator(party)
        if mode == "transfer":
            if not to_party:
                raise Invalid("转接需指定接收方")
            self._ensure_operator(to_party)
            if to_party == party:
                raise Invalid("转出方与接收方不能相同")
        if revision is not None and not isinstance(revision, dict):
            raise Invalid("revision 应为对象")
        with self.write():
            self.work(work)
            if not self._row("SELECT 1 FROM takes WHERE work = ? AND party = ?",
                             (work, party)):
                raise Conflict("该方尚未接用，不能撤出或转接", work=work, party=party)
            if part and not self._row(
                    "SELECT 1 FROM part_takes WHERE work = ? AND part = ? AND party = ?",
                    (work, part, party)):
                raise Conflict("该方未接用此部位", work=work, part=part)
            pending = self._row(
                "SELECT 1 FROM handovers WHERE work = ? AND COALESCE(part, '') = "
                "COALESCE(?, '') AND status = 'pending'", (work, part))
            if pending:
                raise Conflict("已有待确认的移交", work=work, part=part)
            required = {party}
            if mode == "transfer":
                required.add(to_party)
            owner = self._owner_of_work(work)
            if owner:
                required.add(owner)
            snapshot = self._snapshot(work, part)
            handover_id = self._insert(
                "INSERT INTO handovers (work, part, period, mode, from_party, to_party, "
                "revision, required, snapshot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (work, part, parse_period(period), mode, party, to_party,
                 dumps(revision) if revision else None, dumps(sorted(required)),
                 dumps(snapshot), self.clock()))
            self.conn.execute(
                "INSERT INTO handover_confirmations (handover_id, party, at) VALUES (?, ?, ?)",
                (handover_id, party, self.clock()))
            self._event("handover.requested", str(handover_id), work=work, mode=mode)
            return self.handover(handover_id)

    def _snapshot(self, work, part):
        charge_sql = "SELECT * FROM charges WHERE work = ?"
        charge_params = [work]
        if part:
            charge_sql += " AND part = ?"
            charge_params.append(part)
        charges = self._rows(charge_sql + " ORDER BY id", charge_params)
        claim_ids = sorted({c["claim_id"] for c in charges if c["claim_id"]})
        claims = [self.claim(cid) for cid in claim_ids]
        payments = self._rows(
            "SELECT ref, status, amount_cents, claim_id, destination FROM payments "
            "WHERE work = ?" + (" OR claim_id IN (" + ",".join("?" * len(claims)) + ")"
                                if claims else "") + " ORDER BY ref",
            (work, *claim_ids))
        return {"at": self.clock(), "work": self.work(work), "part": part,
                "claims": claims, "charges": charges, "payments": payments,
                "delivered": self._is_delivered(work, part)}

    def _is_delivered(self, work, part):
        row = self._row("SELECT status FROM works WHERE id = ?", (work,))
        if row and row["status"] == "delivered":
            return True
        if part:
            item = self._row("SELECT delivered_at FROM parts WHERE work = ? AND id = ?",
                             (work, part))
            return bool(item and item["delivered_at"] is not None)
        return False

    def handover(self, handover_id):
        row = self._row("SELECT * FROM handovers WHERE id = ?", (handover_id,))
        if not row:
            raise NotFound("未找到移交", handover=handover_id)
        row["snapshot"] = loads(row["snapshot"])
        row["revision"] = loads(row["revision"]) if row["revision"] else None
        row["required"] = loads(row["required"]) if row["required"] else []
        row["result"] = loads(row["result"]) if row["result"] else None
        row["confirmations"] = [r["party"] for r in self._rows(
            "SELECT party FROM handover_confirmations WHERE handover_id = ? ORDER BY party",
            (handover_id,))]
        return row

    def handovers(self, *, status=None):
        sql = "SELECT id FROM handovers"
        params = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        return [self.handover(r["id"]) for r in self._rows(sql + " ORDER BY id", params)]

    def confirm_handover(self, handover_id, party):
        self.party(party)
        with self.write():
            ho = self.handover(handover_id)
            if ho["status"] != "pending":
                raise Conflict("移交已处理", handover=handover_id, status=ho["status"])
            if party not in ho["required"]:
                raise Forbidden("该方不在移交确认范围", handover=handover_id, party=party)
            self.conn.execute(
                "INSERT OR IGNORE INTO handover_confirmations (handover_id, party, at) "
                "VALUES (?, ?, ?)", (handover_id, party, self.clock()))
            confirmed = {r["party"] for r in self._rows(
                "SELECT party FROM handover_confirmations WHERE handover_id = ?",
                (handover_id,))}
            if set(ho["required"]).issubset(confirmed):
                self._apply_handover(ho)
            return self.handover(handover_id)

    def _revision_shares(self, ho):
        revision = ho["revision"] or {}
        shares = revision.get("shares")
        if shares:
            return self._normalise_share_input(shares)
        old = self._allocation_exact(ho["work"], ho["part"]) if ho["part"] \
            else self.current_allocation(ho["work"])
        if not old:
            if ho["mode"] == "transfer":
                return {ho["to_party"]: 10000}
            return None
        shares = dict(old["shares"])
        if ho["mode"] == "transfer":
            shares[ho["to_party"]] = shares.get(ho["to_party"], 0) + shares.pop(
                ho["from_party"], 0)
        else:
            shares.pop(ho["from_party"], None)
            total = sum(shares.values())
            if not total:
                return None
            scaled = {party: bps * 10000 // total for party, bps in shares.items()}
            drift = 10000 - sum(scaled.values())
            if drift:
                party = max(sorted(scaled), key=lambda p: scaled[p])
                scaled[party] += drift
            shares = scaled
        return shares

    def _apply_handover(self, ho):
        work, part = ho["work"], ho["part"]
        delivered = bool(ho["snapshot"].get("delivered")) or self._is_delivered(work, part)
        shares = self._revision_shares(ho)
        result = {"delivered": delivered, "restored": [], "adjustments": [],
                  "reallocated": [], "mode": ho["mode"]}
        if delivered:
            # 已交付并支付：保留当时受理范围，只登记差额；此后的支出按修订
            # 约定承担，不影响已经受理的历史份额。
            result["restored"] = [
                {"claim_id": c["id"], "amount_cents": c["amount_cents"],
                 "settlements": c["settlements"]} for c in ho["snapshot"]["claims"]]
            if shares:
                # 原有份额继续按原样受理；后续新支出按修订后的约定承担。
                self._write_allocation(work, part, shares, "handover", ho["from_party"])
            if ho["mode"] == "transfer":
                for charge in ho["snapshot"]["charges"]:
                    if charge["bearer"] == ho["to_party"] or charge["paid_cents"] <= 0:
                        continue
                    adjustment_id = self._make_adjustment(
                        work, charge["part"], charge["invoice"], charge["line"],
                        charge["bearer"], ho["to_party"], charge["paid_cents"],
                        "转接差额", handover_id=ho["id"])
                    result["adjustments"].append(
                        self._row("SELECT * FROM adjustments WHERE id = ?",
                                  (adjustment_id,)))
        else:
            # 尚未交付：未付份额按修订约定重算；已付部分转差额，保留原结算。
            for charge in ho["snapshot"]["charges"]:
                if charge["origin_group"] != "base":
                    continue
                if charge["status"] == "open":
                    self.conn.execute("DELETE FROM charges WHERE id = ?", (charge["id"],))
                elif charge["status"] == "claimed" and charge["paid_cents"] == 0:
                    self._unbind_charge(charge["claim_id"], charge["id"])
                    self.conn.execute("DELETE FROM charges WHERE id = ?", (charge["id"],))
                    self._recalc_claim(charge["claim_id"])
                elif charge["paid_cents"] > 0:
                    if ho["mode"] == "transfer" and charge["bearer"] != ho["to_party"]:
                        self._make_adjustment(
                            work, charge["part"], charge["invoice"], charge["line"],
                            charge["bearer"], ho["to_party"], charge["paid_cents"],
                            "转接差额", handover_id=ho["id"])
                    if charge["status"] == "claimed":
                        self._unbind_charge(charge["claim_id"], charge["id"])
                        self._recalc_claim(charge["claim_id"])
                    self.conn.execute(
                        "UPDATE charges SET status = 'reversed' WHERE id = ?",
                        (charge["id"],))
            if shares:
                self._write_allocation(work, part, shares, "handover", ho["from_party"])
                self._rebuild_base_charges(work, part, shares, ho)
            result["reallocated"] = self._rows(
                "SELECT * FROM charges WHERE work = ? AND status = 'open' ORDER BY id",
                (work,))
        if ho["mode"] == "withdraw":
            self._drop_taker(work, part, ho["from_party"])
        else:
            self._drop_taker(work, part, ho["from_party"])
            self.conn.execute(
                "INSERT OR IGNORE INTO takes (work, party, at) VALUES (?, ?, ?)",
                (work, ho["to_party"], self.clock()))
            if part:
                self.conn.execute(
                    "INSERT OR IGNORE INTO part_takes (work, part, party, at) "
                    "VALUES (?, ?, ?, ?)", (work, part, ho["to_party"], self.clock()))
        self.conn.execute(
            "UPDATE handovers SET status = 'applied', confirmed_at = ?, result = ? "
            "WHERE id = ?", (self.clock(), dumps(result), ho["id"]))
        self._event("handover.applied", str(ho["id"]), work=work, mode=ho["mode"],
                    delivered=delivered, adjustments=len(result["adjustments"]))

    def _rebuild_base_charges(self, work, part, shares, ho):
        lines = self._rows(
            "SELECT * FROM invoice_lines WHERE work = ? AND status = 'attributed' "
            "ORDER BY invoice, line", (work,))
        for line_row in lines:
            if part:
                if line_row["part"] != part:
                    continue
                target = self._part_amount(line_row, part)
            else:
                if line_row["part"]:
                    continue
                target = line_row["cents"]
            scope = part or ""
            for party, amount in sorted(split_amount(target, shares).items()):
                existing = self._row(
                    "SELECT * FROM charges WHERE invoice = ? AND line = ? AND part = ? "
                    "AND bearer = ? AND origin_group = 'base'",
                    (line_row["invoice"], line_row["line"], scope, party))
                if existing is None:
                    self._insert_charge(line_row["invoice"], line_row["line"], work,
                                        part, party, amount, "handover")
                elif existing["status"] == "open" and existing["cents"] != amount:
                    self.conn.execute("UPDATE charges SET cents = ? WHERE id = ?",
                                      (amount, existing["id"]))

    def _drop_taker(self, work, part, party):
        if part:
            self.conn.execute(
                "DELETE FROM part_takes WHERE work = ? AND part = ? AND party = ?",
                (work, part, party))
            remaining = self._row(
                "SELECT COUNT(*) AS n FROM part_takes WHERE work = ? AND part = ?",
                (work, part))["n"]
            if not remaining:
                self.conn.execute(
                    "UPDATE parts SET status = 'vacated' WHERE work = ? AND id = ?",
                    (work, part))
            still = self._row(
                "SELECT COUNT(*) AS n FROM part_takes WHERE work = ? AND party = ?",
                (work, party))["n"]
            if not still:
                self.conn.execute("DELETE FROM takes WHERE work = ? AND party = ?",
                                  (work, party))
        else:
            self.conn.execute("DELETE FROM takes WHERE work = ? AND party = ?", (work, party))
            self.conn.execute("DELETE FROM part_takes WHERE work = ? AND party = ?",
                              (work, party))

    # ------------------------------------------------------------ submissions
    def submit(self, kind, party, *, site=None, work=None, part=None, payload=None,
               idempotency_key=None):
        """各方提交可用空间、费用或分摊，等待相关方批准。"""
        if kind not in ("space", "expense", "allocation"):
            raise Invalid("未知提交类型", kind=kind)
        role = self.party(party)["role"]
        if role != "operator" and not (role == "builder" and kind == "expense"):
            raise Forbidden("该操作需要经办方身份", party=party, role=role)
        payload = dict(payload or {})
        with self.write():
            if idempotency_key:
                prior = self._row(
                    "SELECT id FROM submissions WHERE idempotency_key = ?",
                    (idempotency_key,))
                if prior:
                    return self.submission(prior["id"])
            payload.pop("_party", None)
            if kind == "space":
                self._validate_space(site, work, part, payload)
            elif kind == "expense":
                payload["_party"] = party
                try:
                    self._validate_expense(work, part, payload)
                finally:
                    payload.pop("_party", None)
            else:
                self._validate_allocation(work, part, payload)
            submission_id = self._insert(
                "INSERT INTO submissions (kind, party, site, work, part, payload, "
                "created_at, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, party, site, work, part, dumps(payload), self.clock(),
                 idempotency_key))
            # 提交方视为已知悉并同意自己的申报。
            self.conn.execute(
                "INSERT INTO submission_confirmations (submission_id, party, at) "
                "VALUES (?, ?, ?)", (submission_id, party, self.clock()))
            self._event("submission.created", str(submission_id), kind=kind, party=party)
            return self.submission(submission_id)

    def _validate_space(self, site, work, part, payload):
        if not work:
            raise Invalid("可用空间提交需给出 work")
        work_row = self._row("SELECT * FROM works WHERE id = ?", (work,))
        if not work_row:
            raise NotFound("未登记设施", work=work)
        if not part:
            raise Invalid("可用空间提交需给出 part")
        usage = payload.get("usage", "unknown")
        if usage not in ("dedicated", "shared", "unknown"):
            raise Invalid("usage 不合法", usage=usage)
        if usage == "dedicated" and not (payload.get("user") or payload.get("operator")):
            raise Invalid("独立使用需给出承担方")
        weight = payload.get("weight", 1)
        if not isinstance(weight, int) or weight <= 0:
            raise Invalid("weight 应为正整数")
        cents = payload.get("cents")
        if cents is not None and (not isinstance(cents, int) or cents <= 0):
            raise Invalid("cents 应为正整数（分）")
        if site and work_row["site"] and site != work_row["site"]:
            raise Conflict("设施不属于该站点", work=work, site=site)

    def _validate_expense(self, work, part, payload):
        if not work:
            raise Invalid("费用提交需给出 work")
        work_row = self._row("SELECT * FROM works WHERE id = ?", (work,))
        if not work_row:
            raise NotFound("未登记设施", work=work)
        if self.party(payload["_party"])["role"] == "builder" \
                and work_row["status"] == "registered":
            raise Conflict("实物尚未验收，施工方只能申报已确认的费用", work=work)
        if part and not self._row("SELECT 1 FROM parts WHERE work = ? AND id = ?",
                                  (work, part)):
            raise NotFound("未登记接用部位", work=work, part=part)
        if not payload.get("invoice") or not payload.get("line"):
            raise Invalid("费用提交需给出 invoice 与 line")
        cents = payload.get("cents")
        if not isinstance(cents, int) or cents <= 0:
            raise Invalid("cents 应为正整数（分）")

    def _validate_allocation(self, work, part, payload):
        if not work:
            raise Invalid("分摊提交需给出 work")
        if not self._row("SELECT 1 FROM works WHERE id = ?", (work,)):
            raise NotFound("未登记设施", work=work)
        if part and not self._row("SELECT 1 FROM parts WHERE work = ? AND id = ?",
                                  (work, part)):
            raise NotFound("未登记接用部位", work=work, part=part)
        payload["shares"] = self._normalise_share_input(payload.get("shares"))

    def submission(self, submission_id):
        row = self._row("SELECT * FROM submissions WHERE id = ?", (submission_id,))
        if not row:
            raise NotFound("未找到提交", submission=submission_id)
        row["payload"] = loads(row["payload"])
        row["required"] = self._submission_required(row)
        row["confirmations"] = [r["party"] for r in self._rows(
            "SELECT party FROM submission_confirmations WHERE submission_id = ? "
            "ORDER BY party", (submission_id,))]
        return row

    def submissions(self, *, kind=None, status=None):
        clauses, params = [], []
        for column, value in (("kind", kind), ("status", status)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        sql = "SELECT id FROM submissions"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return [self.submission(r["id"]) for r in self._rows(sql + " ORDER BY id", params)]

    def _submission_required(self, row):
        payload = row["payload"] if isinstance(row["payload"], dict) else loads(row["payload"])
        kind = row["kind"]
        if kind == "allocation":
            return sorted(payload["shares"])
        work_row = self._row("SELECT * FROM works WHERE id = ?", (row["work"],))
        site = work_row["site"] if work_row else row["site"]
        required = {row["party"]}
        if site:
            required.update(self._site_operators(site))
        owner = self._owner_of_work(row["work"]) if work_row else None
        if owner:
            required.add(owner)
        if kind == "space" and payload.get("usage") == "dedicated":
            user = payload.get("user") or payload.get("operator")
            if user:
                required.add(user)
        return sorted(required)

    def confirm_submission(self, submission_id, party):
        self.party(party)
        with self.write():
            row = self.submission(submission_id)
            if row["status"] != "submitted":
                raise Conflict("提交已处理", submission=submission_id,
                               status=row["status"])
            if party in row["confirmations"]:
                raise Conflict("该方已确认", submission=submission_id, party=party)
            if party not in row["required"]:
                raise Forbidden("该方不在确认范围", submission=submission_id, party=party)
            self.conn.execute(
                "INSERT INTO submission_confirmations (submission_id, party, at) "
                "VALUES (?, ?, ?)", (submission_id, party, self.clock()))
            confirmed = set(row["confirmations"]) | {party}
            if not set(row["required"]).issubset(confirmed):
                return self.submission(submission_id)
            self.conn.execute(
                "UPDATE submissions SET status = 'confirmed', decided_at = ? WHERE id = ?",
                (self.clock(), submission_id))
            self._apply_submission(row)
            self._event("submission.confirmed", str(submission_id), kind=row["kind"])
            return self.submission(submission_id)

    def reject_submission(self, submission_id, party, reason=None):
        self.party(party)
        with self.write():
            row = self.submission(submission_id)
            if row["status"] != "submitted":
                raise Conflict("提交已处理", submission=submission_id,
                               status=row["status"])
            if party not in row["required"]:
                raise Forbidden("该方不在确认范围", submission=submission_id, party=party)
            self.conn.execute(
                "UPDATE submissions SET status = 'rejected', decided_at = ? WHERE id = ?",
                (self.clock(), submission_id))
            self._event("submission.rejected", str(submission_id), party=party,
                        reason=reason)
            return self.submission(submission_id)

    def _apply_submission(self, row):
        payload = row["payload"]
        if row["kind"] == "space":
            usage = payload.get("usage", "unknown")
            user = payload.get("user") or payload.get("operator")
            self.conn.execute(
                "INSERT INTO parts (id, work, usage, user, weight, cents) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(work, id) DO UPDATE SET "
                "usage = excluded.usage, user = excluded.user, weight = excluded.weight, "
                "cents = COALESCE(excluded.cents, parts.cents)",
                (row["part"], row["work"], usage,
                 user if usage == "dedicated" else None,
                 payload.get("weight", 1), payload.get("cents")))
            self.conn.execute(
                "UPDATE works SET status = CASE WHEN status = 'registered' THEN 'accepted' "
                "ELSE status END, accepted_at = COALESCE(accepted_at, ?) WHERE id = ?",
                (self.clock(), row["work"]))
        elif row["kind"] == "allocation":
            self._write_allocation(row["work"], row["part"], payload["shares"],
                                   "submission", row["party"])
        else:
            self.add_invoice_line(
                payload["invoice"], payload["line"], work=row["work"],
                part=row["part"] or payload.get("part"), cents=payload["cents"],
                period=payload.get("period"))

    # ---------------------------------------------------------------- intake
    def apply_intake(self, messages):
        """把到达记录按序落成业务事实；同一 sequence 只应用一次。"""
        results = []
        for message in sorted(messages, key=lambda m: m.get("sequence", 0)):
            sequence = message.get("sequence")
            if not isinstance(sequence, int) or sequence < 1:
                raise Invalid("到达记录缺少合法 sequence", message=message)
            with self.write():
                if self._row("SELECT 1 FROM intake WHERE sequence = ?", (sequence,)):
                    results.append({"sequence": sequence, "status": "duplicate"})
                    continue
                facts = message.get("facts") or {}
                outcome, note = "applied", None
                try:
                    self._apply_fact(message)
                except (Conflict, Invalid, NotFound) as exc:
                    outcome, note = "deferred", f"{exc.code}: {exc.message}"
                self._insert(
                    "INSERT INTO intake (sequence, at, source, facts, applied_at, outcome, "
                    "note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (sequence, message.get("at"), message.get("source"), dumps(facts),
                     self.clock(), outcome, note))
                self._event("intake." + outcome, str(sequence), note=note)
                results.append({"sequence": sequence, "status": outcome,
                                **({"reason": note} if note else {})})
        return results

    def _apply_fact(self, message):
        facts = message.get("facts") or {}
        at = facts.get("at", message.get("at"))
        if "accepted" in facts:
            for work in facts["accepted"]:
                self.accept_work(work, part=facts.get("part"), at=at,
                                 party=facts.get("operator"))
        if "delivered" in facts:
            for work in facts["delivered"]:
                self.conn.execute(
                    "UPDATE works SET status = 'delivered', delivered_at = ? WHERE id = ?",
                    (at, work))
                if facts.get("part"):
                    self.conn.execute(
                        "UPDATE parts SET delivered_at = ? WHERE work = ? AND id = ?",
                        (at, work, facts["part"]))
        if "takes" in facts:
            operator = facts.get("operator")
            if not operator:
                raise Invalid("接用记录缺少 operator")
            for work in facts["takes"]:
                self.take(work, operator, part=facts.get("part"), at=at)
        if "payment" in facts and facts.get("state") == "submitted":
            ref = facts["payment"]
            existing = self._row("SELECT * FROM payments WHERE ref = ?", (ref,))
            if existing:
                if existing["status"] == "reserved" and existing["unassigned"]:
                    self.conn.execute(
                        "UPDATE payments SET invoice = COALESCE(invoice, ?), "
                        "line = COALESCE(line, ?), work = COALESCE(work, ?) WHERE ref = ?",
                        (facts.get("invoice"), facts.get("line"), facts.get("work"), ref))
                return
            invoice = facts.get("invoice")
            if not invoice and facts.get("line"):
                matches = self._rows(
                    "SELECT DISTINCT invoice FROM invoice_lines WHERE line = ?",
                    (facts["line"],))
                if len(matches) == 1:
                    invoice = matches[0]["invoice"]
            self.record_submitted_payment(
                ref, period=facts.get("period"), invoice=invoice,
                line=facts.get("line"), work=facts.get("work"))
        if facts.get("allocation") in ("dedicated", "shared") and facts.get("work"):
            work = facts["work"]
            part = facts.get("part")
            operator = facts.get("operator")
            usage = facts["allocation"]
            work_row = self._row("SELECT * FROM works WHERE id = ?", (work,))
            if not work_row:
                raise NotFound("未登记设施", work=work)
            if part:
                part_row = self._row(
                    "SELECT * FROM parts WHERE work = ? AND id = ?", (work, part))
                if not part_row:
                    self._insert(
                        "INSERT INTO parts (id, work) VALUES (?, ?)", (part, work))
                if usage == "dedicated":
                    if not operator:
                        raise Invalid("独立使用记录缺少 operator")
                    # 原本按共用支付、实际为独立使用：开复核重核负担方，
                    # 不直接改写已付份额。没有可复核支出时仅记录事实。
                    prior = self._rows(
                        "SELECT * FROM charges WHERE work = ? AND part = ? "
                        "AND origin_group = 'base' AND status != 'reversed'",
                        (work, part))
                    shared_before = any(c["basis"] not in ("dedicated", "review", "handover")
                                        for c in prior)
                    existing_usage = self._row(
                        "SELECT usage FROM parts WHERE work = ? AND id = ?", (work, part))
                    usage_before = existing_usage["usage"] if existing_usage else "unknown"
                    if prior and (shared_before or usage_before == "shared"):
                        try:
                            self.open_review(work, part, reason="回路实际独立使用",
                                             party=operator)
                        except Conflict:
                            self.conn.execute(
                                "UPDATE parts SET usage = 'dedicated', user = ? "
                                "WHERE work = ? AND id = ?", (operator, work, part))
                    else:
                        self.conn.execute(
                            "UPDATE parts SET usage = 'dedicated', user = ? "
                            "WHERE work = ? AND id = ?", (operator, work, part))
                else:
                    self.conn.execute(
                        "UPDATE parts SET usage = 'shared' WHERE work = ? AND id = ?",
                        (work, part))
            elif usage == "shared":
                self.conn.execute(
                    "UPDATE works SET type = 'shared' WHERE id = ?", (work,))

    def intake_records(self):
        return self._rows("SELECT * FROM intake ORDER BY sequence")

    # ------------------------------------------------------- circuit evidence
    def apply_circuit_proof(self, work, evidence):
        """按电力方回传的回路范围登记部位。

        ``points`` 是本次完成的回路；``<后缀>_customer`` 表示该回路独立由
        某人使用，``<后缀>_usage`` 为 ``joint``/``shared`` 表示仍共用。
        若某回路原按共用支付、现在证据显示为独立使用，则开复核而不是直接
        改写已付份额。
        """
        if not isinstance(evidence, dict):
            raise Invalid("回路回执应为对象")
        points = evidence.get("points")
        if not isinstance(points, list) or not points:
            raise Invalid("回路回执缺少 points")
        work_row = self.work(work)
        parsed = []
        for point in points:
            suffix = str(point).split("-")[-1]
            customer = evidence.get(f"{suffix}_customer")
            declared = evidence.get(f"{suffix}_usage")
            if customer:
                usage = "dedicated"
            elif declared in ("joint", "shared"):
                usage = "shared"
            elif declared in ("dedicated", "independent"):
                usage = "dedicated"
            else:
                usage = "unknown"
            if usage == "dedicated" and not customer:
                raise Invalid("独立使用回路需要承担方", work=work, part=point)
            if customer:
                self.party(customer)
            parsed.append({"part": point, "usage": usage, "user": customer})
        results = []
        with self.write():
            prior_usage = {}
            for item in parsed:
                row = self._row("SELECT * FROM parts WHERE work = ? AND id = ?",
                                (work, item["part"]))
                prior_usage[item["part"]] = row["usage"] if row else None
                if row is None:
                    # 先按仍共用登记，让原有整项份额能落到各部位。
                    self._insert(
                        "INSERT INTO parts (id, work, usage, user) VALUES (?, ?, ?, ?)",
                        (item["part"], work,
                         "shared" if item["usage"] == "shared" else "unknown",
                         item["user"] if item["usage"] == "dedicated" else None))
            self.prepare_charges(work=work)
            for item in parsed:
                before = prior_usage[item["part"]]
                # 只要该回路曾按共用份额支付过，独立使用的结论就要复核重核。
                had_shared = bool(self._rows(
                    "SELECT 1 FROM charges WHERE work = ? AND part = ? "
                    "AND origin_group = 'base' AND status != 'reversed' AND basis != 'dedicated'",
                    (work, item["part"])))
                self.conn.execute(
                    "UPDATE parts SET usage = ?, user = COALESCE(?, user) "
                    "WHERE work = ? AND id = ?",
                    (item["usage"], item["user"], work, item["part"]))
                review_id = None
                if item["usage"] == "dedicated" and had_shared:
                    try:
                        review_id = self.open_review(
                            work, item["part"], reason="回路回执显示独立使用",
                            party=item["user"])["id"]
                    except Conflict:
                        review_id = None
                results.append({**item, "review": review_id})
            if any(item["usage"] == "shared" for item in results) \
                    and work_row["type"] == "unknown":
                self.conn.execute("UPDATE works SET type = 'shared' WHERE id = ?",
                                  (work,))
            self._event("circuit.proof", work, points=results)
        return {"work": work, "parts": results, "work_detail": self.work(work)}

    # ----------------------------------------------------------------- views
    def view_party(self, party):
        """各方查看自己正在使用、待移交的范围与待确认责任。"""
        self.party(party)
        using = self._rows(
            "SELECT w.id, w.type, w.status, w.accepted_at, w.delivered_at, "
            "(SELECT group_concat(pt.part) FROM part_takes pt WHERE pt.work = w.id "
            "AND pt.party = ?) AS part_ids FROM works w JOIN takes t ON t.work = w.id "
            "WHERE t.party = ? ORDER BY w.id", (party, party))
        for row in using:
            ids = row.pop("part_ids", None)
            row["parts"] = ids.split(",") if ids else []
        handovers = []
        for ho in self.handovers():
            if ho["status"] == "pending" and (
                    party in (ho["from_party"], ho["to_party"])
                    or party in ho["required"]):
                handovers.append({k: ho[k] for k in (
                    "id", "work", "part", "mode", "from_party", "to_party", "status",
                    "created_at")})
        responsibilities = self._rows(
            "SELECT c.*, cl.status AS claim_status, cl.mode AS claim_mode, "
            "cl.period AS claim_period FROM charges c "
            "LEFT JOIN claims cl ON cl.id = c.claim_id "
            "WHERE c.bearer = ? AND c.status IN ('open', 'claimed', 'settled') "
            "ORDER BY c.id", (party,))
        to_confirm = []
        for claim in self.claims():
            if claim["status"] == "pending_confirmation" and party in claim["required"] \
                    and party not in claim["confirmations"]:
                to_confirm.append({k: claim[k] for k in (
                    "id", "period", "bearer", "amount_cents", "mode", "status")})
        submissions = [s for s in self.submissions(status="submitted")
                       if party in s["required"] and party not in s["confirmations"]]
        return {
            "party": self.party(party),
            "using": using,
            "pending_handovers": handovers,
            "responsibilities": responsibilities,
            "to_confirm": to_confirm,
            "pending_submissions": [{k: s[k] for k in (
                "id", "kind", "party", "work", "part", "status")} for s in submissions],
            "adjustments": self.adjustments(party=party),
        }

    def view_owner(self):
        return {
            "handovers": [h for h in self.handovers() if h["status"] == "pending"],
            "reviews": self.reviews(status="pending"),
            "submissions": self.submissions(status="submitted"),
            "works": self.works(),
            "adjustments": self.adjustments(status="open"),
        }

    def view_cashier(self, period=None):
        period = parse_period(period)
        return {
            "period": period,
            "payments": self.payments(period=period),
            "pending_payments": self.pending_payments(period),
            "budget": self.budget_state(period),
            "claims": self.claims(period=period),
            "adjustments": self.adjustments(status="open"),
        }

    def invoice_trail(self, invoice, line=None):
        """资金方逐张发票追到设施、负担方与结算去向。"""
        sql = "SELECT * FROM invoice_lines WHERE invoice = ?"
        params = [invoice]
        if line:
            sql += " AND line = ?"
            params.append(line)
        lines = self._rows(sql + " ORDER BY line", params)
        if not lines:
            raise NotFound("未找到发票", invoice=invoice)
        for row in lines:
            charges = self._rows(
                "SELECT c.*, cl.status AS claim_status, cl.mode AS claim_mode, "
                "cl.period AS claim_period FROM charges c "
                "LEFT JOIN claims cl ON cl.id = c.claim_id "
                "WHERE c.invoice = ? AND c.line = ? ORDER BY c.id",
                (invoice, row["line"]))
            row["work_detail"] = (self.work(row["work"]) if row["work"] else None)
            movements = []
            for charge in charges:
                payments = self._rows(
                    "SELECT ref, status, amount_cents, destination, settled_at "
                    "FROM payments WHERE claim_id = ? ORDER BY ref",
                    (charge["claim_id"],)) if charge["claim_id"] else []
                settlements = self._rows(
                    "SELECT * FROM settlements WHERE claim_id = ? ORDER BY id",
                    (charge["claim_id"],)) if charge["claim_id"] else []
                adjustments = self._rows(
                    "SELECT * FROM adjustments WHERE invoice = ? AND line = ? "
                    "AND (from_party = ? OR to_party = ?) ORDER BY id",
                    (invoice, row["line"], charge["bearer"], charge["bearer"]))
                movements.append({"charge": charge, "payments": payments,
                                  "settlements": settlements, "adjustments": adjustments})
            row["movements"] = movements
            row["total_charged"] = sum(
                c["cents"] for c in charges if c["status"] != "reversed")
            row["total_paid"] = sum(c["paid_cents"] for c in charges)
        return {"invoice": invoice, "lines": lines}

    def period_report(self, period):
        period = parse_period(period)
        claims = self._rows(
            "SELECT mode, status, COUNT(*) AS count, SUM(amount_cents) AS cents "
            "FROM claims WHERE period = ? GROUP BY mode, status ORDER BY mode, status",
            (period,))
        payments = self._rows(
            "SELECT status, COUNT(*) AS count, "
            "SUM(COALESCE(amount_cents, expected_cents, 0)) AS cents "
            "FROM payments WHERE period = ? GROUP BY status ORDER BY status", (period,))
        adjustments = self._rows(
            "SELECT status, COUNT(*) AS count, SUM(cents) AS cents FROM adjustments "
            "GROUP BY status ORDER BY status")
        return {"period": period, "claims": claims, "payments": payments,
                "adjustments": adjustments, "budget": self.budget_state(period),
                "pending_payments": self.pending_payments(period)}

    # ---------------------------------------------------------------- periods
    def _ensure_open_period(self, period):
        row = self._row("SELECT * FROM periods WHERE id = ?", (period,))
        if not row:
            self._insert("INSERT INTO periods (id) VALUES (?)", (period,))
        elif row["status"] == "closed":
            raise Conflict("该期间已结账", period=period)

    def close_period(self, period, party):
        self.require_role(party, "cashier", "owner")
        period = parse_period(period)
        with self.write():
            self._ensure_open_period(period)
            pending_claims = self._row(
                "SELECT COUNT(*) AS n FROM claims WHERE period = ? AND status IN "
                "('pending_confirmation', 'approved')", (period,))["n"]
            if pending_claims:
                raise Conflict("仍有未结认领，不能结账", period=period,
                               claims=pending_claims)
            in_flight = self._row(
                "SELECT COUNT(*) AS n FROM payments WHERE period = ? AND status IN "
                "('reserved', 'failed')", (period,))["n"]
            if in_flight:
                raise Conflict("仍有待回执的付款，不能结账", period=period,
                               payments=in_flight)
            self.conn.execute(
                "UPDATE periods SET status = 'closed', closed_at = ? WHERE id = ?",
                (self.clock(), period))
            self._event("period.closed", period)
            return self.period_report(period)

    def events(self):
        return self._rows("SELECT * FROM events ORDER BY id")
