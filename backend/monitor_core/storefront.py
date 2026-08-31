"""LinkDong storefront validation, HTTP access, and payload normalization."""

from __future__ import annotations

import html
import json
import re
import uuid
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ALLOWED_HOST = "pay.ldxp.cn"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
USER_AGENT = "LDXP-Local-Monitor/2.0"
VISITOR_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{6,80}")
WAF_MARKERS = (b"aliyunCaptcha", b"aliyunCaptcha-sliding-slider", b"waf_nc", b"u_atoken")
# Aliyun occasionally changes the challenge wrapper while keeping the response
# status at 403.  Keep these secondary markers deliberately narrow and only
# use them together with a 403/HTML response so normal JSON API errors are not
# mistaken for a browser challenge.
WAF_TEXT_MARKERS = (b"aliyun", b"waf", b"captcha", "滑块".encode("utf-8"), "人机验证".encode("utf-8"))
UNLISTED_ERROR_MARKERS = ("商品未上架", "商品不存在", "已下架", "已不在店铺列表")


class WafChallengeRequired(RuntimeError):
    pass


def is_waf_response(raw: bytes, *, content_type: str = "", status: int | None = None) -> bool:
    """Identify an Aliyun WAF challenge without treating business JSON as WAF.

    The storefront normally returns challenge HTML with HTTP 403, but some
    edge nodes return a 200/HTML wrapper or a 403 body without the canonical
    ``aliyunCaptcha`` token.  Exact markers are checked first; fallback text is
    accepted only for HTML or a 403 response.
    """
    body = bytes(raw or b"")
    if any(marker.lower() in body.lower() for marker in WAF_MARKERS):
        return True
    is_html = "text/html" in str(content_type or "").lower()
    if not (is_html or status == 403):
        return False
    lowered = body.lower()
    return any(marker.lower() in lowered for marker in WAF_TEXT_MARKERS) or status == 403 and is_html


class DescriptionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def plain_text(value: Any) -> str:
    parser = DescriptionParser()
    parser.feed(html.unescape(str(value or "")))
    return " ".join(" ".join(parser.parts).split())


def parse_item_url(value: str) -> tuple[str, str]:
    parsed = urlparse((value or "").strip())
    match = re.fullmatch(r"/item/([A-Za-z0-9_-]{3,80})/?", parsed.path)
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST or not match:
        raise ValueError("仅支持 https://pay.ldxp.cn/item/商品编号 格式的商品链接")
    goods_key = match.group(1)
    return goods_key, f"https://{ALLOWED_HOST}/item/{goods_key}"


def parse_shop_url(value: str) -> tuple[str, str]:
    parsed = urlparse((value or "").strip())
    match = re.fullmatch(r"/shop/([A-Za-z0-9_-]{3,80})/?", parsed.path)
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST or not match:
        raise ValueError("仅支持 https://pay.ldxp.cn/shop/店铺Token 格式的店铺链接")
    token = match.group(1)
    return token, f"https://{ALLOWED_HOST}/shop/{token}"


