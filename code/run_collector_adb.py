"""
实时监听 Android 触摸事件并自动采集 UI 数据。

功能概述：
- 启动 adb shell getevent -lt，实时解析点击事件（DOWN -> X/Y -> UP）。
- 捕获到点击坐标后，立即执行：
    * uiautomator dump -> 拉取 XML
    * exec-out screencap -> 拉取截图
    * dumpsys activity -> 获取当前 Activity
- 解析 XML，找到最小包含点击坐标的 node，并提取属性。
- 在截图上绘制红框并与原图一同保存；将采集信息追加写入 JSON。
- Ctrl+C 退出时保存最终截图并优雅退出。

依赖：
- Python 标准库: subprocess, re, time, datetime, os, pathlib, sys, threading, queue, xml.etree.ElementTree, io
- 第三方库: Pillow (PIL)

作者：自动生成 by GitHub Copilot
"""

from __future__ import annotations

import os
import re
import sys
import time
import queue
import signal
import atexit
import threading
import subprocess
from io import BytesIO
import json
from pathlib import Path
from datetime import datetime
import xml.etree.ElementTree as ET

# 第三方库导入与友好提示
try:
    from PIL import Image, ImageDraw
except Exception as e:  # pragma: no cover
    print("[ERROR] 未安装 Pillow，请先安装: pip install pillow", file=sys.stderr)
    raise

# JSON 使用标准库，不需要额外依赖


# -----------------------------
# 路径与常量
# -----------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_OUT_DIR = SCRIPT_DIR / "UI_Automated_acquisition"
IMAGE_DIR = BASE_OUT_DIR / "image"
JSON_PATH = BASE_OUT_DIR / "collected_data.json"
XML_DIR = BASE_OUT_DIR / "element_xml"
TEMP_XML_LOCAL = SCRIPT_DIR / "window_dump.xml"  # 可选调试文件
TEMP_SCREENSHOT = SCRIPT_DIR / "temp_screenshot.png"
TEMP_PRE_SCREENSHOT = SCRIPT_DIR / "temp_pre_screenshot.png"
VERBOSE = os.environ.get("ADB_LISTENER_VERBOSE", "0").lower() in ("1", "true", "yes")
FORCE_EVENT_DEVICE = os.environ.get("ADB_EVENT_DEVICE")  # 例如: /dev/input/event2
IMAGE_INDEX = 1  # 每次脚本启动从 1 开始累加


def debug(msg: str):
    if VERBOSE:
        print(f"[DBG] {msg}")


def timestamp_str() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


def ensure_directories():
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    BASE_OUT_DIR.mkdir(parents=True, exist_ok=True)
    XML_DIR.mkdir(parents=True, exist_ok=True)


def check_adb_available():
    try:
        res = subprocess.run(["adb", "version"], capture_output=True, text=True, timeout=10)
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or res.stdout.strip())
    except FileNotFoundError:
        print("[ERROR] 未找到 adb 命令，请确保已安装并加入 PATH。", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] adb 检查失败: {e}", file=sys.stderr)
        sys.exit(1)


def run_adb_cmd(args: list[str], timeout: float | None = None, capture: bool = True) -> subprocess.CompletedProcess:
    """运行 adb 命令的帮助函数。"""
    cmd = ["adb"] + args
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    else:
        return subprocess.run(cmd, timeout=timeout)


def adb_exec_out_bytes(args: list[str], timeout: float | None = None) -> bytes:
    """调用 adb exec-out 并返回二进制输出。"""
    cmd = ["adb", "exec-out"] + args
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="ignore") or "exec-out failed")
    return proc.stdout


