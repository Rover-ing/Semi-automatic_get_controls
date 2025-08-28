import os
import re
import json
import yaml
import logging
import hashlib
import tempfile
from PIL import Image

"""
提取器：读取 elements.yml，按照现有清洗逻辑产出一个标准 JSON，供上传器使用。
不会进行任何网络请求。

输出文件：extracted_actions_{upload_version}.json
结构包括：
- app 基本信息
- 运行参数（屏幕尺寸、blacklist 高度、上传版本）
- items 列表（每个可上传动作的数据包）
"""

# --- 运行参数与常量（与原脚本保持一致） ---
SCREEN_WIDTH = 1080
SCREEN_HEIGHT = 2340
BLACKLIST_HEIGHT = 100  # 页面哈希时忽略顶部像素高度
ELEMENTS_ENCODING = 'gbk'
upload_version = '8.18'

# --- 路径配置 ---
base_dir = os.path.dirname(os.path.abspath(__file__))
ELEMENTS_FILE_PATH = os.path.join(base_dir, 'AppCrawlerResult（8.18）', 'elements.yml')
IMAGE_DIR = os.path.join(base_dir, 'AppCrawlerResult（8.18）')
EXTRACT_LOG_FILE = os.path.join(base_dir, 'extract_errors.log')
EXTRACT_SUMMARY_FILE = os.path.join(base_dir, 'extract_summary.json')
EXTRACTED_JSON_PATH = os.path.join(base_dir, f'extracted_actions_{upload_version}.json')

# App 版本信息配置（仅用于元信息记录）
APP_NAME = "腾讯元宝"
APP_VERSION = "V2.34.0.83"
APP_PACKAGE = "com.tencent.hunyuan.app.chat"
APP_PLATFORM = "Android"


# --- 图像处理工具函数---
def generate_image_hash(image_path: str, blacklist_height: int = BLACKLIST_HEIGHT):
    """生成页面图片哈希（忽略顶部 blacklist 区域）。"""
    try:
        with Image.open(image_path) as img:
            width, height = img.size
            img_copy = img.copy()
            if blacklist_height > 0 and height > blacklist_height:
                black_img = Image.new('RGB', (width, blacklist_height), 'black')
                img_copy.paste(black_img, (0, 0))
            pixel_data = img_copy.tobytes()
            hash_object = hashlib.sha256(pixel_data)
            return str(hash_object.hexdigest())
    except FileNotFoundError:
        logging.error(f"错误: 图片文件未找到 {image_path}")
        return None
    except Exception as e:
        logging.error(f"无法处理图片 {image_path}: {e}")
        return None


def compute_cropped_control_hash(image_path: str, bounds: dict):
    """根据相对坐标裁剪图片并返回控件区域哈希（不落盘）。"""
    try:
        with Image.open(image_path) as img:
            width, height = img.size
            x1 = int(bounds['x1'] * width)
            y1 = int(bounds['y1'] * height)
            x2 = int(bounds['x2'] * width)
            y2 = int(bounds['y2'] * height)
            cropped_img = img.crop((x1, y1, x2, y2))
            pixel_data = cropped_img.tobytes()
            return hashlib.sha256(pixel_data).hexdigest()
    except FileNotFoundError:
        logging.error(f"错误: 图片文件未找到 {image_path}")
        return None
    except Exception as e:
        logging.error(f"无法处理或裁剪图片 {image_path}: {e}")
        return None


def find_image_by_index_and_suffix(image_dir: str, index: int, suffix: str):
    """在目录中按索引+后缀查找图片。"""
    search_prefix = f"{index}_"
    for filename in os.listdir(image_dir):
        if filename.startswith(search_prefix) and filename.endswith(suffix):
            return os.path.join(image_dir, filename), None
    return None, f"未找到以 '{search_prefix}' 开头并以 '{suffix}' 结尾的图片。"


def build_url_lookup_map(element_store_map: dict):
    """从 elements.yml 中构建 index -> activity 的 URL 快速查找表。"""
    url_map = {}
    for key, value in element_store_map.items():
        element = value.get('element', {})
        url = element.get('url')
        img_name_raw = os.path.basename(value.get('resImg', ''))
        match = re.match(r'(\d+)_', img_name_raw)
        if match and url:
            index = int(match.group(1))
            url_map[index] = url
    return url_map


