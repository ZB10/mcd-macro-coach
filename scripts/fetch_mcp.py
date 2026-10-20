"""通过 MCP Streamable HTTP 抓取菜单与套餐详情。

为什么需要这个脚本：营养表只有单品数据，而菜单上大半是套餐。
一份套餐的营养必须靠 ``query-meal-detail`` 的 rounds 拆解才能算出来，
但一个门店有几十个套餐，逐个手动查不现实，Skill 每次运行都现场查
又太慢。所以这里一次性抓全、落盘缓存，后续求解全部读本地文件。

Token 只从环境变量读，不落盘、不进仓库——这是参赛声明的硬要求。

用法：

    export MCD_MCP_TOKEN="your-token"
    python scripts/fetch_mcp.py --store 3500027 --out examples/store_3500027.json

输出结构：

    {"storeCode": "...", "fetchedAt": "...", "menu": {...}, "details": {"code": {...}}}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

MCP_URL = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2024-11-05"

# 套餐识别关键词。命中的商品才会去查 detail，避免无谓请求。
COMBO_HINTS = ("套餐", "组合", "件套", "二件", "三件")


class McpError(RuntimeError):
    pass


class McpClient:
    """极简 MCP Streamable HTTP 客户端。

    只实现本项目用到的三件事：initialize、tools/call、解析返回。
    没有 session 管理——实测 mcd-mcp 不返回 Mcp-Session-Id，可直接无状态调用。
    """

    def __init__(self, token: str, url: str = MCP_URL, timeout: float = 30.0) -> None:
        if not token:
            raise McpError("缺少 MCP Token")
        self.url = url
        self.token = token
        self.timeout = timeout
        self._next_id = 0

    def _post(self, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise McpError(f"HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise McpError(f"网络错误: {exc.reason}") from exc

        # 服务端可能返回纯 JSON，也可能返回 SSE（data: {...}）。
        text = raw.strip()
        if text.startswith("event:") or text.startswith("data:"):
            chunks = [
                line[5:].strip()
                for line in text.splitlines()
                if line.startswith("data:")
            ]
            text = chunks[-1] if chunks else ""

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise McpError(f"响应不是合法 JSON: {text[:200]}") from exc

    def initialize(self) -> dict:
        self._next_id += 1
        resp = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "mcd-macro-coach", "version": "1.0"},
                },
            }
        )
        if "error" in resp:
            raise McpError(f"initialize 失败: {resp['error']}")
        return resp.get("result", {})

    def call_tool(self, name: str, arguments: dict) -> dict:
        """调用工具并返回解析后的业务 JSON。

        mcd-mcp 把业务数据塞在 content[0].text 里，前面还拼了一段给大模型看的
        输出格式说明，真正的 JSON 是其中单独一行。所以这里逐行尝试解析，
        取最后一个能解析成 dict 的——那才是 payload。
        """
        self._next_id += 1
        resp = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        if "error" in resp:
            raise McpError(f"{name} 调用失败: {resp['error']}")

        result = resp.get("result") or {}
        if result.get("isError"):
            raise McpError(f"{name} 返回错误: {result}")

        for block in result.get("content") or []:
            text = block.get("text") or ""
            parsed = _extract_json(text)
            if parsed is not None:
                return parsed
        raise McpError(f"{name} 响应中找不到 JSON payload")


def _extract_json(text: str) -> dict | None:
    """从混排文本里抠出业务 JSON。

    注意：mcd-mcp 的 content 里，业务 JSON 后面**直接接着**下一段说明文字，
    中间没有换行，例如 ``...}}## 数据传参规则``。因此不能用 ``json.loads``
    整行解析（会报 Extra data），必须用 ``raw_decode`` 从行首解析、
    允许尾部有多余字符。
    """
    decoder = json.JSONDecoder()

    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            obj, _ = decoder.raw_decode(stripped)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    candidate: dict | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj, _ = decoder.raw_decode(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and ("success" in obj or "code" in obj or "data" in obj):
            candidate = obj
    return candidate


def _looks_like_combo(name: str) -> bool:
    return any(hint in name for hint in COMBO_HINTS)


def fetch_store(
    client: McpClient,
    store_code: str,
    order_type: int = 1,
    be_type: int = 1,
    workers: int = 6,
    fetch_all_details: bool = False,
) -> dict:
    """抓取门店菜单，并为疑似套餐的商品抓取详情。"""
    menu = client.call_tool(
        "query-meals",
        {"storeCode": store_code, "orderType": order_type, "beType": be_type},
    )
    meals = (menu.get("data") or {}).get("meals") or {}

    if fetch_all_details:
        targets = list(meals.keys())
    else:
        targets = [
            code
            for code, entry in meals.items()
            if _looks_like_combo((entry.get("name") or ""))
        ]

    print(f"菜单共 {len(meals)} 件，待查详情 {len(targets)} 件…", flush=True)

    details: dict[str, dict] = {}
    done = 0

    def work(code: str) -> tuple[str, dict | None]:
        try:
            detail = client.call_tool(
                "query-meal-detail",
                {
                    "storeCode": store_code,
                    "orderType": order_type,
                    "beType": be_type,
                    "code": code,
                },
            )
            return code, detail.get("data") or detail
        except McpError as exc:
            print(f"  ! {code} 抓取失败: {exc}", file=sys.stderr)
            return code, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for code, detail in pool.map(work, targets):
            done += 1
            if detail:
                details[code] = detail
            if done % 10 == 0:
                print(f"  进度 {done}/{len(targets)}", flush=True)
            time.sleep(0.05)  # 轻微限速，避免触发风控

    return {
        "storeCode": store_code,
        "orderType": order_type,
        "beType": be_type,
        "fetchedAt": datetime.now().isoformat(timespec="seconds"),
        "menu": menu,
        "details": details,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取麦当劳门店菜单与套餐详情")
    parser.add_argument("--store", required=True, help="门店编码 storeCode")
    parser.add_argument("--order-type", type=int, default=1, choices=[1, 2])
    parser.add_argument("--be-type", type=int, default=1, choices=[1, 2, 5, 6])
    parser.add_argument("--out", required=True, help="输出 JSON 路径")
    parser.add_argument("--workers", type=int, default=6, help="并发数")
    parser.add_argument("--all-details", action="store_true", help="对所有商品都查详情")
    parser.add_argument("--token", default=None, help="MCP Token，默认读环境变量 MCD_MCP_TOKEN")
    args = parser.parse_args(argv)

    token = args.token or os.environ.get("MCD_MCP_TOKEN", "")
    if not token:
        print("错误：未提供 Token。请设置环境变量 MCD_MCP_TOKEN。", file=sys.stderr)
        return 2

    client = McpClient(token)
    try:
        client.initialize()
        bundle = fetch_store(
            client,
            store_code=args.store,
            order_type=args.order_type,
            be_type=args.be_type,
            workers=args.workers,
            fetch_all_details=args.all_details,
        )
    except McpError as exc:
        print(f"抓取失败: {exc}", file=sys.stderr)
        return 1

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, ensure_ascii=False, indent=2)

    print(f"已写入 {args.out}")
    print(f"  餐品 {len((bundle['menu'].get('data') or {}).get('meals') or {})} 件")
    print(f"  套餐详情 {len(bundle['details'])} 份")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