def autodetect_touch_event_device() -> str | None:
    """
    使用 `adb shell getevent -pl` 自动探测触摸屏对应的 /dev/input/eventX。
    返回设备路径或 None（表示回退到监听所有设备）。
    """
    try:
        res = run_adb_cmd(["shell", "getevent", "-pl"], timeout=10)
        if res.returncode != 0:
            return None
        output = res.stdout
    except Exception:
        return None

    # 将输出按设备块进行解析
    # 常见片段：
    # add device 1: /dev/input/event2
    #   name:     "goodix_ts"
    #   events:
    #     KEY ... BTN_TOUCH ...
    #     ABS ... ABS_MT_POSITION_X ... ABS_MT_POSITION_Y ...
    blocks = re.split(r"\n(?=add device \d+: )", output)
    candidates: list[tuple[str, str]] = []  # (dev_path, name)
    for blk in blocks:
        m_dev = re.search(r"add device \d+:\s+(?P<dev>/dev/input/event\d+)", blk)
        if not m_dev:
            continue
        dev = m_dev.group("dev")
        name_m = re.search(r"name:\s+\"(?P<name>.+?)\"", blk)
        name = name_m.group("name") if name_m else ""
        has_btn_touch = re.search(r"BTN_TOUCH", blk, re.I) is not None
        has_abs_pos = re.search(r"ABS_MT_POSITION_X|ABS_X", blk, re.I) is not None and \
                       re.search(r"ABS_MT_POSITION_Y|ABS_Y", blk, re.I) is not None
        name_hint = re.search(r"touch|ts|finger", name, re.I) is not None
        score = (2 if has_btn_touch else 0) + (2 if has_abs_pos else 0) + (1 if name_hint else 0)
        if score > 0:
            candidates.append((dev, name, score))  # type: ignore

    if not candidates:
        return None
    # 选择分数最高的设备
    candidates.sort(key=lambda x: x[2], reverse=True)  # type: ignore
    return candidates[0][0]


class GetEventReader(threading.Thread):
    """后台线程：读取 adb shell getevent -lt 输出并逐行放入队列。"""

    def __init__(self, device: str | None, line_queue: queue.Queue[str]):
        super().__init__(daemon=True)
        self.device = device
        self.line_queue = line_queue
        self.proc: subprocess.Popen | None = None
        self._stop_event = threading.Event()

    def run(self):
        args = ["shell", "getevent", "-lt"]
        if self.device:
            args.append(self.device)
        # 使用逐行输出
        self.proc = subprocess.Popen(["adb"] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, universal_newlines=True)
        try:
            assert self.proc.stdout is not None
            for line in self.proc.stdout:
                if self._stop_event.is_set():
                    break
                self.line_queue.put(line.rstrip("\r\n"))
        except Exception:
            pass

    def stop(self):
        self._stop_event.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
            try:
                # 等待片刻再强杀
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


