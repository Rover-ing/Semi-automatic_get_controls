import os
import re
import json
import logging
import hashlib
from typing import Dict, Any, List, Optional, Tuple
from PIL import Image

"""
采集数据清洗器：读取 UI_Automated_acquisition1/collected_data.json，
清洗为 upload_actions.py 可直接上传的标准 JSON：extracted_actions_{upload_version}.json。

顶层结构包含：
- app: 基础应用信息（可在下方配置）
- screen: 屏幕宽高与黑名单高度（用于 page_hash 计算忽略顶部像素）
- upload_version: 本次清洗/上传版本号
- image_dir: 图片存放目录（可自动探测，也可覆盖）
- items: 每条动作的数据包

items 结构：
{
  "current_index": int,  # 由 elem_id 解析出的序号
  "is_activity_jumped": bool,
  "source": {"activity": str, "image_path": str, "image_hash": str},
  "destination": {"activity": str, "image_path": str, "image_hash": str},
  "control": {
    "bounds": {x1,y1,x2,y2},   # 相对坐标(0~1)
    "text": str,
    "resource_id": str,
    "classtag": str,
    "operation": str,          # short-click / long-click / input / swipe ...
    "control_image_hash": str, # 对 raw 图片按 bounds 裁剪后 hash
    "screenshot_with_box_path": str, # 带框截图 boxed
    # 以下为可选补充字段，按 operation 类型写入：
    # long-click: duration
    # swipe: duration, direction, swipe_distance
    # input: input_text
  }
}
"""

# ===== 基础配置（可按需修改） =====
SCREEN_WIDTH = 1080
SCREEN_HEIGHT = 2400  # 若无法探测原图尺寸，将退回该默认值
BLACKLIST_HEIGHT = 100
UPLOAD_VERSION = "collected_8.27"

# App 元信息（用于记录）
APP_NAME = "腾讯元宝"
APP_VERSION = "V2.36.10.41"
APP_PACKAGE = "com.tencent.hunyuan.app.chat"
APP_PLATFORM = "Android"

# 路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
DEFAULT_COLLECTED_JSON = os.path.join(
    ROOT_DIR, "adbgetevent", "UI_Automated_acquisition", "collected_data.json"
)
EXTRACT_LOG_FILE = os.path.join(BASE_DIR, "extract_errors.log")
EXTRACT_SUMMARY_FILE = os.path.join(BASE_DIR, "extract_summary.json")
OUTPUT_JSON_PATH = os.path.join(BASE_DIR, f"extracted_actions_{UPLOAD_VERSION}.json")


# ===== 工具函数 =====
def ensure_file(path: Optional[str]) -> Optional[str]:
    if path and os.path.exists(path):
        return path
    return None


def parse_bounds_to_relative_with_size(bounds_str: str, img_w: int, img_h: int) -> Optional[Dict[str, float]]:
    """将 "[x1,y1][x2,y2]" 解析为相对坐标字典，按传入原图尺寸归一化。"""
    try:
        nums = list(map(int, re.findall(r"\d+", bounds_str)))
        if len(nums) != 4 or img_w <= 0 or img_h <= 0:
            return None
        x1, y1, x2, y2 = nums
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(img_w, x2), min(img_h, y2)
        if x2 <= x1 or y2 <= y1:
            return None
        return {
            "x1": x1 / img_w,
            "y1": y1 / img_h,
            "x2": x2 / img_w,
            "y2": y2 / img_h,
        }
    except Exception:
        return None


def page_image_hash(image_path: str, blacklist_height: int = BLACKLIST_HEIGHT) -> Optional[str]:
    """对页面图片生成哈希（顶部 BLACKLIST 区域置黑）。"""
    path = ensure_file(image_path)
    if not path:
        return None
    try:
        with Image.open(path) as img:
            w, h = img.size
            img_copy = img.copy()
            if blacklist_height > 0 and h > blacklist_height:
                black = Image.new("RGB", (w, blacklist_height), "black")
                img_copy.paste(black, (0, 0))
            return hashlib.sha256(img_copy.tobytes()).hexdigest()
    except Exception:
        return None


