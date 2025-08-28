"""
UIAutodev 桥接服务：接收前端（UIAutodev/weditor 或自定义脚本）传来的控件信息，
在“点击前”完成快照采集（XML + 截图），在截图上画框、写入 JSON，然后按控件 bounds 的中心坐标触发一次点击。

特点：
- 复用 adb 管道：uiautomator dump / screencap / input tap
- 接口接受 bounds 或 xpath（推荐 bounds，最稳）
- 顺序命名：elem_{N}_raw.png / elem_{N}_boxed.png / element_xml/elem_{N}.xml
- JSON 文件覆盖初始化可通过 --reset-json 控制

启动示例（PowerShell）：
  python .\adbgetevent\bridge_server.py --host 127.0.0.1 --port 8001 --reset-json

前端调用示例（PowerShell）：
  $body = @{ bounds = "[100,200][300,260]" } | ConvertTo-Json
  Invoke-RestMethod -Uri http://127.0.0.1:8001/bridge/capture_tap -Method Post -ContentType 'application/json' -Body $body

返回：包含 elem_id、center 坐标、文件路径、activity 等。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
from typing import Any, Dict, Optional, Tuple
import logging
import traceback
import threading

from flask import Flask, jsonify, request
import re

# 复用现有采集与写盘逻辑（兼容直接脚本运行与作为包导入）
try:
    import adbgetevent.run_collector_adb as core  # 从工作区根目录运行
except ModuleNotFoundError:
    import os, sys
    sys.path.append(os.path.dirname(__file__))  # 退回到同目录导入
    import run_collector_adb as core  # type: ignore


app = Flask(__name__)
_BACKEND = "adb"  # or 'u2'
_U2 = None  # type: ignore


# -----------------------------
# Logging
# -----------------------------
LOGGER = logging.getLogger("bridge")
if not LOGGER.handlers:
    debug_env = str(os.environ.get("BRIDGE_DEBUG", "")).lower() in ("1", "true", "yes")
    LOGGER.setLevel(logging.DEBUG if debug_env else logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s")
    try:
        from logging.handlers import RotatingFileHandler
        log_dir = (Path(__file__).resolve().parent / "UI_Automated_acquisition").resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_dir / "bridge_server.log", maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8")
        fh.setFormatter(fmt)
        LOGGER.addHandler(fh)
    except Exception:
        pass
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    LOGGER.addHandler(sh)


def ensure_ready(reset_json: bool = False):
    core.ensure_directories()
    if reset_json:
        try:
            with open(core.JSON_PATH, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False)
        except Exception as e:
            LOGGER.warning("重置 JSON 失败: %s", e)
    else:
        # 确保 JSON 文件存在
        try:
            if not core.JSON_PATH.exists():
                with open(core.JSON_PATH, "w", encoding="utf-8") as f:
                    json.dump([], f, ensure_ascii=False)
        except Exception as e:
            LOGGER.warning("初始化 JSON 失败: %s", e)


def adb_device_status() -> tuple[bool, str]:
    """检查是否有可用的 adb 设备连接。返回 (ok, message)。"""
    try:
        res = core.run_adb_cmd(["devices"], timeout=10)
        if res.returncode != 0:
            return False, res.stderr.strip() or res.stdout.strip() or "adb devices failed"
        # 输出形如：\nList of devices attached\n<serial>\tdevice\n
        lines = [ln.strip() for ln in (res.stdout or "").splitlines()]
        devices = [ln for ln in lines if ln and not ln.lower().startswith("list of devices")]
        # 过滤掉 unauthorized/offline
        ready = [ln for ln in devices if ln.endswith("\tdevice")]
        if not ready:
            return False, "no device ready (check USB, authorization, or 'adb connect')"
        return True, ready[0]
    except Exception as e:
        return False, f"adb check error: {e}"


def normalize_bounds_str(bounds_str: str) -> str:
    """去除 bounds 内多余空白，兼容 "[ x1, y1 ][ x2, y2 ]" 这类格式。"""
    try:
        s = re.sub(r"\s+", "", str(bounds_str))
        return s
    except Exception:
        return bounds_str


def next_index_from_json() -> int:
    try:
        if core.JSON_PATH.exists():
            with open(core.JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return len(data)
    except Exception:
        return 0
    return 0


def find_node_by_bounds(xml_path: Path, bounds_str: str):
    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(xml_path)
        root = tree.getroot()
        for node in root.iter():
            if node.tag.lower() != "node":
                continue
            if node.attrib.get("bounds") == bounds_str:
                return node
    except Exception:
        return None
    return None


def find_node_by_xpath(xml_path: Path, xpath: str):
    """
    尝试在当前 XML 中按 XPath 查找节点。为兼容 weditor 常见形态：
    - //android.widget.TextView[@text='xxx']
      实际 XML 标签都是 <node ... class="android.widget.TextView" ...>
      因此需改写为：.//node[@class='android.widget.TextView' and @text='xxx']
    """
    import xml.etree.ElementTree as ET
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()

        # 新增：去掉整体括号分组，如 (//xxx)[7] -> //xxx[7]
        def unwrap_grouped_xpath(xp: str) -> str:
            xp = (xp or "").strip()
            m = re.match(r"^\(\s*(.+?)\s*\)\s*(\[\d+\])?\s*$", xp)
            if m:
                inner = m.group(1).strip()
                idx = m.group(2) or ""
                return f"{inner}{idx}"
            return xp

        xpath = unwrap_grouped_xpath(xpath)

        def norm_basic(xp: str) -> str:
            xp = (xp or "").strip()
            if xp.startswith("//"):
                xp = "." + xp
            elif xp.startswith("/"):
                xp = "." + xp
            if not xp.startswith("."):
                xp = ".//" + xp
            return xp

        def rewrite_android_class_segment(xp: str) -> str | None:
            # 仅取最后一段作为关键匹配点
            segs = re.split(r"/(?=[^/])", xp.strip().lstrip("./"))
            if not segs:
                return None
            last = segs[-1]
            # 例：android.widget.TextView[@text='xx'][1]
            m = re.match(r"^(?P<tag>[A-Za-z0-9_\.]+)(?P<preds>(\[.*\])*)$", last)
            if not m:
                return None
            tag = m.group("tag")
            preds = m.group("preds") or ""
            if tag.lower() in ("node", "hierarchy"):
                return None
            # 将 tag -> node[@class='tag']，保留其余谓词（将 [@..] 直接拼接）
            conds = []
            index_part = ""
            if preds:
                parts = re.findall(r"(\[[^\]]+\])", preds)
                for p in parts:
                    if re.match(r"^\[\d+\]$", p):
                        index_part += p
                    else:
                        conds.append(p.strip("[]"))
            conds.insert(0, f"@class='{tag}'")
            cond_expr = " and ".join(conds) if conds else f"@class='{tag}'"
            return f".//node[{cond_expr}]{index_part}"

        # 尝试：原表达式
        xp1 = norm_basic(xpath)
        try:
            node = root.find(xp1)
            if node is not None:
                return node
        except Exception:
            pass

        # 尝试：仅取最后一段重写为 node[@class='...'] 形式
        xp2 = rewrite_android_class_segment(xpath)
        if xp2:
            try:
                node = root.find(xp2)
                if node is not None:
                    return node
            except Exception:
                pass

        # 再尝试：如果包含 "//android."，截断到最后一段后使用重写
        if "//android." in xpath:
            tail = xpath.split("/")[-1]
            tail = unwrap_grouped_xpath(tail)  # 防止尾段仍带 ')'
            xp3 = rewrite_android_class_segment(tail) or tail
            if xp3:
                try:
                    node = root.find(norm_basic(xp3))
                    if node is not None:
                        return node
                except Exception:
                    pass

        # 终极兜底：手动解析最后一段（支持 @class/@text/@resource-id/@content-desc 以及 [n] 索引）
        try:
            last = xpath.strip().split("/")[-1]
            last = unwrap_grouped_xpath(last)
            m = re.match(r"^(?P<tag>[A-Za-z0-9_\.]+)(?P<preds>(\[[^\]]+\])*)$", last)
            if m:
                tag = m.group("tag")
                preds = m.group("preds") or ""
                conds: dict[str, str] = {}
                index_n: Optional[int] = None
                for p in re.findall(r"\[([^\]]+)\]", preds):
                    p = p.strip()
                    if re.fullmatch(r"\d+", p):
                        try:
                            index_n = int(p)
                        except Exception:
                            index_n = None
                    else:
                        m2 = re.match(r"@([A-Za-z0-9_\-]+)\s*=\s*(['\"])(.*?)\2", p)
                        if m2:
                            attr = m2.group(1)
                            val = m2.group(3)
                            conds[attr] = val
                target_class = None if tag.lower() in ("node", "hierarchy") else tag
                matches = []
                for nd in root.iter():
                    if nd.tag.lower() != "node":
                        continue
                    a = nd.attrib
                    if target_class and a.get("class") != target_class:
                        continue
                    ok = True
                    for k, v in conds.items():
                        if a.get(k) != v:
                            ok = False
                            break
                    if ok:
                        matches.append(nd)
                if matches:
                    if index_n is not None and 1 <= index_n <= len(matches):
                        return matches[index_n - 1]
                    return matches[0]
        except Exception:
            pass
    except Exception:
        return None
    return None


def compute_center(bounds: Tuple[int, int, int, int]) -> Tuple[int, int]:
    x1, y1, x2, y2 = bounds
    cx = (x1 + x2) // 2
    cy = (y1 + y2) // 2
    return cx, cy


def perform_tap(cx: int, cy: int) -> Optional[str]:
    """根据选择的后端执行点击。返回错误字符串或 None 表示成功。"""
    global _BACKEND, _U2
    if _BACKEND == "u2":
        try:
            if _U2 is None:
                return "uiautomator2 not connected"
            _U2.click(cx, cy)
            return None
        except Exception as e:
            return str(e)
    else:
        try:
            res = core.run_adb_cmd(["shell", "input", "tap", str(cx), str(cy)], timeout=10)
            if res.returncode != 0:
                return res.stderr.strip() or "adb input tap failed"
            return None
        except Exception as e:
            return str(e)


@app.get("/")
def home():
    return jsonify({
        "name": "UIAutodev Bridge Server",
        "endpoints": [
            "GET /health",
            "POST /bridge/capture_tap  { bounds?: '[x1,y1][x2,y2]', xpath?: '...', node?: {...}, action?: 'short-click|long-click|input|swipe|back', durationMs?, text?, dx?, dy?, direction?, distance?, waitAfterMs?(默认400), midCapture? }",
            "POST /bridge/final_screenshot  {}  -> 保存一条仅包含当前页面快照的记录（无点击）",
        ],
    })


@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return resp


@app.route("/bridge/capture_tap", methods=["OPTIONS"])
def bridge_capture_tap_options():
    return ("", 204)


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.post("/bridge/capture_tap")
def bridge_capture_tap():
    """核心端点：
    Body JSON:
    - bounds: 字符串，如 "[x1,y1][x2,y2]"（推荐）
    - xpath: UIAutodev 的 XPath（可选，若未提供 bounds，尝试用它定位）
    - node: 可选，前端已知的节点属性字典（text/class/resource-id 等），用于兜底写 JSON
        - action: 操作类型：short-click|long-click|input|swipe|back（默认 short-click）
            衍生参数：
                - durationMs: long-click/滑动持续时间（默认 800）
                - text: input 文本
                - dx, dy: swipe 位移（与 direction/distance 互斥，若给定 direction 则忽略 dx/dy）
                - direction: up/down/left/right/custom（默认 custom）
                - distance: 与 direction 联用的像素距离
                - waitAfterMs: 执行动作后的等待时间（默认 1000ms）
        - tap: bool（兼容旧参数），仅当未传 action 时生效
    """
    try:
        payload: Dict[str, Any] = request.get_json(force=True, silent=False)  # 抛错更易调试
    except Exception as e:
        LOGGER.warning("invalid JSON body: %s", e)
        return jsonify({"ok": False, "error": "invalid JSON"}), 400

    bounds_str: Optional[str] = payload.get("bounds")
    xpath: Optional[str] = payload.get("xpath")
    node_from_front: Dict[str, Any] = payload.get("node") or {}
    do_tap: bool = payload.get("tap", True)
    action_raw: Optional[str] = payload.get("action")
    duration_ms: int = int(payload.get("durationMs", 800) or 800)
    text_arg: Optional[str] = payload.get("text")
    dx: Optional[int] = payload.get("dx")
    dy: Optional[int] = payload.get("dy")
    direction: str = str(payload.get("direction", "custom")).lower()
    distance: Optional[int] = payload.get("distance")
    wait_after_ms: int = int(payload.get("waitAfterMs", 400) or 400)
    mid_capture: bool = bool(payload.get("midCapture", False))
    mid_delay_ms: int = int(payload.get("midDelayMs", 50) or 50)

    def norm_action(s: Optional[str]) -> str:
        t = (s or "").strip().lower().replace("_", "-")
        if t in ("", None):
            return "short-click" if do_tap else "none"
        if t in ("tap", "click", "short", "short-click"): return "short-click"
        if t in ("long-press", "long-press", "longclick", "long-click", "press"): return "long-click"
        if t in ("input", "text", "type"): return "input"
        if t == "swipe": return "swipe"
        if t in ("back", "system-back", "navigate-back"): return "back"
        return t or "short-click"

    action = norm_action(action_raw)

    # 0) 基础环境校验：adb 设备
    ok, msg = adb_device_status()
    if not ok:
        LOGGER.warning("设备未就绪: %s", msg)
        return jsonify({"ok": False, "error": f"adb device not ready: {msg}"}), 503

    # 1) 生成新序号并完成“点击前快照”（source_*）
    idx = next_index_from_json()
    elem_xml_path = core.XML_DIR / f"elem_{idx}.xml"
    pre_raw_path = core.IMAGE_DIR / f"elem_{idx}_raw.png"
    boxed_path = core.IMAGE_DIR / f"elem_{idx}_boxed.png"

    try:
        # 根据后端选择采集方式：u2 或 adb
        global _BACKEND, _U2
        if _BACKEND == "u2" and _U2 is not None:
            try:
                xml_text = _U2.dump_hierarchy()
            except Exception:
                # 某些版本提供的 API,控件树的存储
                xml_text = _U2.dump_hierarchy(compressed=False)
            Path(elem_xml_path).write_text(xml_text, encoding="utf-8")
            try:
                _U2.screenshot(str(pre_raw_path))
            except Exception:
                # 兜底：返回 PIL image
                img = _U2.screenshot()
                img.save(str(pre_raw_path))
            activity = ""
            try:
                cur = _U2.app_current() or {}
                pkg = cur.get("package")
                act = cur.get("activity")
                if pkg and act:
                    # 规范化为 pkg/Activity
                    activity = f"{pkg}/{act if act.startswith('.') else act}"
            except Exception:
                pass
        else:
            core.dump_ui_xml_to(elem_xml_path)
            core.take_screenshot_to(pre_raw_path)
            activity = core.get_current_activity()
    except Exception as e:
        LOGGER.error("预采集失败 idx=%s: %s\n%s", idx, e, traceback.format_exc())
        return jsonify({"ok": False, "error": f"pre-capture failed: {e}"}), 502

    # 2) 决定 bounds 与 node_attr（除 back 外都需要元素）
    node_attr: Dict[str, Any] = {}
    rect: Optional[Tuple[int, int, int, int]] = None

    if action != "back" and bounds_str:
        bounds_str = normalize_bounds_str(bounds_str)
        rect = core.parse_bounds(bounds_str)
        if rect is None:
            LOGGER.warning("invalid bounds format: %s", bounds_str)
            return jsonify({"ok": False, "error": "invalid bounds format"}), 400
        node = find_node_by_bounds(elem_xml_path, bounds_str)
        if node is not None:
            node_attr = dict(node.attrib)
        elif isinstance(node_from_front, dict) and node_from_front:
            node_attr = dict(node_from_front)
            node_attr.setdefault("bounds", bounds_str)
        else:
            node_attr = {"bounds": bounds_str}
    elif action != "back" and xpath:
        node = find_node_by_xpath(elem_xml_path, xpath)
        if node is None:
            LOGGER.warning("xpath not found in current XML: %s", xpath)
            return jsonify({"ok": False, "error": "xpath not found in current XML; supply bounds instead"}), 404
        node_attr = dict(node.attrib)
        bounds_str = node_attr.get("bounds")
        if not bounds_str:
            LOGGER.warning("node has no bounds for xpath: %s", xpath)
            return jsonify({"ok": False, "error": "node has no bounds; supply bounds explicitly"}), 400
        rect = core.parse_bounds(bounds_str)
        if rect is None:
            LOGGER.warning("parsed bounds invalid from node bounds: %s", node_attr.get("bounds"))
            return jsonify({"ok": False, "error": "parsed bounds invalid"}), 400
    elif action != "back":
        return jsonify({"ok": False, "error": "bounds or xpath required"}), 400

    # 3) 绘制 boxed
    try:
        if rect is not None:
            core.draw_rect_on_image(pre_raw_path, boxed_path, rect)
        else:
            # back 等无元素动作：直接复制 raw 作为 boxed
            boxed_path.write_bytes(pre_raw_path.read_bytes())
    except Exception as e:
        LOGGER.warning("绘制红框失败 idx=%s: %s", idx, e)
        # 失败则复制一份 raw
        try:
            boxed_path.write_bytes(pre_raw_path.read_bytes())
        except Exception:
            pass

    # 4) 执行动作
    cx: Optional[int] = None
    cy: Optional[int] = None
    if rect is not None:
        cx, cy = compute_center(rect)

    def perform_long_click(x: int, y: int, ms: int) -> Optional[str]:
        global _BACKEND, _U2
        if _BACKEND == "u2":
            try:
                if _U2 is None:
                    return "uiautomator2 not connected"
                # 某些版本 long_click 接受 seconds；这里统一传 ms/1000
                _U2.long_click(x, y, duration=ms/1000.0)
                return None
            except Exception as e:
                return str(e)
        else:
            try:
                # adb swipe 同点位 + 持续时间 即长按
                res = core.run_adb_cmd(["shell", "input", "swipe", str(x), str(y), str(x), str(y), str(int(ms))], timeout=15)
                if res.returncode != 0:
                    return res.stderr.strip() or "adb long press failed"
                return None
            except Exception as e:
                return str(e)

    def perform_swipe(x1: int, y1: int, x2: int, y2: int, ms: int) -> Optional[str]:
        global _BACKEND, _U2
        if _BACKEND == "u2":
            try:
                if _U2 is None: return "uiautomator2 not connected"
                _U2.swipe(x1, y1, x2, y2, duration=ms/1000.0)
                return None
            except Exception as e:
                return str(e)
        else:
            try:
                res = core.run_adb_cmd(["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(int(ms))], timeout=15)
                if res.returncode != 0:
                    return res.stderr.strip() or "adb swipe failed"
                return None
            except Exception as e:
                return str(e)

    def perform_back() -> Optional[str]:
        global _BACKEND, _U2
        if _BACKEND == "u2":
            try:
                if _U2 is None: return "uiautomator2 not connected"
                _U2.press("back")
                return None
            except Exception as e:
                return str(e)
        else:
            try:
                res = core.run_adb_cmd(["shell", "input", "keyevent", "4"], timeout=10)
                if res.returncode != 0:
                    return res.stderr.strip() or "adb back failed"
                return None
            except Exception as e:
                return str(e)

    def perform_input_text(txt: str) -> Optional[str]:
        global _BACKEND, _U2
        if _BACKEND == "u2":
            try:
                if _U2 is None: return "uiautomator2 not connected"
                try:
                    _U2.send_keys(txt)
                except Exception:
                    # 兜底：启用快速输入法
                    try:
                        _U2.set_fastinput_ime(True)
                    except Exception:
                        pass
                    _U2.send_keys(txt)
                return None
            except Exception as e:
                return str(e)
        else:
            try:
                # adb shell input text 需要对空格等转义
                safe = (txt or "").replace(" ", "%s")
                res = core.run_adb_cmd(["shell", "input", "text", safe], timeout=20)
                if res.returncode != 0:
                    return res.stderr.strip() or "adb input text failed"
                return None
            except Exception as e:
                return str(e)

    # 基础参数校验（保持旧行为的一致错误码）
    if action in ("short-click", "long-click", "swipe") and (cx is None or cy is None):
        return jsonify({"ok": False, "error": "no center; element required"}), 400
    if action == "input" and not text_arg:
        return jsonify({"ok": False, "error": "text required for input action"}), 400

    def exec_action() -> Optional[str]:
        if action == "short-click":
            return perform_tap(cx, cy)  # type: ignore[arg-type]
        elif action == "long-click":
            return perform_long_click(cx, cy, duration_ms)  # type: ignore[arg-type]
        elif action == "input":
            return perform_input_text(str(text_arg))
        elif action == "swipe":
            sx, sy = cx, cy  # type: ignore[assignment]
            if direction in ("up", "down", "left", "right") and (distance is not None):
                d = int(distance)
                if direction == "up": tx, ty = sx, max(0, sy - d)
                elif direction == "down": tx, ty = sx, sy + d
                elif direction == "left": tx, ty = max(0, sx - d), sy
                else: tx, ty = sx + d, sy
            else:
                tx = sx + int(dx or 0)
                ty = sy + int(dy or 0)
            return perform_swipe(sx, sy, int(tx), int(ty), duration_ms)
        elif action == "back":
            return perform_back()
        elif action == "none":
            return None
        else:
            return f"unknown action: {action}"

    # 5) 采集动作后的页面（dest_*），支持 midCapture

    dest_img_path = core.IMAGE_DIR / f"elem_{idx}_dest.png"
    dest_xml_path = core.XML_DIR / f"elem_{idx}_dest.xml"
    dest_activity = ""
    action_error: Optional[str] = None
    if mid_capture:
        # 动作并发执行，立即开始采集
        th = threading.Thread(target=lambda: exec_action(), daemon=True)
        th.start()
        # 微小延迟避免与截图竞争（可配置）
        try:
            if mid_delay_ms > 0:
                time.sleep(mid_delay_ms / 1000.0)
        except Exception:
            pass
        try:
            if _BACKEND == "u2" and _U2 is not None:
                try:
                    _U2.screenshot(str(dest_img_path))
                except Exception:
                    img = _U2.screenshot()
                    img.save(str(dest_img_path))
                try:
                    try:
                        dest_xml_text = _U2.dump_hierarchy()
                    except Exception:
                        dest_xml_text = _U2.dump_hierarchy(compressed=False)
                    Path(dest_xml_path).write_text(dest_xml_text, encoding="utf-8")
                except Exception:
                    pass
                try:
                    cur = _U2.app_current() or {}
                    pkg = cur.get("package"); act = cur.get("activity")
                    if pkg and act:
                        dest_activity = f"{pkg}/{act if act.startswith('.') else act}"
                except Exception:
                    pass
            else:
                core.take_screenshot_to(dest_img_path)
                try:
                    core.dump_ui_xml_to(dest_xml_path)
                except Exception:
                    pass
                dest_activity = core.get_current_activity()
        except Exception as e:
            LOGGER.warning("进行时采集失败 idx=%s: %s", idx, e)
        # 等待动作结束但不要无限阻塞
        try:
            timeout_s = (duration_ms / 1000.0 + 1.5) if action in ("long-click", "swipe") else 1.0
            th.join(timeout=timeout_s)
        except Exception:
            pass
        # 无法直接拿到线程返回，按需重做一次（可选不取）
        # 为避免重复执行动作，这里不再补发，只保留 None
        action_error = None
    else:
        action_error = exec_action()
        if wait_after_ms > 0:
            time.sleep(wait_after_ms / 1000.0)
        try:
            if _BACKEND == "u2" and _U2 is not None:
                try:
                    _U2.screenshot(str(dest_img_path))
                except Exception:
                    img = _U2.screenshot()
                    img.save(str(dest_img_path))
                try:
                    try:
                        dest_xml_text = _U2.dump_hierarchy()
                    except Exception:
                        dest_xml_text = _U2.dump_hierarchy(compressed=False)
                    Path(dest_xml_path).write_text(dest_xml_text, encoding="utf-8")
                except Exception:
                    pass
                try:
                    cur = _U2.app_current() or {}
                    pkg = cur.get("package"); act = cur.get("activity")
                    if pkg and act:
                        dest_activity = f"{pkg}/{act if act.startswith('.') else act}"
                except Exception:
                    pass
            else:
                core.take_screenshot_to(dest_img_path)
                try:
                    core.dump_ui_xml_to(dest_xml_path)
                except Exception:
                    pass
                dest_activity = core.get_current_activity()
        except Exception as e:
            LOGGER.warning("采集动作后截图失败 idx=%s: %s", idx, e)

    # 6) 写入 JSON 记录
    try:
        # 计算 swipe 记录的距离（若有）
        swipe_dist_val = None
        swipe_dir_val = None
        if action == "swipe":
            try:
                if direction in ("up", "down", "left", "right") and (distance is not None):
                    swipe_dist_val = int(distance)
                    swipe_dir_val = direction
                elif cx is not None and cy is not None:
                    tx = cx + int(dx or 0)
                    ty = cy + int(dy or 0)
                    swipe_dist_val = int(((tx - cx)**2 + (ty - cy)**2) ** 0.5)
                    # 基于 dx/dy 推断方向（主轴为绝对值较大的分量）
                    ddx = int(dx or 0)
                    ddy = int(dy or 0)
                    if abs(ddx) >= abs(ddy):
                        if ddx > 0:
                            swipe_dir_val = "right"
                        elif ddx < 0:
                            swipe_dir_val = "left"
                    else:
                        if ddy > 0:
                            swipe_dir_val = "down"
                        elif ddy < 0:
                            swipe_dir_val = "up"
            except Exception:
                swipe_dist_val = None
                swipe_dir_val = None

        core.append_json_record(
            elem_id=f"elem_{idx}",
            click_xy=(cx, cy) if (cx is not None and cy is not None and action != "back") else None,
            node_attr=node_attr,
            raw_img=pre_raw_path,
            boxed_img=boxed_path,
            xml_path=elem_xml_path,
            activity=activity,
            dest_img=dest_img_path,
            source_activity=activity,
            dest_activity=dest_activity,
            action=action,
            is_activity_jumped=(activity != dest_activity) if (activity or dest_activity) else None,
            input_text=(str(text_arg) if action == "input" and text_arg else None),
            swipe_distance=swipe_dist_val,
            swipe_direction=swipe_dir_val,
            duration=(int(duration_ms) if action in ("long-click", "swipe") else None),
            source_xml=elem_xml_path,
            dest_xml=(dest_xml_path if dest_xml_path.exists() else None),
        )
    except Exception as e:
        LOGGER.error("写入 JSON 失败 idx=%s: %s\n%s", idx, e, traceback.format_exc())
        return jsonify({"ok": False, "error": f"append json failed: {e}"}), 500

    resp = {
        "ok": True,
        "elem_id": f"elem_{idx}",
        "center": ({"x": cx, "y": cy} if (cx is not None and cy is not None) else None),
        "activity": activity,
    "capture_timing": ("mid" if mid_capture else "post"),
        "files": {
            "raw": str(pre_raw_path.resolve()),
            "boxed": str(boxed_path.resolve()),
            "xml": str(elem_xml_path.resolve()),
            "json": str(core.JSON_PATH.resolve()),
            "dest": str(dest_img_path.resolve()),
            "dest_xml": str(dest_xml_path.resolve()) if dest_xml_path.exists() else None,
        },
    }
    if action_error:
        resp["action_error"] = action_error
    return jsonify(resp)


@app.post("/bridge/final_screenshot")
def final_screenshot():
    """保存当前页面的一条‘最终快照’记录（无点击），包含 XML、raw、boxed(同raw) 与 activity。"""
    # 基础环境校验
    ok, msg = adb_device_status()
    if not ok:
        return jsonify({"ok": False, "error": f"adb device not ready: {msg}"}), 503

    idx = next_index_from_json()
    elem_xml_path = core.XML_DIR / f"elem_{idx}.xml"
    raw_path = core.IMAGE_DIR / f"elem_{idx}_raw.png"
    boxed_path = core.IMAGE_DIR / f"elem_{idx}_boxed.png"

    try:
        global _BACKEND, _U2
        if _BACKEND == "u2" and _U2 is not None:
            try:
                xml_text = _U2.dump_hierarchy()
            except Exception:
                xml_text = _U2.dump_hierarchy(compressed=False)
            Path(elem_xml_path).write_text(xml_text, encoding="utf-8")
            try:
                _U2.screenshot(str(raw_path))
            except Exception:
                img = _U2.screenshot()
                img.save(str(raw_path))
            activity = ""
            try:
                cur = _U2.app_current() or {}
                pkg = cur.get("package"); act = cur.get("activity")
                if pkg and act:
                    activity = f"{pkg}/{act if act.startswith('.') else act}"
            except Exception:
                pass
        else:
            core.dump_ui_xml_to(elem_xml_path)
            core.take_screenshot_to(raw_path)
            activity = core.get_current_activity()
        # boxed 同 raw
        try:
            boxed_path.write_bytes(raw_path.read_bytes())
        except Exception:
            pass

        # 记录
        core.append_json_record(
            elem_id=f"elem_{idx}",
            click_xy=None,
            node_attr={},
            raw_img=raw_path,
            boxed_img=boxed_path,
            xml_path=elem_xml_path,
            activity=activity,
            action="final",
        )
    except Exception as e:
        LOGGER.error("final_screenshot 失败 idx=%s: %s\n%s", idx, e, traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({
        "ok": True,
        "elem_id": f"elem_{idx}",
        "activity": activity,
        "file": str(raw_path.resolve()),
        "xml": str(elem_xml_path.resolve()),
    })


# 兜底的全局异常处理，避免 500 无日志
@app.errorhandler(Exception)
def handle_unexpected_error(err):
    try:
        LOGGER.error("Unhandled error: %s\n%s", err, traceback.format_exc())
    except Exception:
        pass
    # 返回最小化错误信息；如需带堆栈，可设置 BRIDGE_DEBUG=1
    body = {"ok": False, "error": str(err)}
    if str(os.environ.get("BRIDGE_DEBUG", "")).lower() in ("1", "true", "yes"):
        body["trace"] = traceback.format_exc()
    return jsonify(body), 500


@app.get("/autosend.js")
def serve_autosend_js():
        """提供 weditor 自动发送脚本，便于在浏览器端注入。
        用法（weditor 控制台）：
            var s=document.createElement('script'); s.src='http://127.0.0.1:8001/autosend.js'; document.head.appendChild(s)
        """
        try:
                js_path = Path(__file__).resolve().parent / "weditor_autosend.js"
                content = js_path.read_text(encoding="utf-8")
                return content, 200, {"Content-Type": "application/javascript; charset=utf-8"}
        except Exception as e:
                LOGGER.warning("读取 autosend.js 失败: %s", e)
                return "// autosend.js not found", 404, {"Content-Type": "application/javascript"}


def main():
    parser = argparse.ArgumentParser(description="UIAutodev Bridge Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--reset-json", action="store_true")
    parser.add_argument("--backend", choices=["adb", "u2"], default="adb", help="点击后端：adb 或 u2(uiautomator2)")
    args = parser.parse_args()

    core.check_adb_available()
    ensure_ready(reset_json=args.reset_json)

    global _BACKEND, _U2
    _BACKEND = args.backend
    if _BACKEND == "u2":
        try:
            import uiautomator2 as u2  # type: ignore
            _U2 = u2.connect()  # 默认连接
            LOGGER.info("uiautomator2 connected")
        except Exception as e:
            LOGGER.warning("uiautomator2 连接失败，将回退 adb：%s", e)
            _BACKEND = "adb"

    LOGGER.info("Listening on http://%s:%s", args.host, args.port)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
