"""Domain errors for the order-query integration."""

from __future__ import annotations


class OrderQueryError(RuntimeError):
    def __init__(
        self,
        detail: str,
        *,
        code: str = "order_query_failed",
        status: int = 502,
        retryable: bool = False,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.status = status
        self.retryable = retryable


class OrderQueryInputError(OrderQueryError):
    def __init__(self, detail: str) -> None:
        super().__init__(detail, code="invalid_order_query", status=400)


class OrderQuerySessionExpired(OrderQueryError):
    def __init__(self) -> None:
        super().__init__(
            "订单查询会话已过期，请重新查询",
            code="order_query_session_expired",
            status=410,
            retryable=True,
        )


class OrderQueryBusy(OrderQueryError):
    def __init__(self) -> None:
        super().__init__(
            "订单查询任务较多，请稍后重试",
            code="order_query_busy",
            status=429,
            retryable=True,
        )


class UpstreamOrderError(OrderQueryError):
    pass


class CaptchaVerificationExpired(UpstreamOrderError):
    def __init__(self) -> None:
        super().__init__(
            "订单验证码已失效",
            code="captcha_verification_expired",
            status=502,
            retryable=True,
        )


class CaptchaRecognizerUnavailable(RuntimeError):
    """Raised internally so the service can fall back to manual entry."""