def control_crop_hash(image_path: str, bounds_rel: Dict[str, float]) -> Optional[str]:
    """按相对 bounds 在原图上裁剪后进行哈希。"""
    path = ensure_file(image_path)
    if not path:
        return None
    try:
        with Image.open(path) as img:
            w, h = img.size
            x1 = int(bounds_rel["x1"] * w)
            y1 = int(bounds_rel["y1"] * h)
            x2 = int(bounds_rel["x2"] * w)
            y2 = int(bounds_rel["y2"] * h)
            if x2 <= x1 or y2 <= y1:
                return None
            crop = img.crop((x1, y1, x2, y2))
            return hashlib.sha256(crop.tobytes()).hexdigest()
    except Exception:
        return None


def infer_image_dir(items: List[Dict[str, Any]]) -> Optional[str]:
    for it in items:
        images = it.get("images") or {}
        raw = images.get("raw")
        if raw:
            return os.path.dirname(raw)
    return None


def parse_current_index(elem_id: str) -> Optional[int]:
    m = re.match(r".*?(\d+)$", str(elem_id).replace("elem_", ""))
    return int(m.group(1)) if m else None


def normalize_action(action: Optional[str]) -> str:
    """将采集到的 action 名标准化为 short-click/long-click/input/swipe/back。"""
    a = (action or "").strip().lower()
    mapping = {
        "tap": "short-click",
        "click": "short-click",
        "short_click": "short-click",
        "short-click": "short-click",
        "long_press": "long-click",
        "long-press": "long-click",
        "longclick": "long-click",
        "long-click": "long-click",
        "input": "input",
        "text": "input",
        "swipe": "swipe",
        "scroll": "swipe",
        "back": "back",
    }
    return mapping.get(a, a or "short-click")


def pick_number(src: Dict[str, Any], keys: List[str]) -> Optional[float]:
    for k in keys:
        if k in src and src[k] is not None:
            try:
                return float(src[k])
            except Exception:
                pass
    return None


def build_control_object(node: Dict[str, Any], action: str, bounds_rel: Dict[str, float],
                         raw_img: str, boxed_img: str) -> Optional[Dict[str, Any]]:
    ctrl_hash = control_crop_hash(raw_img, bounds_rel)
    if not ctrl_hash:
        return None

    control: Dict[str, Any] = {
        "bounds": bounds_rel,
        "text": node.get("text") or "",
        "resource_id": node.get("resource-id") or "",
        "classtag": node.get("class") or "",
        "operation": action or "short-click",
        "control_image_hash": ctrl_hash,
        "screenshot_with_box_path": boxed_img or "",
    }

    # 根据不同操作补充字段
    # 这些字段直接平铺在 control 内，便于 upload_actions 消费
    # 额外字段在调用处补充

    return control


