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


class OrderComplaintInputError(OrderQueryError):
    def __init__(self, detail: str) -> None:
        super().__init__(detail, code="invalid_order_complaint", status=400)


class OrderComplaintUnavailable(OrderQueryError):
    def __init__(self, detail: str = "当前订单暂不支持申请售后") -> None:
        super().__init__(detail, code="order_complaint_unavailable", status=409)


class OrderComplaintSubmissionConflict(OrderQueryError):
    def __init__(self, detail: str = "该订单已提交售后申请，请勿重复提交") -> None:
        super().__init__(detail, code="order_complaint_submission_conflict", status=409)


class OrderComplaintSubmissionUnknown(OrderQueryError):
    def __init__(self) -> None:
        super().__init__(
            "提交结果暂时无法确认，请刷新订单状态后再决定是否重试",
            code="order_complaint_submission_unknown",
            status=503,
            retryable=True,
        )


class OrderQuerySessionExpired(OrderQueryError):
    def __init__(self) -> None:
        super().__init__(
            "订单查询会话已过期，请重新查询",
            code="order_query_session_expired",
            status=410,
            retryable=True,
        )


class OrderQueryPasswordRequired(OrderQueryError):
    def __init__(self) -> None:
        super().__init__(
            "请输入订单安全密码",
            code="order_query_password_required",
            status=400,
        )


class OrderQueryPasswordInvalid(OrderQueryError):
    def __init__(self) -> None:
        super().__init__(
            "订单安全密码错误，请重新输入",
            code="order_query_password_invalid",
            status=403,
        )


class OrderQueryPasswordRateLimited(OrderQueryError):
    def __init__(self) -> None:
        super().__init__(
            "订单安全密码尝试次数过多，请稍后重试",
            code="order_query_password_rate_limited",
            status=429,
            retryable=True,
        )


class OrderQueryDetailNotFound(OrderQueryError):
    def __init__(self) -> None:
        super().__init__(
            "当前查询结果中没有该订单",
            code="order_query_detail_not_found",
            status=404,
        )


class OrderQueryDetailUnavailable(OrderQueryError):
    def __init__(self) -> None:
        super().__init__(
            "订单尚未付款或发货信息暂不可用",
            code="order_query_detail_unavailable",
            status=409,
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
