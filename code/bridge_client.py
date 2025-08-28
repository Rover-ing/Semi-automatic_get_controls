"""
桥接客户端：将 XPath 或 bounds 提交给本地桥接服务 /bridge/capture_tap。

用法（PowerShell）：
  # 方式一：提供 bounds
  python .\adbgetevent\bridge_client.py --bounds "[100,200][300,260]"

  # 方式二：提供 XPath（需要安装 uiautomator2，用它解析当前页面的该节点并取 bounds）
  python .\adbgetevent\bridge_client.py --xpath "//android.widget.Button[@text='确定']"

可选参数：
  --host 127.0.0.1  --port 8001  --no-tap
"""

from __future__ import annotations

import argparse
import json
from typing import Dict, Any

import requests


def bounds_to_str(bounds: Dict[str, int]) -> str:
    x1 = int(bounds.get("left", 0))
    y1 = int(bounds.get("top", 0))
    x2 = int(bounds.get("right", 0))
    y2 = int(bounds.get("bottom", 0))
    return f"[{x1},{y1}][{x2},{y2}]"


def main():
    ap = argparse.ArgumentParser(description="Bridge client to post bounds/xpath to capture_tap")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--bounds", help="bounds string like [x1,y1][x2,y2]")
    ap.add_argument("--xpath", help="XPath expression, will use uiautomator2 to resolve bounds")
    ap.add_argument("--no-tap", action="store_true", help="only capture, do not perform tap")
    args = ap.parse_args()

    payload: Dict[str, Any] = {}

    if args.bounds:
        payload["bounds"] = args.bounds
    elif args.xpath:
        try:
            import uiautomator2 as u2  # type: ignore
            d = u2.connect()
            el = d.xpath(args.xpath).get()
            info = el.info
            b = info.get("bounds") or {}
            payload["bounds"] = bounds_to_str(b)
            # 同时尽量附带部分节点属性
            payload["node"] = {
                "text": info.get("text"),
                "class": info.get("className") or info.get("class"),
                "resource-id": info.get("resourceName") or info.get("resource-id"),
                "content-desc": info.get("contentDescription") or info.get("content-desc"),
                "package": info.get("packageName") or info.get("package"),
                "bounds": payload["bounds"],
            }
        except Exception as e:
            print(f"[ERR] 解析 XPath 失败：{e}")
            return
    else:
        print("请使用 --bounds 或 --xpath 其中之一。")
        return

    if args.no_tap:
        payload["tap"] = False

    url = f"http://{args.host}:{args.port}/bridge/capture_tap"
    try:
        resp = requests.post(url, json=payload, timeout=20)
        print(resp.status_code, resp.text)
    except Exception as e:
        print(f"[ERR] 请求失败：{e}")


if __name__ == "__main__":
    main()