def main(collected_json: Optional[str] = None,
         output_json_path: Optional[str] = None):
    # 日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        filename=EXTRACT_LOG_FILE,
        filemode='w',
        encoding='utf-8',
    )
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logging.getLogger().addHandler(console)

    src_path = collected_json or os.environ.get("COLLECTED_JSON_PATH") or DEFAULT_COLLECTED_JSON
    out_path = output_json_path or os.environ.get("EXTRACTED_JSON_PATH") or OUTPUT_JSON_PATH

    if not os.path.exists(src_path):
        logging.error(f"找不到采集数据: {src_path}")
        return

    with open(src_path, "r", encoding="utf-8") as f:
        collected: List[Dict[str, Any]] = json.load(f)

    # 自动探测 image_dir
    autod_image_dir = infer_image_dir(collected) or ""

    items: List[Dict[str, Any]] = []
    summary = {"prepared": 0, "skipped": 0, "errors": 0}

    for entry in collected:
        try:
            elem_id = entry.get("elem_id") or ""
            idx = parse_current_index(elem_id)

            node = entry.get("node") or {}
            images = entry.get("images") or {}
            action = normalize_action(entry.get("action"))

            raw = images.get("raw")
            boxed = images.get("boxed")
            dest = images.get("dest")
            if not raw or not os.path.exists(raw):
                logging.warning(f"跳过：raw 图片不存在: {raw}")
                summary["skipped"] += 1
                continue
            if not dest or not os.path.exists(dest):
                # 目标页可能失败，允许但无法计算目的页哈希，仍尝试构造（image_hash 置 None）
                logging.warning(f"目标图片缺失（允许）：{dest}")

            # 使用原图真实尺寸进行坐标归一
            try:
                with Image.open(raw) as _img_probe:
                    img_w, img_h = _img_probe.size
            except Exception:
                img_w, img_h = SCREEN_WIDTH, SCREEN_HEIGHT

            bounds_rel = parse_bounds_to_relative_with_size(node.get("bounds") or "", img_w, img_h)
            if not bounds_rel:
                logging.warning(f"跳过：bounds 无法解析或无效，elem: {elem_id}")
                summary["skipped"] += 1
                continue

            # source/destination activity
            source_activity = entry.get("source_activity") or entry.get("activity") or ""
            dest_activity = entry.get("dest_activity") or ""
            is_jump = bool(entry.get("is_activity_jumped", False))

            # 页面哈希
            src_hash = page_image_hash(raw)
            dst_hash = page_image_hash(dest) if dest else None

            control = build_control_object(node, action, bounds_rel, raw, boxed or "")
            if not control:
                logging.warning(f"跳过：控件哈希失败，elem: {elem_id}")
                summary["skipped"] += 1
                continue

            # 按操作补充额外字段
            if action == "long-click":
                dur = pick_number(entry, ["duration", "durationMs", "duration_ms"])
                if dur is not None:
                    control["duration"] = dur
            elif action == "swipe":
                # 兼容不同字段命名
                dur = pick_number(entry, ["duration"]) 
                if dur is not None:
                    control["duration"] = dur

                swipe_direction = entry.get("swipe_direction") or entry.get("dir")
                dx = pick_number(entry, ["dx"]) or 0.0
                dy = pick_number(entry, ["dy"]) or 0.0

                # 先取显式距离
                dist = pick_number(entry, ["swipe_distance", "distance", "dist"]) 
                # 若未提供，尝试由 dx/dy 计算
                if dist is None and (dx or dy):
                    try:
                        import math
                        dist = float(math.hypot(dx, dy))
                    except Exception:
                        dist = None

                # 若未提供方向，尝试由 dx/dy 推断主方向
                if not swipe_direction and (dx or dy):
                    if abs(dx) >= abs(dy):
                        swipe_direction = "right" if dx > 0 else "left"
                    else:
                        swipe_direction = "down" if dy > 0 else "up"

                if swipe_direction:
                    control["swipe_direction"] = swipe_direction
                if dist is not None:
                    control["swipe_distance"] = dist
                # 同时保留 dx/dy 以便回溯
                if dx:
                    control["dx"] = dx
                if dy:
                    control["dy"] = dy

            elif action == "input":
                # 优先使用 input_text，其次 text/input/inputText
                input_text = (
                    entry.get("input_text")
                    or entry.get("inputText")
                    or entry.get("input")
                    or (entry.get("text") if isinstance(entry.get("text"), str) else None)
                )
                if input_text is not None:
                    control["input_text"] = input_text

            item: Dict[str, Any] = {
                "current_index": idx if idx is not None else None,
                "is_activity_jumped": is_jump,
                "source": {
                    "activity": source_activity,
                    "image_path": raw,
                    "image_hash": src_hash,
                },
                "destination": {
                    "activity": dest_activity,
                    "image_path": dest or "",
                    "image_hash": dst_hash,
                },
                "control": control,
            }

            items.append(item)
            summary["prepared"] += 1
        except Exception as e:
            logging.exception(f"处理条目失败，已跳过。elem_id={entry.get('elem_id')}")
            summary["errors"] += 1

    payload = {
        "app": {
            "name": APP_NAME,
            "version": APP_VERSION,
            "platform": APP_PLATFORM,
            "package": APP_PACKAGE,
        },
        "screen": {
            "width": SCREEN_WIDTH,
            "height": SCREEN_HEIGHT,
            "blacklist_height": BLACKLIST_HEIGHT,
        },
        "upload_version": UPLOAD_VERSION,
    "image_dir": autod_image_dir or os.path.join(ROOT_DIR, "adbgetevent", "UI_Automated_acquisition", "image"),
        "items": items,
    }

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        with open(EXTRACT_SUMMARY_FILE, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        logging.info(
            f"清洗完成：{out_path} 生成成功；prepared={summary['prepared']} skipped={summary['skipped']} errors={summary['errors']}"
        )
    except Exception as e:
        logging.error(f"写出结果失败: {e}")


if __name__ == "__main__":
    main()