class ClickDetector:
    """解析 getevent 行文本，识别点击事件并输出坐标。"""

    # 代码映射（十六进制字符串 -> 含义）
    CODE_MAP = {
        # EV_KEY: BTN_TOUCH
    "014A": "BTN_TOUCH",
    # EV_KEY: BTN_TOOL_FINGER (部分设备用它表示手指触摸)
    "0145": "BTN_TOOL_FINGER",
        # EV_ABS: Multi-touch positions
        "0035": "ABS_MT_POSITION_X",
        "0036": "ABS_MT_POSITION_Y",
    # EV_ABS: Multi-touch slot
    "002F": "ABS_MT_SLOT",
    # EV_ABS: Tracking ID（-1 表示抬起）
    "0039": "ABS_MT_TRACKING_ID",
        # 有些设备会输出 ABS_X/ABS_Y 为 0000/0001
        "0000": "ABS_X",
        "0001": "ABS_Y",
    }

    def __init__(self):
        self.touch_active: bool = False
        self.tracking_id: int | None = None
        self.last_x: int | None = None
        self.last_y: int | None = None

    LINE_RE = re.compile(
        r"^\[\s*\d+\.\d+\]\s+"  # 时间戳
        r"(?:(?P<dev>/dev/input/event\d+):\s+)?"  # 可选设备路径
        r"(?P<type>(EV_\w+|[0-9a-fA-F]{4}))\s+"   # 类型
        r"(?P<code>([A-Z0-9_]+|[0-9a-fA-F]{4}))\s+"  # 代码
        r"(?P<value>(DOWN|UP|[0-9a-fA-F]+))",  # 值
        re.IGNORECASE,
    )

    def parse_line(self, line: str) -> tuple[bool, tuple[int, int] | None, bool]:
        """
        解析一行 getevent 输出。
        返回三元组 (clicked, (x, y) or None, down_started)。
        - clicked=True 表示抬起完成一次点击；
        - down_started=True 表示刚发生按下（用于预采集）。
        """
        m = self.LINE_RE.match(line.strip())
        if not m:
            return False, None, False

        type_field = m.group("type").upper()
        code_field = m.group("code").upper()
        val_field = m.group("value").upper()

        # 将数字化的类型转换为语义
        if type_field == "0001":
            ev_type = "EV_KEY"
        elif type_field == "0003":
            ev_type = "EV_ABS"
        else:
            ev_type = type_field

        # 将代码可能的 16 进制映射到名称
        if re.fullmatch(r"[0-9A-F]{4}", code_field):
            code_name = self.CODE_MAP.get(code_field, code_field)
        else:
            code_name = code_field

        # 将 value 解析为数值或语义
        if val_field in ("DOWN", "UP"):
            val = 1 if val_field == "DOWN" else 0
        else:
            try:
                # ffffffff 在某些设备代表 -1（如 TRACKING_ID 释放）
                if val_field.lower() == "ffffffff":
                    val = -1
                else:
                    val = int(val_field, 16)
            except Exception:
                # 有些设备会输出十进制
                try:
                    val = int(val_field)
                except Exception:
                    return False, None, False

        # 处理逻辑
        clicked = False
        click_xy: tuple[int, int] | None = None
        down_started = False

        if ev_type == "EV_ABS" and code_name in ("ABS_MT_POSITION_X", "ABS_X"):
            self.last_x = val
        elif ev_type == "EV_ABS" and code_name in ("ABS_MT_POSITION_Y", "ABS_Y"):
            self.last_y = val
        elif ev_type == "EV_ABS" and code_name == "ABS_MT_TRACKING_ID":
            # 触摸跟踪 ID 变化：>=0 表示按下/接触，-1 表示抬起
            if val >= 0:
                self.tracking_id = val
                self.touch_active = True
                down_started = True
            else:  # -1 抬起
                if self.touch_active and self.last_x is not None and self.last_y is not None:
                    clicked = True
                    click_xy = (self.last_x, self.last_y)
                self.touch_active = False
                self.tracking_id = None
        elif ev_type == "EV_KEY" and code_name in ("BTN_TOUCH", "BTN_TOOL_FINGER"):
            if val == 1:  # DOWN
                self.touch_active = True
                down_started = True
            elif val == 0:  # UP
                # 完整点击完成
                if self.touch_active and self.last_x is not None and self.last_y is not None:
                    clicked = True
                    click_xy = (self.last_x, self.last_y)
                self.touch_active = False

        if VERBOSE and (code_name in ("ABS_MT_POSITION_X", "ABS_MT_POSITION_Y", "ABS_X", "ABS_Y", "ABS_MT_TRACKING_ID", "ABS_MT_SLOT", "BTN_TOUCH", "BTN_TOOL_FINGER")):
            debug(f"{ev_type} {code_name} -> {val}; active={self.touch_active}; x={self.last_x}, y={self.last_y}; clicked={clicked}; down={down_started}")

        return clicked, click_xy, down_started


def dump_ui_xml_to(dst: Path) -> Path:
    # 触发 dump
    res = run_adb_cmd(["shell", "uiautomator", "dump", "/sdcard/window_dump.xml"], timeout=10)
    if res.returncode != 0:
        raise RuntimeError(f"uiautomator dump 失败: {res.stderr.strip()}")
    # 拉取到指定位置
    res2 = run_adb_cmd(["pull", "/sdcard/window_dump.xml", str(dst)], timeout=15)
    if res2.returncode != 0:
        raise RuntimeError(f"拉取 XML 失败: {res2.stderr.strip()}")
    return dst


def take_screenshot_to(path: Path):
    data = adb_exec_out_bytes(["screencap", "-p"], timeout=15)
    # exec-out screencap 已经是 PNG 数据
    path.write_bytes(data)


