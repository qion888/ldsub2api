#!/usr/bin/env python3
"""
Redeem 401 找回 API SDK
=======================

版本：2026.08.13.2
发布时间：2026-08-13

卡密驱动接口：所有找回操作只需 card_code，无需提供订单号或凭据编号。
下载令牌由 batch-cards 响应直接返回（done 任务的 download_token），
不再需要任何按任务编号查询的低层接口。

公开 API，仅需卡密（card_code）即可调用，无需 API Key 或 Token。

⚠️  并发限制：全局并发上限 100，建议客户端控制在 30 并发以内。
    超过建议值不会报错，但会导致排队等待，找回耗时成倍增长。

    from redeem_api_sdk import RedeemClient

    client = RedeemClient("https://xx1xx.team")

    # 1. 检测哪些账号是 401 失效
    result = client.health_check(["RCL-xxxx", "RCL-yyyy"])
    print(f"需找回: {result.need_reclaim}, 正常: {result.healthy}")

    # 2. 只找回 401 的
    reclaim = client.batch_reclaim(["RCL-xxxx", "RCL-yyyy"], mode="401")

    # 3. 轮询直到全部完成，下载令牌直接挂在结果上
    done = client.poll_until_done(["RCL-xxxx", "RCL-yyyy"])
    print(f"已完成: {done.done}, 已更新: {done.updated}, 本来正常: {done.no_action}")
    for t in done.downloadables:
        data = client.download(t.order_no, t.download_token)
        open(f"{t.order_no}.json", "wb").write(data)
"""

from __future__ import annotations

SDK_VERSION = "2026.08.13.2"
SDK_RELEASED_AT = "2026-08-13T00:00:00Z"

import time
from dataclasses import dataclass, field
from typing import Optional
import re
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class _Response:
    def __init__(self, status: int, body: bytes):
        self.status_code = status
        self.content = body

    def json(self):
        return json.loads(self.content.decode("utf-8"))

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _RequestsCompat:
    RequestException = RuntimeError
    @staticmethod
    def _request(method, url, *, json_body=None, params=None, timeout=30):
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        body = json.dumps(json_body, ensure_ascii=False).encode("utf-8") if json_body is not None else None
        request = Request(url, data=body, headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "LDXP-RedeemSDK/1.0"}, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                return _Response(int(getattr(response, "status", 200)), response.read(8 * 1024 * 1024 + 1))
        except HTTPError as exc:
            return _Response(int(exc.code), exc.read(8 * 1024 * 1024 + 1))
        except URLError as exc:
            raise RuntimeError(str(exc.reason)) from exc

    @classmethod
    def post(cls, url, *, json=None, timeout=30):
        return cls._request("POST", url, json_body=json, timeout=timeout)

    @classmethod
    def get(cls, url, *, params=None, timeout=30):
        return cls._request("GET", url, params=params, timeout=timeout)


requests = _RequestsCompat()

# -------------------------------------------------------
# 可选工具：从 sub2api JSON 的 name 字段提取卡密（OAuth 兑换库路径）
# -------------------------------------------------------
#
# 本项目在生成 sub2api 交付包时，通过 patchSub2APINameWithCardCode
# 将卡密追加到 account.name 末尾（空格分隔）：
#
#   "name": "716-晚9点半-30D team 10032974 RCL-xxxx-xxxx"
#            ←─────────── 原始 name ────────────→ ← 卡密 →
#
# 使用示例：
#   from redeem_api_sdk import extract_card_codes_from_json
#
#   mapping = extract_card_codes_from_json("sub2api_export.json")
#   # {"RCL-xxxx-xxxx": [0, 1, 2]}  ← {card_code: 该卡密对应的账号索引列表}

