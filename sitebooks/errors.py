"""业务拒绝与输入校验异常。"""


class SiteBooksError(Exception):
    """所有可预期业务错误的基类。"""

    status = 400
    code = "invalid"

    def __init__(self, message=None, **details):
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details

    def as_dict(self):
        body = {"error": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return body


class Invalid(SiteBooksError):
    status = 400
    code = "invalid"


class Unauthorized(SiteBooksError):
    status = 401
    code = "unauthorized"


class Forbidden(SiteBooksError):
    status = 403
    code = "forbidden"


class NotFound(SiteBooksError):
    status = 404
    code = "not_found"


class Conflict(SiteBooksError):
    status = 409
    code = "conflict"


class ReceiptUnavailable(SiteBooksError):
    status = 503
    code = "receipt_temporarily_unavailable"


class ReceiptNotFound(SiteBooksError):
    status = 404
    code = "receipt_not_found"
