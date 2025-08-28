import os
import json
import logging
import tempfile
import requests
from typing import Optional, Dict, Any
from PIL import Image

"""
上传器：读取提取 JSON（由 extract_actions.py 产出），执行去重查询并上传到 Directus。

默认输入：extracted_actions_{upload_version}.json（可通过环境变量或命令行覆盖）。
"""

# --- Directus 配置（保持与原脚本一致） ---
DIRECTUS_URL = "http://114.132.210.199:8055"
DIRECTUS_TOKEN = "VvA5umrNXlV6iPSsvFxZ6tcV6W0_iDXN"
PAGES_TABLE = "Pages"
CONTROLS_TABLE = "Controls"
ACTIONS_TABLE = "Action"
APP_TABLE = "App"

# --- 路径与日志 ---
base_dir = os.path.dirname(os.path.abspath(__file__))
UPLOAD_LOG_FILE = os.path.join(base_dir, 'upload_errors.log')
UPLOAD_SUMMARY_FILE = os.path.join(base_dir, 'upload_summary.json')
JSON_PATH = os.path.join(base_dir, f'extracted_actions_collected_8.27.json')


def get_auth_headers():
    if not DIRECTUS_TOKEN:
        raise ValueError("错误: 请设置好您的 Directus 静态访问令牌 (DIRECTUS_TOKEN)。")
    return {"Authorization": f"Bearer {DIRECTUS_TOKEN}"}


def upload_file(image_path: str) -> Optional[str]:
    """上传文件到 Directus 并返回 file id。"""
    if not image_path or not os.path.exists(image_path):
        logging.error(f"错误: 尝试上传但文件不存在: {image_path}")
        return None

    files = {'file': (os.path.basename(image_path), open(image_path, 'rb'), 'image/png')}
    try:
        headers = get_auth_headers()
        response = requests.post(f"{DIRECTUS_URL}/files", files=files, headers=headers)
        response.raise_for_status()
        return response.json()['data']['id']
    except (requests.exceptions.RequestException, ValueError, KeyError) as e:
        logging.error(f"上传文件失败 {image_path}: {e}")
        return None