def get_current_activity() -> str:
    try:
        res = run_adb_cmd(["shell", "dumpsys", "activity"], timeout=15)
        if res.returncode != 0:
            return ""
        out = res.stdout
        # 容错：确保为 str
        if not isinstance(out, str) and out is not None:
            try:
                out = out.decode(errors="ignore")  # type: ignore
            except Exception:
                out = str(out)
        if not isinstance(out, str):
            return ""

        text = out
        # 兼容多种输出格式，取包含 mResumedActivity 的行
        m = re.search(r"mResumedActivity:\s*(.*?)\n", text)
        if not m:
            # 备用：mFocusedActivity
            m = re.search(r"mFocusedActivity:\s*(.*?)\n", text)
        if m:
            line = m.group(1)
            # 常见形态: ActivityRecord{... u0 com.pkg/.MainActivity}
            m2 = re.search(r"\s([\w\.]+)/(\.?[\w\.$]+)", line)
            if m2:
                pkg, act = m2.group(1), m2.group(2)
                if act.startswith("."):
                    act = pkg + act
                return f"{pkg}{act}"
            return line.strip()
        return ""
    except Exception:
        return ""


def parse_bounds(bounds_str: str) -> tuple[int, int, int, int] | None:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return x1, y1, x2, y2


def find_smallest_node_containing(xml_path: Path, x: int, y: int):
    tree = ET.parse(xml_path)
    root = tree.getroot()
    best_node = None
    best_area = None

    # uiautomator dump 通常为 <hierarchy> 下多层 <node>
    for node in root.iter():
        if node.tag.lower() != "node":
            continue
        b = node.attrib.get("bounds")
        if not b:
            continue
        rect = parse_bounds(b)
        if not rect:
            continue
        x1, y1, x2, y2 = rect
        if x1 <= x <= x2 and y1 <= y <= y2:
            area = (x2 - x1) * (y2 - y1)
            if area < 0:
                continue
            if best_area is None or area < best_area:
                best_area = area
                best_node = node

    return best_node


def draw_rect_on_image(src_png: Path, dst_png: Path, rect: tuple[int, int, int, int]):
    img = Image.open(src_png).convert("RGB")
    draw = ImageDraw.Draw(img)
    x1, y1, x2, y2 = rect
    # 画一个红色矩形框，线宽 4
    for i in range(4):
        draw.rectangle([x1 - i, y1 - i, x2 + i, y2 + i], outline=(255, 0, 0))
    img.save(dst_png)


def append_yaml_record(activity: str, click_xy: tuple[int, int], node_attr: dict, raw_img: Path, boxed_img: Path, xml_path: Path):
    # 已弃用：保留函数签名避免旧代码调用，但直接抛出说明
    raise NotImplementedError("append_yaml_record 已弃用，已改为 JSON 输出，请使用 append_json_record")


