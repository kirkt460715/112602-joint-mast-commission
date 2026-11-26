# 乡村共建站点的分阶段接用与共用支出认领

发票金额以分计，回路、发票行和各方接用范围分别登记。同一设施可能分回路报告，采购回执表明实际到账。

所有资料供本地联调，为合成记录。`samples/register.json` 是基础台账，`samples/intake.json` 是送达的业务事实，`samples/receipts.json` 是对端查询合同。

## 本地资料端

使用 Python 3.11.2 标准库。服务只监听回环地址。

```sh
python3 -m unittest -v
python3 partner_feed.py --port 8765
```

- `GET /register` 读取台账。
- `GET /intake?since=0&limit=50` 读取一页到达记录，将返回的 `cursor` 传给下一次查询的 `since`。消息含 `sequence`、`at`、`source`、`facts`，其中 `at` 是收到记录的时间，业务发生时间可在 `facts` 中明确报告。
- `GET /receipt?action=操作&desk=经办方&ref=编号` 以三项值匹配样例。对端返回 `evidence` 作为独立事实。标记 `delayed` 的样例首次查询返回 503，后续可以读取回执。未知编号返回 404。联调端不会自行办理业务操作。

通过 `--samples` 指定另一组同名资料可换样例。临时不可查的状态按联调进程保存。

## 后端（`sitebooks/`）

产权方、两家经办、出纳与资金方在同一套账上逐期办理。金额以分保存，分摊
以万分比（bps）保存；所有写操作在 SQLite `BEGIN IMMEDIATE` 事务内完成。

```sh
python3 partner_feed.py --port 8765              # 终端 1
python3 -m sitebooks.server --port 8770 \
    --db sitebooks.sqlite --partner http://127.0.0.1:8765 --bootstrap   # 终端 2
```

`--bootstrap` 导入台账与默认角色（`tower-a:owner`、`cash-1:cashier`、
`fund-1:funder`，可用 `--bootstrap-actors` 覆盖）。请求通过 `X-Actor` 头带上
操作人身份；除 `/health` 外都要求身份。

```sh
curl -s -H 'X-Actor: tower-a' localhost:8770/register
curl -s -H 'X-Actor: tower-a' -X POST 'localhost:8770/intake/pull?since=0'
curl -s -H 'X-Actor: op-a' localhost:8770/views/party/op-a
```

### 记账结构

| 表 / 概念 | 含义 |
| --- | --- |
| `works` / `parts` | 设施与回路（部位）；用途 `dedicated` / `shared` / `unknown` |
| `allocations` | 经确认的分摊约定，可按整项设施或单个回路约定 |
| `invoice_lines` | 发票行；未确定归属时 `held`，认领时不可见 |
| `charges` | 某发票行上某负担方的一份应负担额；`(invoice,line,part,bearer,origin_group)` 唯一 |
| `claims` | 认领；`single` 单边结、`bilateral` 需相关方确认 |
| `payments` | 付款；`reserved → confirmed → settled`，也可 `failed` / `released` |
| `adjustments` | 复核或移交产生的差额，单独办结 |
| `handovers` | 撤出 / 转接，冻结当时范围 |

关键规则：

- **共用支出不重复取得**：`charges` 的唯一索引与 `claim_charges.charge_id`、
  `payment_charges.charge_id` 的唯一约束叠加，同一份额只能被一笔认领、一笔
  付款占用。
- **单边还是双边**：份额只牵涉一个负担方且无待复核、无未结差额时是
  `single`，否则 `bilateral`，由牵涉方（分摊约定各方、产权方、来源方）确认。
- **回执未到不放出预算**：付款先占用（`reserved`）；对端 503 时保持占用并
  记录原因（`POST /payments/{ref}/sync` 返回 202）；金额不符转 `failed`，占用
  金额不变。结算期间不会因回执未到而再出下一笔。
- **未知归属的付款**：采购平台报来的付款以 `unassigned=1` 占位，同一发票行
  不能再占用第二笔；回执确认后用 `POST /payments/{ref}/assign` 归到认领并按
  份额入账。
- **回路独立使用的复核**：`POST /works/{work}/circuit-proof` 按电力方回传的
  回路登记用途；若该回路曾按共用支付，自动开复核（`reviews`）。复核不改写
  历史：已付份额保留原认领与原结算，只立 `adjustments` 差额；未付份额按新
  范围重算。
- **撤出 / 转接**：`POST /works/{work}/handovers` 冻结范围。未交付的按
  `revision.shares` 重算；已交付、已支付的保留当时受理，差额另办。留在场内
  的一方仍可使用与争议无关的已验收设施。
- **逐张发票追踪**：`GET /invoices/{invoice}/trail` 给出每行 → 设施 → 各负担
  方份额 → 认领 → 付款 → 结算去向 → 调整。

### 主要接口

```
GET  /register                                  台账快照
POST /register/import                           导入台账（或从对端）
POST /intake/pull?since=&limit=                 拉取到达记录并落账
POST /intake                                    直接送入到达记录
GET  /works                         GET /works/{work}
POST /works/{work}/accept|take|deliver
POST /works/{work}/parts            登记回路的用途 / 权重 / 承担方
POST /works/{work}/circuit-proof    按电力方回执登记回路范围
GET/POST /works/{work}/allocations  查看 / 提出分摊
POST /allocations/{id}/confirm
GET  /invoices/{invoice}             GET /invoices/{invoice}/trail
POST /invoices/{invoice}/lines
POST /invoices/{invoice}/lines/{line}/hold|attribute|withdraw
POST /charges/prepare              按空间与分摊生成份额
GET  /charges?bearer=&invoice=&line=&status=&work=
GET/POST /submissions               空间 / 费用 / 分摊申报
POST /submissions/{id}/confirm|reject
GET/POST /claims                    GET /claims/{id}
POST /claims/{id}/confirm|reject
GET  /budgets?period=               PUT /budgets
GET/POST /payments                  GET /payments/{ref}
POST /payments/{ref}/sync|release|assign|settle
GET  /reviews                       POST /reviews
POST /reviews/{id}/apply|dismiss
GET  /adjustments                   POST /adjustments/{id}/settle
GET/POST /handovers                 GET /handovers/{id}
POST /handovers/{id}/confirm
GET  /views/owner  /views/party/{party}  /views/cashier?period=
GET  /reports/period/{period}       POST /periods/{period}/close
```

错误统一为 `{"error": 代码, "message": 说明, "details": {...}}`，并映射到
400/401/403/404/409。

### 角色

- `owner`（产权方）：登记设施、复核定性、查看全站视图。
- `operator`（两家经办）：申报可用空间与费用、发起认领、撤出 / 转接。
- `builder`（施工方）：只能对已验收设施申报经确认的实物与费用。
- `cashier`（出纳）：预算、付款占用与结算、结账。
- `funder`（资金方）：按发票追踪到设施、负担方与结算去向。

### 测试

```sh
python3 -m unittest -v                 # 含资料端与后端
python3 -m unittest tests.test_ledger  # 领域规则
python3 -m unittest tests.test_api     # HTTP 接口与并发
```