def extract_card_code_from_name(name: str) -> Optional[str]:
    """从单个 account.name 提取卡密。

    patchSub2APINameWithCardCode 在 name 末尾追加 " " + card_code，
    且只在 name 中尚不包含该 card_code 时才追加。因此卡密始终是
    name 中最后一个空格分隔的 token。

    Returns:
        卡密字符串，若 name 为空或不含空格则返回 None。
    """
    if not name:
        return None
    # 取最后一个空格后的 token
    parts = name.rsplit(" ", 1)
    if len(parts) < 2:
        return None
    candidate = parts[-1].strip()
    if not _looks_like_card_code(candidate):
        return None
    return candidate


def _looks_like_card_code(s: str) -> bool:
    """判断字符串是否像卡密（字母/数字/连字符，不含 URL 特征）。"""
    if len(s) < 4 or len(s) > 128:
        return False
    if "://" in s:
        return False
    return bool(re.match(r'^[A-Za-z0-9][A-Za-z0-9\-]*[A-Za-z0-9]$', s))


def extract_card_codes_from_json(filepath_or_text: str) -> dict[str, list[int]]:
    """从 sub2api JSON 中提取所有卡密 → 账号索引映射。

    支持传入文件路径或 JSON 字符串。

    返回: ``{card_code: [index, ...]}``

        空字典表示未找到任何卡密。

    适用场景：
        客户有大量 sub2api JSON 文件，需要知道每个文件里哪些账号
        属于同一张卡——据此对同一张卡的账号一起执行健康检查或批量找回。

    ```python
    mapping = extract_card_codes_from_json("sub2api_export.json")
    for card_code, indices in mapping.items():
        accounts = data["accounts"]  # 预先加载的 JSON
        emails = [accounts[i]["credentials"]["email"] for i in indices]
        print(f"{card_code}: {len(indices)} 个账号")
    ```
    """
    import json

    # 尝试作为文件路径
    try:
        with open(filepath_or_text, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        # 作为 JSON 字符串
        try:
            data = json.loads(filepath_or_text)
        except json.JSONDecodeError:
            return {}

    accounts = data.get("accounts", [])
    if not isinstance(accounts, list):
        return {}

    mapping: dict[str, dict[int, None]] = {}
    for i, account in enumerate(accounts):
        name = account.get("name", "")
        code = extract_card_code_from_name(name)
        if code:
            mapping.setdefault(code, {})[i] = None

    return {k: sorted(v.keys()) for k, v in mapping.items()}


# -------------------------------------------------------
# 响应模型
# -------------------------------------------------------

@dataclass
class HealthCheckResult:
    ok: bool
    need_reclaim: int = 0
    healthy: int = 0
    cannot_reclaim: int = 0
    unknown: int = 0
    total: int = 0
    not_loadable: int = 0
    credentials: list[dict] = field(default_factory=list)
    error: str = ""


@dataclass
class ReclaimTask:
    card_code: str
    order_no: str
    resource_uid: str
    status: str = ""
    message: str = ""
    tier_label: str = ""
    no_action: bool = False
    permanent: bool = False
    error_code: str = ""
    provider_status: int = 0
    failure_class: str = ""
    download_token: str = ""
    download_error: str = ""


@dataclass
class BatchReclaimResult:
    ok: bool
    total: int = 0
    requested_cards: int = 0
    valid_cards: int = 0
    queued: int = 0
    already_running: int = 0
    done: int = 0
    unreclaimable: int = 0
    not_owned: int = 0
    skipped: int = 0
    failed: int = 0
    tracked_tasks: int = 0
    scanned_resources: int = 0
    distinct_resources: int = 0
    skipped_not_401: int = 0
    cards: list[dict] = field(default_factory=list)
    all_tasks: list[ReclaimTask] = field(default_factory=list)
    error: str = ""


@dataclass
class PollResult:
    """poll_until_done() 返回的聚合结果。"""

    done: int = 0                     # 总完成数
    updated: int = 0                  # 凭据被真实更新的（no_action=false）
    no_action: int = 0                # 本来正常、未动的
    unreclaimable: int = 0            # 彻底无法找回
    failed: int = 0                   # exhausted / 内部错误等失败终态
    not_owned: int = 0                # 不属于该卡的条目
    skipped: int = 0                  # 服务端跳过的条目
    still_running: int = 0            # 还在处理中的
    elapsed_seconds: float = 0.0
    raw: Optional[dict] = None
    tasks: list[ReclaimTask] = field(default_factory=list)  # 最终快照的全部任务（含 download_token）
    downloadables: list[ReclaimTask] = field(default_factory=list)  # 已拿到下载令牌的 done 任务


# -------------------------------------------------------
# 客户端
# -------------------------------------------------------

class RedeemClient:
    """401 找回 API 客户端。

    Args:
        base_url: 兑换服务地址，例如 ``https://xx1xx.team``。
        timeout: 请求超时秒数，默认 30。
    """

    def __init__(self, base_url: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # --- 公开 API ---

    def health_check(self, card_codes: list[str]) -> HealthCheckResult:
        """检测卡密下账号的当前状态（只读，不修改数据）。

        ```bash
        curl -X POST {base}/api/redeem/reclaim/health-check \\
          -H 'Content-Type: application/json' \\
          -d '{"card_codes":["RCL-xxxx"]}'
        ```
        """
        try:
            r = requests.post(
                f"{self.base_url}/api/redeem/reclaim/health-check",
                json={"card_codes": card_codes},
                timeout=max(self.timeout, 90),
            )
            data = r.json()
            if not data.get("ok"):
                return HealthCheckResult(ok=False, error=data.get("error", "未知错误"))
            return HealthCheckResult(
                ok=True,
                need_reclaim=data.get("need_reclaim", 0),
                healthy=data.get("healthy", 0),
                cannot_reclaim=data.get("cannot_reclaim", 0),
                unknown=data.get("unknown", 0),
                total=data.get("total", 0),
                not_loadable=data.get("not_loadable", 0),
                credentials=data.get("credentials", []),
            )
        except requests.RequestException as e:
            return HealthCheckResult(ok=False, error=str(e))

    def batch_reclaim(
        self, card_codes: list[str], mode: str = "401"
    ) -> BatchReclaimResult:
        """批量找回多张卡密的所有凭据。

        ⚠️  全局并发上限 100，建议每次提交不超过 20 张卡密，串行分批执行。

        Args:
            card_codes: 卡密数组。单批建议 ≤20 张。
            mode: ``"401"`` 只找回 401 / ``"all"`` 找回全部。
        """
        return _do_batch_cards(self, card_codes, mode=mode, query_only=False)

    def refresh_progress(self, card_codes: list[str]) -> BatchReclaimResult:
        """只读刷新批量找回进度（不入队新任务）。"""
        return _do_batch_cards(self, card_codes, mode="all", query_only=True)

    def poll_until_done(
        self,
        card_codes: list[str],
        interval: float = 12.0,
        max_wait: float = 600.0,
    ) -> PollResult:
        """轮询批量找回进度直到全部完成或超时。

        Args:
            card_codes: 卡密数组（与发起找回时相同）。
            interval: 轮询间隔秒数。
            max_wait: 最大等待秒数。
        """
        started = time.monotonic()
        last_res: Optional[BatchReclaimResult] = None
        while True:
            elapsed = time.monotonic() - started
            if elapsed > max_wait:
                return PollResult(
                    still_running=1, elapsed_seconds=elapsed,
                    tasks=list(last_res.all_tasks) if last_res else [],
                    downloadables=_downloadable_tasks(last_res) if last_res else [],
                )

            res = self.refresh_progress(card_codes)
            if not res.ok:
                time.sleep(interval)
                continue
            last_res = res

            # 没有待处理/进行中的任务 = 全部完成
            if res.queued == 0 and res.already_running == 0:
                updated = sum(1 for t in res.all_tasks if t.status == "done" and not t.no_action)
                no_action = sum(1 for t in res.all_tasks if t.status == "done" and t.no_action)
                return PollResult(
                    done=res.done,
                    updated=updated,
                    no_action=no_action,
                    unreclaimable=res.unreclaimable,
                    failed=res.failed,
                    not_owned=res.not_owned,
                    skipped=res.skipped,
                    still_running=0,
                    elapsed_seconds=elapsed,
                    raw=None,
                    tasks=list(res.all_tasks),
                    downloadables=_downloadable_tasks(res),
                )

            time.sleep(interval)

    def download(self, order_no: str, token: str) -> Optional[bytes]:
        """下载找回后的凭据 JSON 文件。

        找回完成后，从 ``PollResult.downloadables``（或 batch-cards 任务的
        ``download_token``）取令牌调用此方法。

        ```bash
        curl {base}/api/redeem/orders/{order_no}/download?token={token} -o result.json
        ```

        Returns:
            文件内容 bytes，失败返回 None。
        """
        try:
            r = requests.get(
                f"{self.base_url}/api/redeem/orders/{order_no}/download",
                params={"token": token},
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.content
        except requests.RequestException:
            return None

    def batch_download(
        self, items: list[dict[str, str]], save_dir: str = "."
    ) -> list[str]:
        """批量下载多个订单的凭据 JSON，保存为 ``{order_no}.json``。

        ```bash
        curl -X POST {base}/api/redeem/batch-download \\
          -H 'Content-Type: application/json' \\
          -d '{{"items":[{{"order_no":"...","download_token":"..."}}]}}'
        ```

        Returns:
            保存的文件路径列表。
        """
        try:
            r = requests.post(
                f"{self.base_url}/api/redeem/batch-download",
                json={"items": items},
                timeout=self.timeout,
            )
            r.raise_for_status()
            import os
            path = os.path.join(save_dir, f"batch_{int(time.time())}.json")
            with open(path, "wb") as f:
                f.write(r.content)
            return [path]
        except requests.RequestException:
            return []


# -------------------------------------------------------
# 内部实现
# -------------------------------------------------------

def _do_batch_cards(
    client: RedeemClient, card_codes: list[str], mode: str, query_only: bool
) -> BatchReclaimResult:
    try:
        r = requests.post(
            f"{client.base_url}/api/redeem/reclaim/batch-cards",
            json={"card_codes": card_codes, "mode": mode, "query_only": query_only},
            timeout=client.timeout,
        )
        data = r.json()
        if not data.get("ok"):
            return BatchReclaimResult(ok=False, error=data.get("error", ""))
        tasks = _parse_tasks_from_cards(data)
        return BatchReclaimResult(
            ok=True,
            total=data.get("total", 0),
            requested_cards=data.get("requested_cards", 0),
            valid_cards=data.get("valid_cards", 0),
            queued=data.get("queued", 0),
            already_running=data.get("already_running", 0),
            done=data.get("done", 0),
            unreclaimable=data.get("unreclaimable", 0),
            not_owned=data.get("not_owned", 0),
            skipped=data.get("skipped", 0),
            failed=data.get("failed", 0),
            tracked_tasks=data.get("tracked_tasks", 0),
            scanned_resources=data.get("scanned_resources", 0),
            distinct_resources=data.get("distinct_resources", 0),
            skipped_not_401=data.get("skipped_not_401", 0),
            cards=data.get("cards", []),
            all_tasks=tasks,
        )
    except requests.RequestException as e:
        return BatchReclaimResult(ok=False, error=str(e))


def _parse_task(t: dict, default_card_code: str = "") -> ReclaimTask:
    return ReclaimTask(
        card_code=t.get("card_code", default_card_code),
        order_no=t.get("order_no", ""),
        resource_uid=t.get("resource_uid", ""),
        status=t.get("status", ""),
        message=t.get("message", ""),
        tier_label=t.get("tier_label", ""),
        no_action=bool(t.get("no_action", False)),
        permanent=bool(t.get("permanent", False)),
        error_code=t.get("error_code", "") or "",
        provider_status=int(t.get("provider_status", 0) or 0),
        failure_class=t.get("failure_class", "") or "",
        download_token=t.get("download_token", "") or "",
        download_error=t.get("download_error", "") or "",
    )


def _parse_tasks(raw_tasks: list[dict], default_card_code: str = "") -> list[ReclaimTask]:
    return [_parse_task(t, default_card_code=default_card_code) for t in raw_tasks if isinstance(t, dict)]


def _parse_tasks_from_cards(data: dict) -> list[ReclaimTask]:
    tasks: list[ReclaimTask] = []
    for card in data.get("cards", []) or []:
        if not isinstance(card, dict):
            continue
        tasks.extend(_parse_tasks(card.get("tasks", []) or [], default_card_code=card.get("card_code", "")))
    return tasks


def _downloadable_tasks(res: BatchReclaimResult) -> list[ReclaimTask]:
    """取出已完成且带下载令牌的任务。"""
    return [t for t in res.all_tasks if t.status == "done" and t.download_token and t.order_no]


# -------------------------------------------------------
# 命令行 Demo
# -------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python3 redeem_api_sdk.py <兑换服务地址> [卡密...]")
        print("示例: python3 redeem_api_sdk.py https://xx1xx.team RCL-xxxx RCL-yyyy")
        sys.exit(1)

    base = sys.argv[1]
    codes = sys.argv[2:] if len(sys.argv) > 2 else []

    client = RedeemClient(base)

    if not codes:
        print("未提供卡密，仅展示客户端用法。")
        print()
        print("  client = RedeemClient(base_url)")
        print("  result = client.health_check(card_codes)")
        print("  reclaim = client.batch_reclaim(card_codes, mode='401')")
        print("  done = client.poll_until_done(card_codes)")
        sys.exit(0)

    print(f"服务: {base}")
    print(f"卡密数: {len(codes)}")
    print()

    # Step 1: 检测
    print("[1/3] 检测账号状态...")
    hc = client.health_check(codes)
    if not hc.ok:
        print(f"  检测失败: {hc.error}")
        sys.exit(1)
    print(f"  需找回 401: {hc.need_reclaim} | 正常: {hc.healthy} | 不可找回: {hc.cannot_reclaim} | 未知: {hc.unknown}")
    print()

    # Step 2: 找回
    if hc.need_reclaim > 0:
        print(f"[2/3] 正在找回 {hc.need_reclaim} 个 401 凭据 (mode=401)...")
        reclaim = client.batch_reclaim(codes, mode="401")
        if not reclaim.ok:
            print(f"  提交失败: {reclaim.error}")
            sys.exit(1)
        print(f"  已入队: {reclaim.queued}, 处理中: {reclaim.already_running}, 完成: {reclaim.done}")
        print()
    else:
        print("[2/3] 无需找回，跳过。")
        print()
        sys.exit(0)

    # Step 3: 轮询
    print("[3/3] 等待完成（最长 10 分钟）...")
    done = client.poll_until_done(codes)
    if done.still_running:
        print(f"  超时，仍有 {done.still_running} 条处理中，可稍后重试。")
    else:
        print(f"  全部完成！")
        print(f"  已更新凭据: {done.updated} | 本来正常: {done.no_action} | 不可找回: {done.unreclaimable}")
        print(f"  耗时: {done.elapsed_seconds:.0f} 秒")
        if done.downloadables:
            print(f"  可下载 {len(done.downloadables)} 个已恢复订单：")
            for t in done.downloadables:
                print(f"    {t.order_no} ({t.card_code})")
                data = client.download(t.order_no, t.download_token)
                print(f"      下载 {'成功 ' + str(len(data)) + ' 字节' if data else '失败'}")
        for t in done.tasks:
            if t.status == "done" and not t.download_token:
                print(f"  ⚠ {t.order_no} 已完成但无下载令牌：{t.download_error or '未知原因'}")