def append_json_record(
    elem_id: str,
    click_xy: tuple[int, int] | None,
    node_attr: dict,
    raw_img: Path,
    boxed_img: Path,
    xml_path: Path,
    activity: str | None = "",
    *,
    # 新增可选字段，保持向后兼容
    dest_img: Path | None = None,
    source_activity: str | None = None,
    dest_activity: str | None = None,
    action: str | None = None,
    is_activity_jumped: bool | None = None,
    # 本次新增动作细节
    input_text: str | None = None,
    swipe_distance: int | None = None,
    swipe_direction: str | None = None,
    duration: int | None = None,
    source_xml: Path | None = None,
    dest_xml: Path | None = None,
):
    """将一条采集记录追加到 collected_data.json（数组）中。

    目标 JSON 结构（每条记录）：
    {
      "elem_id": "elem_0",
      "time": "2025-08-15T12:34:56",
      "click": {"x": 123, "y": 456},
      "node": {
        "bounds": "[x1,y1][x2,y2]",
        "text": "...",
        "class": "...",
        "resource-id": "...",
        "content-desc": "...",
        "package": "..."
      },
    "images": {
        "raw": ".../elem_0_raw.png",
        "boxed": ".../elem_0_boxed.png"
      },
    "xml": ".../tmp_nodes.xml",
    "activity": "com.pkg/.MainActivity"
    }
    """
    # 统一字段映射，兼容不同属性命名
    node = {
        "bounds": node_attr.get("bounds"),
        "text": node_attr.get("text"),
        "class": node_attr.get("class"),
        "resource-id": node_attr.get("resource-id") or node_attr.get("resourceId"),
        "content-desc": node_attr.get("content-desc") or node_attr.get("contentDescription"),
        "package": node_attr.get("package"),
    }

    record = {
        "elem_id": elem_id,
        "time": datetime.now().isoformat(timespec="seconds"),
        "node": node,
        "images": {
            "raw": str(Path(raw_img).resolve()),
            "boxed": str(Path(boxed_img).resolve()),
        },
        "xml": str(Path(xml_path).resolve()),
        # 为兼容旧结构，保留 activity 字段，同时新增 source/dest 字段
        "activity": activity or (source_activity or ""),
    }
    if source_xml is not None:
        record["source_xml"] = str(Path(source_xml).resolve())
    if dest_xml is not None:
        record["dest_xml"] = str(Path(dest_xml).resolve())

    # 可选 click 字段（如 back 动作可能无坐标）
    if click_xy is not None:
        record["click"] = {"x": int(click_xy[0]), "y": int(click_xy[1])}

    # 追加新字段
    if dest_img is not None:
        record["images"]["dest"] = str(Path(dest_img).resolve())
    if source_activity is not None:
        record["source_activity"] = source_activity
    if dest_activity is not None:
        record["dest_activity"] = dest_activity
    if action is not None:
        record["action"] = action
    if is_activity_jumped is not None:
        record["is_activity_jumped"] = bool(is_activity_jumped)
    if input_text is not None:
        record["input_text"] = str(input_text)
    if swipe_distance is not None:
        try:
            record["swipe_distance"] = int(swipe_distance)
        except Exception:
            pass
    if swipe_direction is not None:
        try:
            record["swipe_direction"] = str(swipe_direction)
        except Exception:
            pass
    if duration is not None:
        try:
            record["duration"] = int(duration)
        except Exception:
            pass

    # 读取现有 JSON（如果损坏则回退为空数组）
    data: list = []
    try:
        if JSON_PATH.exists():
            with open(JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, list):
                    data = []
    except Exception:
        data = []

    data.append(record)

    # 覆盖写回
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class PreClickSnapshot:
    def __init__(self, index: int, xml_path: Path, screenshot_path: Path, activity: str):
        self.index = index
        self.xml_path = xml_path
        self.screenshot_path = screenshot_path
        self.activity = activity


def capture_pre_click_snapshot() -> PreClickSnapshot | None:
    idx = IMAGE_INDEX
    try:
        # 将“点击前”的控件树按元素序号保存为独立 XML 文件
        elem_xml_path = XML_DIR / f"elem_{idx}.xml"
        dump_ui_xml_to(elem_xml_path)
        # 将“点击前”的截图直接保存到 image 目录（作为 raw）
        pre_raw_path = IMAGE_DIR / f"elem_{idx}_raw.png"
        take_screenshot_to(pre_raw_path)
        activity = get_current_activity()
        debug(f"预采集完成 idx={idx}, xml={elem_xml_path.name}, image={pre_raw_path.name}, activity={activity}")
        return PreClickSnapshot(idx, elem_xml_path, pre_raw_path, activity)
    except Exception as e:
        print(f"[WARN] 预采集失败: {e}")
        return None


