"""SQLite 表结构。金额一律为分，分摊比例为万分比（bps）。

关键约束：

* ``charges`` 是“某张发票行上某个负担方应负担的一份”。唯一索引
  ``(invoice, line, part, bearer, origin_group)`` 保证同一发票行、同一部
  位、同一来源批次里，同一负担方只有一份——共用支出不能重复取得。
* ``claim_charges.charge_id`` 唯一，``payment_charges.charge_id`` 唯一，
  认领和付款都不能重复绑定同一份额。
* ``payments`` 有部分唯一索引保证一个认领同时最多一笔在途付款。
"""

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS parties (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('owner', 'operator', 'cashier', 'funder', 'builder')),
    name TEXT
);

CREATE TABLE IF NOT EXISTS sites (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS site_operators (
    site TEXT NOT NULL,
    party TEXT NOT NULL,
    PRIMARY KEY (site, party)
);

CREATE TABLE IF NOT EXISTS works (
    id TEXT PRIMARY KEY,
    site TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL CHECK (type IN ('dedicated', 'shared', 'unknown')),
    user TEXT,
    status TEXT NOT NULL DEFAULT 'registered'
        CHECK (status IN ('registered', 'accepted', 'taken', 'delivered')),
    accepted_at INTEGER,
    delivered_at INTEGER
);

CREATE TABLE IF NOT EXISTS parts (
    id TEXT NOT NULL,
    work TEXT NOT NULL,
    usage TEXT NOT NULL DEFAULT 'unknown'
        CHECK (usage IN ('dedicated', 'shared', 'unknown')),
    user TEXT,
    weight INTEGER NOT NULL DEFAULT 1 CHECK (weight > 0),
    cents INTEGER CHECK (cents IS NULL OR cents > 0),
    delivered_at INTEGER,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'vacated', 'handed_over')),
    PRIMARY KEY (work, id)
);

CREATE TABLE IF NOT EXISTS takes (
    work TEXT NOT NULL,
    party TEXT NOT NULL,
    at INTEGER,
    PRIMARY KEY (work, party)
);

CREATE TABLE IF NOT EXISTS part_takes (
    work TEXT NOT NULL,
    part TEXT NOT NULL,
    party TEXT NOT NULL,
    at INTEGER,
    PRIMARY KEY (work, part, party)
);

CREATE TABLE IF NOT EXISTS allocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work TEXT NOT NULL,
    part TEXT,
    status TEXT NOT NULL DEFAULT 'confirmed'
        CHECK (status IN ('proposed', 'confirmed', 'superseded')),
    basis TEXT NOT NULL DEFAULT 'manual',
    proposed_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    confirmed_at INTEGER,
    note TEXT
);

CREATE TABLE IF NOT EXISTS allocation_shares (
    allocation_id INTEGER NOT NULL,
    party TEXT NOT NULL,
    bps INTEGER NOT NULL CHECK (bps > 0),
    PRIMARY KEY (allocation_id, party)
);

CREATE TABLE IF NOT EXISTS allocation_confirmations (
    allocation_id INTEGER NOT NULL,
    party TEXT NOT NULL,
    at INTEGER NOT NULL,
    PRIMARY KEY (allocation_id, party)
);

CREATE TABLE IF NOT EXISTS invoices (
    id TEXT PRIMARY KEY,
    period TEXT NOT NULL DEFAULT '',
    vendor TEXT,
    issued_at INTEGER
);

CREATE TABLE IF NOT EXISTS invoice_lines (
    invoice TEXT NOT NULL,
    line TEXT NOT NULL,
    work TEXT,
    part TEXT,
    cents INTEGER NOT NULL CHECK (cents > 0),
    status TEXT NOT NULL DEFAULT 'held'
        CHECK (status IN ('held', 'attributed', 'withdrawn')),
    held_reason TEXT,
    PRIMARY KEY (invoice, line)
);

CREATE TABLE IF NOT EXISTS charges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice TEXT NOT NULL,
    line TEXT NOT NULL,
    work TEXT NOT NULL,
    part TEXT NOT NULL DEFAULT '',
    bearer TEXT NOT NULL,
    cents INTEGER NOT NULL CHECK (cents > 0),
    paid_cents INTEGER NOT NULL DEFAULT 0 CHECK (paid_cents >= 0),
    basis TEXT NOT NULL,
    origin_group TEXT NOT NULL DEFAULT 'base',
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'claimed', 'settled', 'reversed')),
    claim_id INTEGER,
    created_at INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS charge_entitlement
    ON charges (invoice, line, part, bearer, origin_group);

CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period TEXT NOT NULL,
    bearer TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    status TEXT NOT NULL
        CHECK (status IN ('pending_confirmation', 'approved', 'settled',
                          'rejected', 'cancelled')),
    mode TEXT NOT NULL CHECK (mode IN ('single', 'bilateral')),
    required TEXT NOT NULL DEFAULT '[]',
    idempotency_key TEXT UNIQUE,
    created_at INTEGER NOT NULL,
    decided_at INTEGER,
    note TEXT
);

CREATE TABLE IF NOT EXISTS claim_charges (
    claim_id INTEGER NOT NULL,
    charge_id INTEGER NOT NULL UNIQUE,
    PRIMARY KEY (claim_id, charge_id)
);

CREATE TABLE IF NOT EXISTS claim_confirmations (
    claim_id INTEGER NOT NULL,
    party TEXT NOT NULL,
    at INTEGER NOT NULL,
    PRIMARY KEY (claim_id, party)
);

CREATE TABLE IF NOT EXISTS budgets (
    period TEXT NOT NULL,
    party TEXT NOT NULL,
    cents INTEGER NOT NULL CHECK (cents >= 0),
    PRIMARY KEY (period, party)
);

CREATE TABLE IF NOT EXISTS payments (
    ref TEXT PRIMARY KEY,
    claim_id INTEGER,
    period TEXT,
    bearer TEXT,
    desk TEXT NOT NULL DEFAULT 'procurement',
    invoice TEXT,
    line TEXT,
    work TEXT,
    amount_cents INTEGER CHECK (amount_cents IS NULL OR amount_cents > 0),
    expected_cents INTEGER,
    status TEXT NOT NULL
        CHECK (status IN ('reserved', 'confirmed', 'settled', 'failed', 'released')),
    unassigned INTEGER NOT NULL DEFAULT 0,
    receipt TEXT,
    last_error TEXT,
    destination TEXT,
    created_at INTEGER NOT NULL,
    synced_at INTEGER,
    settled_at INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS payment_active_claim
    ON payments (claim_id)
    WHERE claim_id IS NOT NULL AND status IN ('reserved', 'confirmed');

CREATE TABLE IF NOT EXISTS payment_charges (
    payment_ref TEXT NOT NULL,
    charge_id INTEGER NOT NULL UNIQUE,
    PRIMARY KEY (payment_ref, charge_id)
);

CREATE TABLE IF NOT EXISTS settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER,
    adjustment_id INTEGER,
    payment_ref TEXT,
    destination TEXT NOT NULL,
    cents INTEGER NOT NULL CHECK (cents > 0),
    at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work TEXT NOT NULL,
    part TEXT,
    invoice TEXT NOT NULL,
    line TEXT NOT NULL,
    old_detail TEXT,
    new_bearer TEXT,
    amount_cents INTEGER,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'applied', 'dismissed')),
    reason TEXT,
    created_at INTEGER NOT NULL,
    decided_at INTEGER
);

CREATE TABLE IF NOT EXISTS adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work TEXT,
    part TEXT,
    invoice TEXT,
    line TEXT,
    from_party TEXT NOT NULL,
    to_party TEXT,
    cents INTEGER NOT NULL CHECK (cents > 0),
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'settled', 'waived')),
    review_id INTEGER,
    handover_id INTEGER,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS handovers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work TEXT NOT NULL,
    part TEXT,
    period TEXT,
    mode TEXT NOT NULL CHECK (mode IN ('withdraw', 'transfer')),
    from_party TEXT NOT NULL,
    to_party TEXT,
    revision TEXT,
    required TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'applied', 'rejected')),
    snapshot TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    confirmed_at INTEGER,
    result TEXT
);

CREATE TABLE IF NOT EXISTS handover_confirmations (
    handover_id INTEGER NOT NULL,
    party TEXT NOT NULL,
    at INTEGER NOT NULL,
    PRIMARY KEY (handover_id, party)
);

CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('space', 'expense', 'allocation')),
    party TEXT NOT NULL,
    site TEXT,
    work TEXT,
    part TEXT,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'submitted'
        CHECK (status IN ('submitted', 'confirmed', 'rejected')),
    idempotency_key TEXT UNIQUE,
    created_at INTEGER NOT NULL,
    decided_at INTEGER
);

CREATE TABLE IF NOT EXISTS submission_confirmations (
    submission_id INTEGER NOT NULL,
    party TEXT NOT NULL,
    at INTEGER NOT NULL,
    PRIMARY KEY (submission_id, party)
);

CREATE TABLE IF NOT EXISTS periods (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    closed_at INTEGER
);

CREATE TABLE IF NOT EXISTS intake (
    sequence INTEGER PRIMARY KEY,
    at INTEGER,
    source TEXT,
    facts TEXT NOT NULL,
    applied_at INTEGER NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'applied',
    note TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT,
    detail TEXT
);
"""

# 追加：游标等小型状态
SCHEMA += """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""
