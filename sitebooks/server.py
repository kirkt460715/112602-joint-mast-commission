"""启动后端服务。

    python3 -m sitebooks.server --port 8770 --db sitebooks.sqlite \
        --partner http://127.0.0.1:8765 --bootstrap

``--bootstrap`` 会从对端资料端读取台账并导入；不给出 ``--partner`` 时只能
用 ``POST /register/import`` 自行导入。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .api import serve
from .feed import PartnerClient
from .ledger import SiteBooks


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--db", default="sitebooks.sqlite")
    parser.add_argument("--partner", default=None,
                        help="资料端地址，例如 http://127.0.0.1:8765")
    parser.add_argument("--bootstrap", action="store_true",
                        help="启动时从资料端导入台账")
    parser.add_argument("--bootstrap-actors", default="tower-a:owner,cash-1:cashier,"
                                                      "fund-1:funder")
    args = parser.parse_args(argv)

    if args.db != ":memory:":
        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    books = SiteBooks(args.db)
    for item in filter(None, args.bootstrap_actors.split(",")):
        party, _, role = item.partition(":")
        if party and role:
            books.add_party(party, role)
    client = PartnerClient(args.partner) if args.partner else None
    if args.bootstrap:
        if not client:
            parser.error("--bootstrap 需要同时给出 --partner")
        books.seed(client.register())
        print("已导入台账", file=sys.stderr)
    server = serve(books, client, args.port)
    print(f"后端 http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        books.close()


if __name__ == "__main__":
    main()
