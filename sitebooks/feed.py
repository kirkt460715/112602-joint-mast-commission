"""读取对端资料端的客户端。

服务端只监听回环地址；这里不缓存业务结论，只把对端回执作为独立事实返回。
"""

from __future__ import annotations

import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .errors import ReceiptNotFound, ReceiptUnavailable


class PartnerError(Exception):
    """对端不可用且不是回执暂时不可查的情形。"""


class PartnerClient:
    def __init__(self, base_url="http://127.0.0.1:8765", timeout=3.0, retries=2, backoff=0.05):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff

    def _get(self, path, query=None):
        url = self.base_url + path
        if query:
            url += "?" + urlencode(query)
        request = Request(url, headers={"Accept": "application/json"})
        last = None
        for attempt in range(self.retries + 1):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                if exc.code == 503:
                    raise ReceiptUnavailable("对端回执暂不可查") from exc
                if exc.code == 404:
                    raise ReceiptNotFound("对端回执未登记") from exc
                raise PartnerError(f"对端返回 {exc.code}") from exc
            except (URLError, TimeoutError) as exc:
                last = exc
                if attempt < self.retries:
                    time.sleep(self.backoff * (attempt + 1))
        raise PartnerError(f"对端连接失败: {last}") from last

    def register(self):
        return self._get("/register")

    def intake(self, since=0, limit=50):
        return self._get("/intake", {"since": since, "limit": limit})

    def receipt(self, action, desk, ref):
        return self._get("/receipt", {"action": action, "desk": desk, "ref": ref})["evidence"]