def _first_value(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if item.get(key) not in (None, ""):
            return item[key]
    return None


def _stock_value(item: dict[str, Any]) -> Any:
    keys = (
        "stock",
        "inventory",
        "inventory_count",
        "stock_count",
        "card_count",
        "cards_count",
        "surplus",
        "surplus_count",
        "quantity",
        "remain",
        "remaining",
    )
    containers = [item]
    for key in ("extend", "inventory_info", "stock_info"):
        value = item.get(key)
        if isinstance(value, dict):
            containers.append(value)
    for container in containers:
        value = _first_value(container, keys)
        if value not in (None, ""):
            return value
    return None


def _money(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        number = Decimal(str(value))
        return f"{number:.2f}"
    except (InvalidOperation, ValueError):
        return str(value)


def _compact_number(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        number = Decimal(str(value))
        return format(number.normalize(), "f")
    except (InvalidOperation, ValueError):
        return str(value)


def _upstream_enabled(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes"}


def commerce_tags_from_goods(item: dict[str, Any]) -> list[dict[str, str]]:
    extend = item.get("extend") if isinstance(item.get("extend"), dict) else {}
    tags: list[dict[str, str]] = []

    multiple = item.get("multipleoffers") if isinstance(item.get("multipleoffers"), dict) else {}
    multiple_rules = multiple.get("rules") if isinstance(multiple.get("rules"), list) else []
    if _upstream_enabled(multiple.get("available")) and multiple_rules:
        details = []
        discount_type = str(multiple.get("discount_type") or "")
        for rule in multiple_rules:
            if not isinstance(rule, dict):
                continue
            condition = _compact_number(rule.get("condition"))
            value = _compact_number(rule.get("value"))
            if not condition or not value:
                continue
            if discount_type == "1":
                details.append(f"购{condition}件享{value}折")
            elif discount_type == "2":
                details.append(f"购{condition}件减{value}元")
            else:
                details.append(f"购{condition}件优惠{value}")
        tags.append({
            "key": "multiple_offers",
            "label": "多件折扣",
            "tone": "promotion",
            "detail": "；".join(details) or "购买多件可享优惠",
        })

    discount = item.get("discount") if isinstance(item.get("discount"), dict) else {}
    if _upstream_enabled(discount.get("available")):
        rebate = _compact_number(discount.get("rebate"))
        tags.append({
            "key": "discount",
            "label": "折扣优惠",
            "tone": "promotion",
            "detail": f"当前享{rebate}折" if rebate else "当前商品参与折扣",
        })

    fullgift = item.get("fullgift") if isinstance(item.get("fullgift"), dict) else {}
    gift_rules = fullgift.get("rules") if isinstance(fullgift.get("rules"), list) else []
    if _upstream_enabled(fullgift.get("available")):
        details = []
        for rule in gift_rules:
            if not isinstance(rule, dict):
                continue
            condition = _compact_number(rule.get("condition"))
            value = _compact_number(rule.get("value"))
            if condition and value:
                details.append(f"购{condition}件赠{value}件")
        tags.append({
            "key": "full_gift",
            "label": "满件赠送",
            "tone": "promotion",
            "detail": "；".join(details) or "达到门槛可获赠品",
        })

    if str(item.get("goods_type") or "").strip().lower() == "card":
        tags.append({
            "key": "delivery",
            "label": "自动发货",
            "tone": "success",
            "detail": "卡密商品付款后由平台自动交付",
        })

    limit_value = _first_value(extend, ("limit_count", "limit"))
    try:
        minimum = max(1, int(limit_value or 1))
    except (TypeError, ValueError):
        minimum = 1
    tags.append({
        "key": "minimum",
        "label": f"{minimum}件起购",
        "tone": "info",
        "detail": f"单次购买数量不得少于{minimum}件",
    })

    if _upstream_enabled(item.get("coupon_status")):
        tags.append({
            "key": "coupon",
            "label": "支持优惠券",
            "tone": "offer",
            "detail": "结算时可输入有效优惠券",
        })

    if _upstream_enabled(extend.get("query_password_status")):
        tags.append({
            "key": "query_password",
            "label": "密码保护",
            "tone": "secure",
            "detail": "查询订单或领取卡密时需要安全密码",
        })
    return tags


def is_unlisted_error(value: Any) -> bool:
    message = str(value or "")
    return any(marker in message for marker in UNLISTED_ERROR_MARKERS)


def normalize_goods_payload(payload: dict[str, Any], goods_key: str) -> dict[str, Any]:
    if payload.get("code") != 1 or not isinstance(payload.get("data"), dict):
        message = str(payload.get("msg") or "商品接口未返回有效数据")
        raise RuntimeError(message[:160])

    item = payload["data"]
    extend = item.get("extend") if isinstance(item.get("extend"), dict) else {}
    category = item.get("category") if isinstance(item.get("category"), dict) else {}
    seller = item.get("user") if isinstance(item.get("user"), dict) else {}
    limit_count = extend.get("limit_count")
    sale_status = "on_sale" if item.get("status") == 1 else "off_sale"
    stock_value = _stock_value(item) if sale_status == "on_sale" else None
    specs = {
        "商品编号": goods_key,
        "商品类型": item.get("goods_type") or "未知",
        "商品分类": category.get("name") or "未分类",
        "最低起购": limit_count if limit_count not in (None, "", 0) else 1,
        "联系方式": item.get("contact_format") or "任意",
        "查询密码": "需要" if _upstream_enabled(extend.get("query_password_status")) else "不需要",
        "店铺": seller.get("nickname") or "链动小铺",
    }
    return {
        "goods_key": goods_key,
        "title": str(item.get("name") or f"链动小铺商品 {goods_key}"),
        "price": _money(item.get("real_price") if item.get("real_price") not in (None, "") else item.get("price")),
        "market_price": _money(item.get("market_price")),
        "stock": stock_value,
        "stock_label": (
            str(stock_value) if stock_value is not None else "接口未公开数量"
        ) if sale_status == "on_sale" else "未上架",
        "sale_status": sale_status,
        "description": plain_text(item.get("description")),
        "image": str(item.get("image") or ""),
        "specs": specs,
        "limit_count": int(limit_count) if str(limit_count or "").isdigit() else None,
        "contact_format": str(item.get("contact_format") or "any"),
        "query_password_required": _upstream_enabled(extend.get("query_password_status")),
        "commerce_tags": commerce_tags_from_goods(item),
        "source_url": str(item.get("link") or f"https://{ALLOWED_HOST}/item/{goods_key}"),
        "raw_data": item,
    }


def _post_shop_api(
    endpoint: str,
    payload: dict[str, Any],
    referer: str,
    *,
    visitor_id: str | None = None,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": f"https://{ALLOWED_HOST}",
        "Referer": referer,
    }
    if visitor_id:
        headers["Visitorid"] = visitor_id
    request = Request(
        f"https://{ALLOWED_HOST}{endpoint}",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with opener(request, timeout=15) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            content_type = str(getattr(response, "headers", {}).get("Content-Type", ""))
            status = int(getattr(response, "status", 200) or 200)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError("接口响应过大")
    except HTTPError as exc:
        try:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
        except Exception:
            raw = b""
        content_type = str(getattr(exc, "headers", {}).get("Content-Type", ""))
        if is_waf_response(raw, content_type=content_type, status=exc.code) or exc.code == 403:
            raise WafChallengeRequired("链动小铺触发阿里云 WAF 滑块验证，请使用浏览器验证后同步") from exc
        raise RuntimeError(f"链动小铺接口返回 HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError("无法连接链动小铺接口") from exc
    if is_waf_response(raw, content_type=content_type, status=status):
        raise WafChallengeRequired("链动小铺触发阿里云 WAF 滑块验证，请使用浏览器验证后同步")
    if "text/html" in content_type.lower():
        raise RuntimeError("链动小铺接口返回了 HTML 页面，不是商品 JSON")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("链动小铺接口返回了无法解析的数据") from exc
    if not isinstance(result, dict):
        raise RuntimeError("链动小铺接口响应格式无效")
    return result


def _random_visitor_id() -> str:
    """Match the storefront's short localStorage visitor id shape."""
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    value = uuid.uuid4().int
    chars: list[str] = []
    while value:
        value, remainder = divmod(value, 36)
        chars.append(alphabet[remainder])
    return "".join(reversed(chars))[-16:] or "visitor"


def fetch_buyer_juuid(
    shop_token: str,
    *,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    """Resolve the juuid posted by buyerBlackIframe for a shop token."""
    token = str(shop_token or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,80}", token):
        raise ValueError("店铺 Token 格式无效")
    referer = f"https://{ALLOWED_HOST}/shop/{token}"
    script_request = Request(
        f"https://{ALLOWED_HOST}/shopApi/Shop/buyerBlackJs?token={token}",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/javascript, */*;q=0.8",
            "Referer": referer,
        },
        method="GET",
    )
    try:
        with opener(script_request, timeout=15) as response:
            script = response.read(MAX_RESPONSE_BYTES + 1).decode("utf-8", "replace")
    except (HTTPError, URLError, UnicodeError) as exc:
        raise RuntimeError("无法获取店铺支付身份脚本") from exc
    iframe_match = re.search(r"iframe\.src\s*=\s*['\"]([^'\"]+)['\"]", script)
    iframe_url = iframe_match.group(1) if iframe_match else f"https://{ALLOWED_HOST}/shopApi/common/buyerBlackIframe"
    iframe_request = Request(
        iframe_url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html, */*;q=0.8", "Referer": referer},
        method="GET",
    )
    try:
        with opener(iframe_request, timeout=15) as response:
            iframe = response.read(MAX_RESPONSE_BYTES + 1).decode("utf-8", "replace")
    except (HTTPError, URLError, UnicodeError) as exc:
        raise RuntimeError("无法获取支付身份 iframe") from exc
    juuid_match = re.search(r"(?:const\s+juuid|juuid)\s*=\s*['\"]([A-Za-z0-9_-]{1,32})['\"]", iframe)
    if not juuid_match:
        raise RuntimeError("支付身份脚本未返回有效 juuid")
    return {
        "juuid": juuid_match.group(1),
        "source": "buyerBlackJs -> buyerBlackIframe -> postMessage(CloudBuyerBlack)",
        "iframe_url": iframe_url,
    }


def fetch_payment_channels(
    shop_token: str,
    *,
    post_api: Callable[..., dict[str, Any]] = _post_shop_api,
    visitor_id_factory: Callable[[], str] = _random_visitor_id,
) -> list[dict[str, Any]]:
    token = str(shop_token or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,80}", token):
        raise ValueError("店铺 Token 格式无效")
    result = post_api(
        "/shopApi/Shop/getUserChannel",
        {"token": token},
        f"https://{ALLOWED_HOST}/shop/{token}",
        visitor_id=visitor_id_factory(),
    )
    if result.get("code") != 1 or not isinstance(result.get("data"), list):
        raise RuntimeError(str(result.get("msg") or "无法获取可用支付渠道")[:200])
    channels = []
    for channel in result["data"]:
        if not isinstance(channel, dict) or channel.get("status") not in (None, 1, True, "1"):
            continue
        try:
            channel_id = int(channel.get("id"))
        except (TypeError, ValueError):
            continue
        if channel_id < 1:
            continue
        channels.append(
            {
                "id": channel_id,
                "name": str(channel.get("show_name") or channel.get("name") or channel.get("code") or channel_id),
                "code": str(channel.get("code") or ""),
            }
        )
    return channels


def fetch_shop_categories(
    shop_url: str,
    *,
    goods_type: str = "card",
    post_api: Callable[..., dict[str, Any]] = _post_shop_api,
    visitor_id: str | None = None,
) -> dict[str, Any]:
    token, canonical_url = parse_shop_url(shop_url)
    normalized_type = str(goods_type or "card").strip()[:30]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,30}", normalized_type):
        raise ValueError("商品类型格式无效")
    result = post_api(
        "/shopApi/Shop/categoryList",
        {"token": token, "goods_type": normalized_type, "category_key": ""},
        canonical_url,
        visitor_id=visitor_id,
    )
    if result.get("code") != 1:
        raise RuntimeError(str(result.get("msg") or "无法获取店铺分类")[:200])

    raw_data = result.get("data")
    if raw_data is None:
        raw_data = []
    elif isinstance(raw_data, dict):
        for key in ("list", "items", "rows", "records", "categories", "data"):
            if isinstance(raw_data.get(key), list):
                raw_data = raw_data[key]
                break
        else:
            raise RuntimeError("店铺分类接口返回格式无效")
    if not isinstance(raw_data, list):
        raise RuntimeError("店铺分类接口返回格式无效")

    categories: list[dict[str, Any]] = []
    seen: set[int] = set()
    for entry in raw_data:
        if not isinstance(entry, dict):
            continue
        try:
            category_id = int(entry.get("id") or entry.get("category_id"))
        except (TypeError, ValueError):
            continue
        if category_id < 1 or category_id in seen:
            continue
        seen.add(category_id)
        try:
            goods_count = max(0, int(entry.get("goods_count") or entry.get("count") or 0))
        except (TypeError, ValueError):
            goods_count = 0
        categories.append(
            {
                "id": category_id,
                "name": str(
                    entry.get("name")
                    or entry.get("category_name")
                    or entry.get("title")
                    or f"分类 {category_id}"
                ).strip()[:100],
                "goods_count": goods_count,
            }
        )
    return {
        "token": token,
        "url": canonical_url,
        "goods_type": normalized_type,
        "categories": categories,
    }


def create_official_payment_order(
    *,
    goods_key: str,
    quantity: int,
    coupon_code: str,
    channel_id: int,
    contact: str,
    query_password: str,
    select_cards_ids: list[Any] | None,
    juuid: str,
    referer: str,
    visitor_id: str | None = None,
    post_api: Callable[..., dict[str, Any]] = _post_shop_api,
    visitor_id_factory: Callable[[], str] = _random_visitor_id,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,80}", goods_key):
        raise ValueError("商品编号格式无效")
    if quantity < 1 or quantity > 99:
        raise ValueError("购买数量应为 1 到 99")
    if channel_id < 1 or channel_id > 99:
        raise ValueError("支付渠道编号无效")
    contact = str(contact or "").strip()
    if not contact:
        raise ValueError("请填写联系方式")
    query_password = str(query_password or "")
    juuid = str(juuid or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", juuid):
        raise ValueError("juuid 格式无效，请重新获取")
    if not re.fullmatch(rf"https://{re.escape(ALLOWED_HOST)}/item/[A-Za-z0-9_-]{{3,80}}", referer):
        referer = f"https://{ALLOWED_HOST}/item/{goods_key}"
    visitor_id = str(visitor_id or "").strip()
    if not VISITOR_ID_PATTERN.fullmatch(visitor_id):
        visitor_id = visitor_id_factory()
    payload = {
        "goods_key": goods_key,
        "quantity": quantity,
        "coupon_code": str(coupon_code or ""),
        "channel_id": channel_id,
        "contact": contact,
        "query_password": query_password,
        "select_cards_ids": select_cards_ids if isinstance(select_cards_ids, list) else [],
        "extend": {"juuid": juuid},
    }
    result = post_api(
        "/shopApi/Pay/order",
        payload,
        referer,
        visitor_id=visitor_id,
    )
    if result.get("code") != 1 or not isinstance(result.get("data"), dict):
        raise RuntimeError(str(result.get("msg") or "官方支付接口返回失败")[:200])
    data = result["data"]
    trade_no = str(data.get("trade_no") or "").strip()
    payurl = str(data.get("payurl") or data.get("pay_url") or "").strip()
    if not trade_no or not payurl or not payurl.startswith(f"https://{ALLOWED_HOST}/"):
        raise RuntimeError("官方支付接口未返回有效支付链接")
    return {
        "mode": "official",
        "status": "created",
        "trade_no": trade_no,
        "amount": _money(data.get("total_amount")),
        "channel_id": channel_id,
        "channel": "alipay" if channel_id == 1 else "wechat",
        "payment_url": payurl,
        "juuid": juuid,
        "notice": "官方订单已创建。请在弹出的支付页面核对金额并完成支付。",
    }


def fetch_goods(
    url: str,
    *,
    post_api: Callable[..., dict[str, Any]] = _post_shop_api,
) -> dict[str, Any]:
    goods_key, canonical_url = parse_item_url(url)
    payload = post_api(
        "/shopApi/Shop/goodsInfo",
        {"goods_key": goods_key, "trade_no": ""},
        canonical_url,
    )
    return normalize_goods_payload(payload, goods_key)


def _goods_list_rows(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if payload.get("code") != 1:
        raise RuntimeError(str(payload.get("msg") or "店铺商品接口返回失败")[:160])
    data = payload.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)], {}
    if not isinstance(data, dict):
        raise RuntimeError("店铺商品接口未返回列表数据")
    for key in ("list", "items", "rows", "records", "data", "goods"):
        rows = data.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)], data
    return [], data


def normalize_goods_list_item(item: dict[str, Any], shop_token: str) -> dict[str, Any]:
    goods_key = str(
        _first_value(item, ("goods_key", "key", "goodsKey", "item_key")) or ""
    ).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,80}", goods_key):
        link = str(item.get("link") or item.get("url") or "")
        try:
            goods_key, _ = parse_item_url(link)
        except ValueError as exc:
            raise ValueError("店铺列表商品缺少有效 goods_key") from exc
    extend = item.get("extend") if isinstance(item.get("extend"), dict) else {}
    category = item.get("category") if isinstance(item.get("category"), dict) else {}
    seller = item.get("user") if isinstance(item.get("user"), dict) else {}
    limit_count = _first_value(extend, ("limit_count", "limit"))
    sales = _first_value(item, ("sales", "sales_count", "sold", "sale_count"))
    status_value = _first_value(item, ("status", "goods_status", "is_sale"))
    sale_status = "on_sale" if status_value is None or str(status_value).lower() in {"1", "true", "on_sale"} else "off_sale"
    stock_value = _stock_value(item) if sale_status == "on_sale" else None
    specs = {
        "商品编号": goods_key,
        "商品类型": item.get("goods_type") or "未知",
        "商品分类": category.get("name") or item.get("category_name") or "未分类",
        "最低起购": limit_count if limit_count not in (None, "", 0) else 1,
        "查询密码": "需要" if _upstream_enabled(extend.get("query_password_status")) else "未知",
        "店铺": seller.get("nickname") or shop_token,
        "店铺Token": shop_token,
    }
    if sales not in (None, ""):
        specs["累计销量"] = sales
    return {
        "goods_key": goods_key,
        "title": str(item.get("name") or item.get("title") or f"链动小铺商品 {goods_key}"),
        "price": _money(item.get("real_price") if item.get("real_price") not in (None, "") else item.get("price")),
        "market_price": _money(item.get("market_price")),
        "stock": stock_value,
        "stock_label": (
            str(stock_value) if stock_value not in (None, "") else "接口未公开数量"
        ) if sale_status == "on_sale" else "未上架",
        "sale_status": sale_status,
        "description": plain_text(item.get("description")),
        "image": str(item.get("image") or item.get("cover") or ""),
        "specs": specs,
        "limit_count": int(limit_count) if str(limit_count or "").isdigit() else None,
        "contact_format": str(item.get("contact_format") or "any"),
        "query_password_required": _upstream_enabled(extend.get("query_password_status")),
        "commerce_tags": commerce_tags_from_goods(item),
        "source_url": f"https://{ALLOWED_HOST}/item/{goods_key}",
        "raw_data": item,
    }


def fetch_shop_catalog(
    shop_url: str,
    *,
    keywords: str = "",
    category_id: int | None = None,
    goods_type: str = "card",
    page_size: int = 50,
    max_pages: int = 50,
    post_api: Callable[..., dict[str, Any]] = _post_shop_api,
) -> list[dict[str, Any]]:
    token, canonical_url = parse_shop_url(shop_url)
    products: dict[str, dict[str, Any]] = {}
    for current in range(1, max_pages + 1):
        request_data = {
            "token": token,
            "keywords": keywords,
            "category_id": category_id or "",
            "goods_type": goods_type,
            "current": current,
            "pageSize": page_size,
        }
        payload = post_api("/shopApi/Shop/goodsList", request_data, canonical_url)
        rows, pagination = _goods_list_rows(payload)
        for row in rows:
            product = normalize_goods_list_item(row, token)
            products[product["goods_key"]] = product

        total_value = _first_value(pagination, ("total", "count", "total_count"))
        last_page = _first_value(pagination, ("last_page", "lastPage", "pages", "page_count"))
        try:
            total = int(total_value) if total_value not in (None, "") else None
        except (TypeError, ValueError):
            total = None
        try:
            page_count = int(last_page) if last_page not in (None, "") else None
        except (TypeError, ValueError):
            page_count = None

        if not rows:
            break
        if total is not None and current * page_size >= total:
            break
        if page_count is not None and current >= page_count:
            break
        if len(rows) < page_size:
            break
    return list(products.values())