def main():
    # 日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        filename=EXTRACT_LOG_FILE,
        filemode='w',
        encoding='utf-8',
    )
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(formatter)
    logging.getLogger().addHandler(console_handler)

    if not os.path.exists(ELEMENTS_FILE_PATH):
        logging.error(f"错误: elements.yml 文件未找到 at {ELEMENTS_FILE_PATH}")
        return

    with open(ELEMENTS_FILE_PATH, 'r', encoding=ELEMENTS_ENCODING) as f:
        data = yaml.safe_load(f)

    element_store_map = data.get('elementStoreMap', {})
    if not element_store_map:
        logging.info("在 elements.yml 中未找到 elementStoreMap 或其为空。")
        return

    logging.info("正在预处理 elements.yml 以构建 URL 查找表...")
    url_lookup = build_url_lookup_map(element_store_map)
    logging.info("URL 查找表构建完成。")

    summary = {
        'prepared': 0,
        'skipped_elements': 0,
        'errors': 0,
    }

    items = []

    for key, value in element_store_map.items():
        element = value.get('element', {})
        action = value.get('action', '')

        if action in ('SKIPPED', 'READY'):
            logging.info(f"跳过元素 (标记为 {action}): {key}")
            summary['skipped_elements'] += 1
            continue

        logging.info(f"\n--- 正在处理元素: {key} ---")

        # 从 resImg 中提取当前索引
        res_img_name_raw = os.path.basename(value.get('resImg', '').replace('\\\\', '\\'))
        match = re.match(r'(\d+)_', res_img_name_raw)
        if not match:
            logging.error(f"警告: 无法从目标图片名 '{res_img_name_raw}' 中解析出数字索引。跳过此元素。")
            summary['errors'] += 1
            continue
        current_index = int(match.group(1))

        # 目标页面（Destination）
        dest_img_suffix = ".clicked.png"
        dest_img_path, error_msg = find_image_by_index_and_suffix(IMAGE_DIR, current_index, dest_img_suffix)
        if error_msg:
            logging.error(f"错误: {error_msg} 跳过此元素。")
            summary['errors'] += 1
            continue
        dest_image_hash = generate_image_hash(dest_img_path)
        if not dest_image_hash:
            logging.error(f"错误: 无法为目标图片生成哈希值: {os.path.basename(dest_img_path)}。跳过此元素。")
            summary['errors'] += 1
            continue

        # 源页面（Source）
        req_img_suffix = ".click.png"  # 操作时带框截图
        req_img_full_path, error_msg = find_image_by_index_and_suffix(IMAGE_DIR, current_index, req_img_suffix)
        if error_msg:
            logging.warning(f"警告: 未找到操作带框截图 (.click.png)，跳过此元素。原因: {error_msg}")
            summary['skipped_elements'] += 1
            continue
        if current_index == 0:
            logging.warning("警告: 当前图片索引为 0，没有源页面。跳过。")
            summary['skipped_elements'] += 1
            continue
        source_index = current_index - 1
        source_img_suffix = ".clicked.png"
        source_img_full_path, error_msg = find_image_by_index_and_suffix(IMAGE_DIR, source_index, source_img_suffix)
        if error_msg:
            logging.warning(f"警告: {error_msg} 无法构建源页面。")
            summary['skipped_elements'] += 1
            continue
        source_image_hash = generate_image_hash(source_img_full_path)
        if not source_image_hash:
            logging.warning(f"警告: 无法为源图片 '{os.path.basename(source_img_full_path)}' 生成哈希值。")
            summary['skipped_elements'] += 1
            continue

        source_activity_name = url_lookup.get(current_index, 'Unknownactivity')
        destination_activity_name = url_lookup.get(current_index + 1, 'Unknownactivity')
        if destination_activity_name == 'Unknownactivity':
            if dest_image_hash == source_image_hash:
                destination_activity_name = source_activity_name
            else:
                logging.error("警告: destination activity 无法查找且点击后页面发生变化，跳过此元素。")
                summary['skipped_elements'] += 1
                continue

        # 是否发生了 activity 跳转（来源与目标不同即为跳转）
        is_activity_jumped = (source_activity_name != destination_activity_name)

        # 控件信息
        x, y = element.get('x', 0), element.get('y', 0)
        width, height = element.get('width', 0), element.get('height', 0)
        bounds = {
            "x1": x / SCREEN_WIDTH,
            "y1": y / SCREEN_HEIGHT,
            "x2": (x + width) / SCREEN_WIDTH,
            "y2": (y + height) / SCREEN_HEIGHT,
        }
        text = element.get('text', '')
        resource_id = element.get('id', '')
        classtag = element.get('className', '')
        control_operation = 'short-click'

        control_image_hash = compute_cropped_control_hash(source_img_full_path, bounds)
        if not control_image_hash:
            logging.error(f"错误: 无法为控件生成哈希值 (element: {key})。跳过此元素。")
            summary['errors'] += 1
            continue

        items.append({
            'current_index': current_index,
            'is_activity_jumped': is_activity_jumped,
            'source': {
                'activity': source_activity_name,
                'image_path': source_img_full_path,
                'image_hash': source_image_hash,
            },
            'destination': {
                'activity': destination_activity_name,
                'image_path': dest_img_path,
                'image_hash': dest_image_hash,
            },
            'control': {
                'bounds': bounds,
                'text': text,
                'resource_id': resource_id,
                'classtag': classtag,
                'operation': control_operation,
                'control_image_hash': control_image_hash,
                'screenshot_with_box_path': req_img_full_path,
            },
        })
        summary['prepared'] += 1

    payload = {
        'app': {
            'name': APP_NAME,
            'version': APP_VERSION,
            'platform': APP_PLATFORM,
            'package': APP_PACKAGE,
        },
        'screen': {
            'width': SCREEN_WIDTH,
            'height': SCREEN_HEIGHT,
            'blacklist_height': BLACKLIST_HEIGHT,
        },
        'upload_version': upload_version,
        'image_dir': IMAGE_DIR,
        'items': items,
    }

    # 写入 JSON
    try:
        with open(EXTRACTED_JSON_PATH, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        with open(EXTRACT_SUMMARY_FILE, 'w', encoding='utf-8') as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        logging.info(f"提取完成：已生成 {EXTRACTED_JSON_PATH}，共 {summary['prepared']} 条；跳过 {summary['skipped_elements']}；错误 {summary['errors']}")
    except Exception as e:
        logging.error(f"写出提取结果失败: {e}")


if __name__ == '__main__':
    main()