def get_or_create_app(app_name: str, app_version: str, app_platform: str, app_package: str) -> Optional[str]:
    headers = get_auth_headers()
    params = {
        'filter[app_name][_eq]': app_name,
        'filter[app_version][_eq]': app_version,
        'filter[app_platform][_eq]': app_platform,
        'filter[app_package][_eq]': app_package,
        'limit': 1
    }
    try:
        resp = requests.get(f"{DIRECTUS_URL}/items/{APP_TABLE}", params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()['data']
        if data:
            return data[0]['id']
    except (requests.exceptions.RequestException, ValueError) as e:
        logging.error(f"查询 App 失败: {e}")
        return None

    payload = {
        'app_name': app_name,
        'app_version': app_version,
        'app_platform': app_platform,
        'app_package': app_package,
    }
    try:
        resp = requests.post(f"{DIRECTUS_URL}/items/{APP_TABLE}", json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()['data']['id']
    except (requests.exceptions.RequestException, ValueError) as e:
        logging.error(f"创建 App 失败: {e}")
        return None


def get_page_by_hash_and_activity(app_id: str, image_hash: str, activity_name: str) -> Optional[str]:
    params = {
        'filter[app_id][_eq]': app_id,
        'filter[image_hash][_eq]': image_hash,
        'filter[activity_name][_eq]': activity_name,
        'fields': 'id',
        'limit': 1,
    }
    try:
        headers = get_auth_headers()
        resp = requests.get(f"{DIRECTUS_URL}/items/{PAGES_TABLE}", params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()['data']
        return data[0]['id'] if data else None
    except (requests.exceptions.RequestException, ValueError) as e:
        logging.error(f"查询页面失败: {e}")
        return None


def create_page(app_id: str, activity_name: str, image_path: str, image_hash: str) -> Optional[str]:
    screenshot_id = upload_file(image_path)
    if not screenshot_id:
        return None
    payload = {
        'app_id': app_id,
        'activity_name': activity_name,
        'image_hash': image_hash,
        'screen_shot': screenshot_id,
    }
    try:
        headers = get_auth_headers()
        resp = requests.post(f"{DIRECTUS_URL}/items/{PAGES_TABLE}", json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()['data']['id']
    except (requests.exceptions.RequestException, ValueError) as e:
        logging.error(f"创建页面失败: {e}")
        return None


def get_or_create_page(app_id: str, activity: str, image_path: str, image_hash: str, page_cache: Dict[str, str]) -> Optional[str]:
    key = f"{app_id}--{image_hash}-{activity}"
    if key in page_cache:
        return page_cache[key]
    page_id = get_page_by_hash_and_activity(app_id, image_hash, activity)
    if page_id:
        page_cache[key] = page_id
        return page_id
    page_id = create_page(app_id, activity, image_path, image_hash)
    if page_id:
        page_cache[key] = page_id
    return page_id


def get_control_by_attributes(page_id: str, control_image_hash: str, bounds: dict) -> Optional[str]:
    params = {
        'filter[control_image_hash][_eq]': control_image_hash,
        'filter[Page_id][_eq]': page_id,
        'fields': 'id,bound',
    }
    try:
        headers = get_auth_headers()
        resp = requests.get(f"{DIRECTUS_URL}/items/{CONTROLS_TABLE}", params=params, headers=headers)
        resp.raise_for_status()
        candidates = resp.json()['data']
        for cand in candidates:
            if cand.get('bound') == bounds:
                return cand['id']
        return None
    except (requests.exceptions.RequestException, ValueError) as e:
        logging.error(f"查询控件失败: {e}")
        return None


def create_control(app_id: str, page_id: str, bound: dict, text: str, resource_id: str, classtag: str, control_image_hash: str, control_image_path: str) -> Optional[str]:
    control_image = upload_file(control_image_path)
    if not control_image:
        logging.error(f"控件截图上传失败，无法创建控件记录。路径: {control_image_path}")
        return None
    payload = {
        'app_id': app_id,
        'Page_id': page_id,
        'bound': bound,
        'text': text,
        'resource_id': resource_id,
        'classtag': classtag,
        'control_image_hash': control_image_hash,
        'control_image': control_image,
    }
    try:
        headers = get_auth_headers()
        resp = requests.post(f"{DIRECTUS_URL}/items/{CONTROLS_TABLE}", json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()['data']['id']
    except requests.exceptions.HTTPError as e:
        error_details = e.response.json() if e.response else str(e)
        logging.error(f"创建控件失败: {e} - 详细信息: {error_details}, 控件信息: {payload}")
        return None
    except (requests.exceptions.RequestException, ValueError) as e:
        logging.error(f"创建控件失败: {e}, 控件信息: {payload}")
        return None

def check_action_exists(
    source_page_id: str,
    destination_page_id: str,
    control_id: str,
    operation: Optional[str] = None,
    extras: Optional[Dict[str, Any]] = None,
) -> bool:
    params = {
        'filter[source_page_id][_eq]': source_page_id,
        'filter[destination_page_id][_eq]': destination_page_id,
        'filter[control_id][_eq]': control_id,
        'limit': 1,
    }
    if operation is not None:
        params['filter[operation][_eq]'] = operation
    extras = extras or {}
    op = (operation or '').lower()
    if op == 'long-click':
        if 'duration' in extras and extras['duration'] is not None:
            params['filter[duration][_eq]'] = extras['duration']
    elif op == 'swipe':
        if 'swipe_direction' in extras and extras['swipe_direction'] is not None:
            params['filter[swipe_direction][_eq]'] = extras['swipe_direction']
        if 'swipe_distance' in extras and extras['swipe_distance'] is not None:
            params['filter[swipe_distance][_eq]'] = extras['swipe_distance']
        if 'duration' in extras and extras['duration'] is not None:
            params['filter[duration][_eq]'] = extras['duration']
    elif op == 'input':
        if 'input_text' in extras and extras['input_text'] is not None:
            params['filter[input_text][_eq]'] = extras['input_text']
    try:
        headers = get_auth_headers()
        resp = requests.get(f"{DIRECTUS_URL}/items/{ACTIONS_TABLE}", params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()['data']
        return len(data) > 0
    except (requests.exceptions.RequestException, ValueError) as e:
        logging.error(f"查询操作失败: {e}")
        return False


def create_action(
    source_page_id: str,
    destination_page_id: str,
    control_id: str,
    screenshot_with_box_path: str,
    control_operation: str,
    upload_version: str,
    is_activity_jumped: bool = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    screenshot_id = upload_file(screenshot_with_box_path)
    if not screenshot_id:
        return None
    payload = {
        'source_page_id': source_page_id,
        'destination_page_id': destination_page_id,
        'control_id': control_id,
        'control_screenshot_with_box': screenshot_id,
        'operation': control_operation,
        'upload_version': upload_version,
        'is_activity_jumped': is_activity_jumped,
    }
    extra = extra or {}
    op = (control_operation or '').lower()
    if op == 'long-click':
        if 'duration' in extra and extra['duration'] is not None:
            payload['duration'] = extra['duration']
    elif op == 'swipe':
        if 'swipe_direction' in extra and extra['swipe_direction'] is not None:
            payload['swipe_direction'] = extra['swipe_direction']
        if 'swipe_distance' in extra and extra['swipe_distance'] is not None:
            payload['swipe_distance'] = extra['swipe_distance']
        if 'duration' in extra and extra['duration'] is not None:
            payload['duration'] = extra['duration']
    elif op == 'input':
        if 'input_text' in extra and extra['input_text'] is not None:
            payload['input_text'] = extra['input_text']
    try:
        headers = get_auth_headers()
        resp = requests.post(f"{DIRECTUS_URL}/items/{ACTIONS_TABLE}", json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()['data']['id']
    except (requests.exceptions.RequestException, ValueError) as e:
        logging.error(f"创建操作失败: {e}")
        return None


def get_total_item_count(table_name: str) -> int:
    params = {'aggregate[count]': '*'}
    try:
        headers = get_auth_headers()
        resp = requests.get(f"{DIRECTUS_URL}/items/{table_name}", params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()['data']
        return int(data[0]['count'])
    except (requests.exceptions.RequestException, ValueError, KeyError, IndexError) as e:
        logging.error(f"查询 {table_name} 总数失败: {e}")
        return -1


def main(json_path: Optional[str] = None):
    # 日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        filename=UPLOAD_LOG_FILE,
        filemode='w',
        encoding='utf-8',
    )
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(formatter)
    logging.getLogger().addHandler(console_handler)

    # 读取 JSON
    if not json_path:
        # 支持环境变量覆盖
        json_path = os.environ.get('EXTRACTED_JSON_PATH')
    if not json_path:
        # 默认文件名匹配提取器
        default_version = '8.20_2'
        json_path = os.path.join(base_dir, f'extracted_actions_{default_version}.json')

    if not os.path.exists(json_path):
        logging.error(f"找不到提取结果 JSON: {json_path}")
        return

    with open(json_path, 'r', encoding='utf-8') as f:
        payload = json.load(f)

    app_info = payload.get('app', {})
    items = payload.get('items', [])
    upload_version = payload.get('upload_version') or 'unknown'

    # 运行前统计
    db_counts_before = {
        'apps': get_total_item_count(APP_TABLE),
        'pages': get_total_item_count(PAGES_TABLE),
        'controls': get_total_item_count(CONTROLS_TABLE),
        'actions': get_total_item_count(ACTIONS_TABLE),
    }
    logging.info("--- 运行前数据库状态 ---")
    logging.info(f"总应用数: {db_counts_before['apps']}")
    logging.info(f"总页面数: {db_counts_before['pages']}")
    logging.info(f"总控件数: {db_counts_before['controls']}")
    logging.info(f"总操作数: {db_counts_before['actions']}")

    # 获取/创建 App
    app_id = get_or_create_app(
        app_info.get('name'),
        app_info.get('version'),
        app_info.get('platform'),
        app_info.get('package'),
    )
    if not app_id:
        logging.error("无法获取或创建 App 记录，程序终止。")
        return

    page_cache: Dict[str, str] = {}
    summary = {
        'created': {'pages': 0, 'controls': 0, 'actions': 0},
        'skipped': {'pages': 0, 'controls': 0, 'actions': 0, 'elements': 0},
        'errors': 0,
    }

    for item in items:
        # current_index = item.get('current_index')
        src = item.get('source', {})
        dst = item.get('destination', {})
        ctl = item.get('control', {})

        # 源页面
        is_source_new = get_page_by_hash_and_activity(app_id, src.get('image_hash'), src.get('activity')) is None
        source_page_id = get_or_create_page(app_id, src.get('activity'), src.get('image_path'), src.get('image_hash'), page_cache)
        if not source_page_id:
            summary['errors'] += 1
            logging.warning("无法获取或创建源页面，跳过该元素。")
            continue
        summary['created']['pages'] += 1 if is_source_new else 0
        summary['skipped']['pages'] += 0 if is_source_new else 1
        if not is_source_new:
            logging.info(
                f"SKIP-REASON page existing (source): activity={src.get('activity')}, "
                f"hash={str(src.get('image_hash'))[:16]}..., image={os.path.basename(src.get('image_path') or '')}"
            )

        # 目标页面
        is_dest_new = get_page_by_hash_and_activity(app_id, dst.get('image_hash'), dst.get('activity')) is None
        destination_page_id = get_or_create_page(app_id, dst.get('activity'), dst.get('image_path'), dst.get('image_hash'), page_cache)
        if not destination_page_id:
            summary['errors'] += 1
            logging.warning("无法获取或创建目标页面，跳过该元素。")
            continue
        summary['created']['pages'] += 1 if is_dest_new else 0
        summary['skipped']['pages'] += 0 if is_dest_new else 1
        if not is_dest_new:
            logging.info(
                f"SKIP-REASON page existing (destination): activity={dst.get('activity')}, "
                f"hash={str(dst.get('image_hash'))[:16]}..., image={os.path.basename(dst.get('image_path') or '')}"
            )

        bounds = ctl.get('bounds')
        control_id = get_control_by_attributes(source_page_id, ctl.get('control_image_hash'), bounds)
        if control_id:
            summary['skipped']['controls'] += 1
            logging.info(
                f"SKIP-REASON control existing: source_page_id={source_page_id[:8]}..., "
                f"hash={str(ctl.get('control_image_hash'))[:16]}..., bounds={bounds}"
            )
        else:
            # 按 bounds 裁剪源图片，生成临时控件截图
            tmp_path = None
            try:
                with Image.open(src.get('image_path')) as img:
                    w, h = img.size
                    x1 = int(bounds['x1'] * w)
                    y1 = int(bounds['y1'] * h)
                    x2 = int(bounds['x2'] * w)
                    y2 = int(bounds['y2'] * h)
                    cropped = img.crop((x1, y1, x2, y2))
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as temp_f:
                        cropped.save(temp_f, format='PNG')
                        tmp_path = temp_f.name
            except Exception as e:
                logging.error(f"控件截图裁剪失败: {e}")

            control_id = create_control(
                app_id,
                source_page_id,
                bounds,
                ctl.get('text', ''),
                ctl.get('resource_id', ''),
                ctl.get('classtag', ''),
                ctl.get('control_image_hash'),
                tmp_path or src.get('image_path'),
            )
            if control_id:
                summary['created']['controls'] += 1
            else:
                summary['errors'] += 1
                logging.warning("创建控件失败，跳过后续操作创建。")
                continue
            # 清理临时文件
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

        # 构建 Action 的 operation 及所需字段
        op = (ctl.get('operation') or 'short-click').lower()
        extras: Dict[str, Any] = {}
        if op == 'long-click':
            duration = ctl.get('duration')
            if duration is None:
                logging.info("SKIP-REASON action missing field: duration for long-click")
                summary['skipped']['actions'] += 1
                continue
            extras['duration'] = duration
        elif op == 'swipe':
            swipe_direction = ctl.get('swipe_direction')
            swipe_distance = ctl.get('swipe_distance')
            duration = ctl.get('duration')
            missing = [k for k, v in {'swipe_direction': swipe_direction, 'swipe_distance': swipe_distance, 'duration': duration}.items() if v is None]
            if missing:
                logging.info(f"SKIP-REASON action missing fields for swipe: {','.join(missing)}")
                summary['skipped']['actions'] += 1
                continue
            extras.update({'swipe_direction': swipe_direction, 'swipe_distance': swipe_distance, 'duration': duration})
        elif op == 'input':
            input_text = ctl.get('input_text')
            if input_text is None:
                logging.info("SKIP-REASON action missing field: input_text for input")
                summary['skipped']['actions'] += 1
                continue
            extras['input_text'] = input_text

        if check_action_exists(source_page_id, destination_page_id, control_id, op, extras):
            summary['skipped']['actions'] += 1
            logging.info(
                f"SKIP-REASON action existing: src={source_page_id[:8]}..., ctl={control_id[:8]}..., dst={destination_page_id[:8]}" \
                f", op={op}, extras={extras}"
            )
            continue

        action_id = create_action(
            source_page_id,
            destination_page_id,
            control_id,
            ctl.get('screenshot_with_box_path'),
            op,
            upload_version,
            bool(item.get('is_activity_jumped')),
            extras,
        )
        if action_id:
            summary['created']['actions'] += 1
        else:
            summary['errors'] += 1

    db_counts_after = {
        'pages': get_total_item_count(PAGES_TABLE),
        'controls': get_total_item_count(CONTROLS_TABLE),
        'actions': get_total_item_count(ACTIONS_TABLE),
    }

    logging.info("\n=== 上传完成 ===")
    logging.info(f"创建: pages={summary['created']['pages']}, controls={summary['created']['controls']}, actions={summary['created']['actions']}")
    logging.info(f"跳过: pages={summary['skipped']['pages']}, controls={summary['skipped']['controls']}, actions={summary['skipped']['actions']}")
    logging.info(f"错误: {summary['errors']}")
    logging.info(f"总页面数: {db_counts_after['pages']}")   
    logging.info(f"总控件数: {db_counts_after['controls']}")
    logging.info(f"总操作数: {db_counts_after['actions']}")

    try:
        # 将最终统计写入 JSON
        summary['db_counts_before'] = db_counts_before
        summary['db_counts_after'] = db_counts_after
        with open(UPLOAD_SUMMARY_FILE, 'w', encoding='utf-8') as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        logging.info(f"上传总结已写入: {UPLOAD_SUMMARY_FILE}")
    except Exception as e:
        logging.error(f"保存上传总结失败: {e}")


if __name__ == '__main__':
    # 允许传递命令行参数指定 JSON 路径
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else JSON_PATH
    main(path)