def handle_click_with_snapshot(click_xy: tuple[int, int], snap: PreClickSnapshot | None) -> bool:
    x, y = click_xy
    print(f"[INFO] 捕获点击: ({x}, {y}) —— 使用‘点击前’快照进行采集…")

    # 若无可用快照，降级到现采集（不理想，但保证不中断）
    if snap is None:
        print("[WARN] 预采集未完成/失败，本次点击无效（已忽略）。")
        print("[HINT] 检测到预采集未完成时的点击，请撤销你的点击操作。")
        return False
    else:
        xml_path = snap.xml_path
        activity = snap.activity
        source_img = snap.screenshot_path
        current_idx = snap.index

    # 解析 XML 找到控件
    try:
        node = find_smallest_node_containing(xml_path, x, y)
    except Exception as e:
        print(f"[WARN] 解析 XML 失败: {e}")
        node = None
    node_attr = node.attrib if node is not None else {}
    rect = parse_bounds(node_attr.get("bounds", "")) if node is not None else None

    # 输出文件名（保留原图路径为“预采集的 raw 图”，另生成 boxed 图）
    raw_path = Path(source_img)
    boxed_path = IMAGE_DIR / f"elem_{current_idx}_boxed.png"

    # 绘制红框（若找到 rect）
    if rect:
        try:
            draw_rect_on_image(source_img, boxed_path, rect)
        except Exception as e:
            print(f"[WARN] 绘制红框失败: {e}")
            boxed_path.write_bytes(raw_path.read_bytes())
    else:
        boxed_path.write_bytes(raw_path.read_bytes())

    # 记录 JSON
    try:
        append_json_record(
            elem_id=f"elem_{current_idx}",
            click_xy=click_xy,
            node_attr=node_attr,
            raw_img=raw_path,
            boxed_img=boxed_path,
            xml_path=xml_path,
            activity=activity,
        )
    except Exception as e:
        print(f"[WARN] 写入 JSON 失败: {e}")

    print(f"[DONE] 采集完成（基于点击前快照）：{raw_path.name}, {boxed_path.name}, Activity={activity}")
    return True


def final_screenshot_on_exit():
    try:
        final_path = IMAGE_DIR / "final_screenshot.png"
        take_screenshot_to(final_path)
        print(f"[INFO] 已保存最终截图: {final_path}")
    except Exception as e:
        print(f"[WARN] 保存最终截图失败: {e}")


def main():
    print("[INIT] 检查 adb 与创建目录…")
    global IMAGE_INDEX
    check_adb_available()
    ensure_directories()
    # 重置（覆盖）collected_data.json 为一个空数组
    try:
        with open(JSON_PATH, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False)
        debug(f"已重置 {JSON_PATH.name}")
    except Exception as e:
        print(f"[WARN] 初始化 JSON 文件失败: {e}")

    # 在退出时尝试保存最终截图
    atexit.register(final_screenshot_on_exit)

    print("[INIT] 自动探测触摸屏设备…")
    dev = FORCE_EVENT_DEVICE if FORCE_EVENT_DEVICE else autodetect_touch_event_device()
    if dev:
        print(f"[INIT] 触摸设备: {dev}")
    else:
        print("[INIT] 未能精确识别触摸设备，回退到监听所有设备。")

    # 启动读取线程
    line_q: queue.Queue[str] = queue.Queue(maxsize=1000)
    reader = GetEventReader(dev, line_q)
    reader.start()

    detector = ClickDetector()

    print("[RUN] 开始监听（Ctrl+C 结束）…")
    # 启动前先做一次预采集
    pre_snap: PreClickSnapshot | None = capture_pre_click_snapshot()
    try:
        while True:
            try:
                line = line_q.get(timeout=1)
            except queue.Empty:
                continue
            if VERBOSE:
                debug(f"RAW {line}")
            clicked, xy, _down_started = detector.parse_line(line)
            if clicked and xy is not None:
                # 捕获到一次点击（在抬起），使用之前的预采集
                processed = False
                try:
                    processed = handle_click_with_snapshot(xy, pre_snap)
                except Exception as e:
                    print(f"[ERROR] 采集流程异常: {e}")
                finally:
                    # 成功处理才推进序号，否则保持当前序号
                    if processed:
                        IMAGE_INDEX += 1
                    # 无论成功与否，均尝试准备下一次预采集
                    pre_snap = capture_pre_click_snapshot()
    except KeyboardInterrupt:
        print("\n[EXIT] 捕获到 Ctrl+C，正在退出…")
    finally:
        reader.stop()


if __name__ == "__main__":
    main()
