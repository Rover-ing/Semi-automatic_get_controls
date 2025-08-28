import os
import re
import json
import yaml
import requests
from PIL import Image
import hashlib
import logging
import tempfile

# --- 配置 ---
DIRECTUS_URL = "http://114.132.210.199:8055"
DIRECTUS_TOKEN = "VvA5umrNXlV6iPSsvFxZ6tcV6W0_iDXN"
PAGES_TABLE = "Pages"
CONTROLS_TABLE = "Controls"
ACTIONS_TABLE = "Action"
APP_TABLE = "App"
SCREEN_WIDTH = 1080
SCREEN_HEIGHT = 2340
BLACKLIST_HEIGHT = 100 # 生成页面哈希时，顶部忽略的高度
ELEMENTS_ENCODING = 'utf-8'
upload_version = '8.20_2'


# --- 文件路径配置 ---
base_dir = os.path.dirname(os.path.abspath(__file__))
ELEMENTS_FILE_PATH = os.path.join(base_dir, 'AppCrawlerResult（8.20_2）', 'elements.yml')
IMAGE_DIR = os.path.join(base_dir, 'AppCrawlerResult（8.20_2）')
LOG_FILE_PATH = os.path.join(base_dir, 'upload_errors.log')
SUMMARY_FILE_PATH = os.path.join(base_dir, 'upload_summary.json')
URL_LOOKUP_FILE_PATH = os.path.join(base_dir, 'url_lookup_dump.json')

# App版本信息配置
APP_NAME = "腾讯元宝"
APP_VERSION = "V2.34.0.83"
APP_PACKAGE = "com.tencent.hunyuan.app.chat"
APP_PLATFORM = "Android"

def get_auth_headers():
    """返回包含认证令牌的请求头。"""
    if not DIRECTUS_TOKEN:
        raise ValueError("错误: 请设置好您的Directus静态访问令牌 (DIRECTUS_TOKEN)。")
    return {'Authorization': f'Bearer {DIRECTUS_TOKEN}'}


def generate_image_hash(image_path, blacklist_height=BLACKLIST_HEIGHT):
    """
    hash生成函数
    生成图片的phash值，并忽略顶部区域。
    """

    try:
        with Image.open(image_path) as img:
            width, height = img.size
            img_copy = img.copy()
            if blacklist_height > 0 and height > blacklist_height:
                black_img = Image.new('RGB', (width, blacklist_height), 'black')
                img_copy.paste(black_img, (0, 0))
            pixel_data = img_copy.tobytes()
            hash_object = hashlib.sha256(pixel_data)
            hash_value = hash_object.hexdigest()
            return str(hash_value)
    except FileNotFoundError:
        logging.error(f"错误: 图片文件未找到 {image_path}")
        return None
    except Exception as e:
        logging.error(f"无法处理图片 {image_path}: {e}")
        return None

def generate_cropped_image_hash(image_path, bounds):
    """
    控件裁取函数
    根据相对坐标裁剪图片，并生成裁剪区域的哈希值。
    """
    try:
        with Image.open(image_path) as img:
            width, height = img.size
            # 将相对坐标转换为绝对像素坐标
            x1 = int(bounds['x1'] * width)
            y1 = int(bounds['y1'] * height)
            x2 = int(bounds['x2'] * width)
            y2 = int(bounds['y2'] * height)

            # 裁剪图片
            cropped_img = img.crop((x1, y1, x2, y2))

            # 从裁剪后的图片生成哈希
            pixel_data = cropped_img.tobytes()
            hash_object = hashlib.sha256(pixel_data)
            hash_value = hash_object.hexdigest()

            # 将裁剪的图片保存到临时文件
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as temp_f:
                cropped_img.save(temp_f, format='PNG')
                temp_file_path = temp_f.name

            return str(hash_value), temp_file_path
    except FileNotFoundError:
        logging.error(f"错误: 图片文件未找到 {image_path}")
        return None
    except Exception as e:
        logging.error(f"无法处理或裁剪图片 {image_path}: {e}")
        return None

def upload_file(image_path):
    """
    上传文件总操作
    上传文件到Directus并返回文件ID。
    """
    if not os.path.exists(image_path):
        logging.error(f"错误: 尝试上传但文件不存在: {image_path}")
        return None
    
    files = {'file': (os.path.basename(image_path), open(image_path, 'rb'), 'image/png')}
    try:
        headers = get_auth_headers()
        response = requests.post(f"{DIRECTUS_URL}/files", files=files, headers=headers)
        response.raise_for_status()
        file_id = response.json()['data']['id']
        return file_id
    except (requests.exceptions.RequestException, ValueError, KeyError) as e:
        logging.error(f"上传文件失败 {image_path}: {e}")
        return None
    
def get_or_create_app(app_name, app_version, app_platform, app_package):
    """
    查询App
    获取或创建一个App记录，并返回其ID
    """
    headers = get_auth_headers()
    # 1. 查询App是否存在
    params = {
        'filter[app_name][_eq]': app_name,
        'filter[app_version][_eq]': app_version,
        'filter[app_platform][_eq]': app_platform,
        'filter[app_package][_eq]': app_package,
        'limit': 1
    }
    try:
        response = requests.get(f"{DIRECTUS_URL}/items/{APP_TABLE}", params=params, headers=headers)
        response.raise_for_status()
        data = response.json()['data']
        if data:
            app_id = data[0]['id']
            logging.info(f"找到已存在的App: {app_name} v{app_version}, ID: {app_id[:8]}...")
            return app_id
    except (requests.exceptions.RequestException, ValueError) as e:
        logging.error(f"查询App失败: {e}")
        # 查询失败时，不应继续创建，直接返回None
        return None

    # 2. 若不存在，则已创建
    logging.info(f"未找到App: {app_name} v{app_version}，正在创建...")
    payload = {
        "app_name": app_name,
        "app_version": app_version,
        "app_platform": app_platform,
        "app_package": app_package
    }
    try:
        response = requests.post(f"{DIRECTUS_URL}/items/{APP_TABLE}", json=payload, headers=headers)
        response.raise_for_status()
        app_id = response.json()['data']['id']
        logging.info(f"成功创建App, ID: {app_id[:8]}...")
        return app_id
    except (requests.exceptions.RequestException, ValueError) as e:
        logging.error(f"创建App失败: {e}")
        return None
    

def get_page_by_hash_and_activity(app_id, image_hash, activity_name):
    """
    查询页面
    通过App_id, 哈希值和Activity名称查询页面是否存在。
    """
    params = {
        'filter[app_id][_eq]': app_id,
        'filter[image_hash][_eq]': image_hash,
        'filter[activity_name][_eq]': activity_name,
        'fields': 'id',
        'limit': 1
    }
    try:
        headers = get_auth_headers()
        response = requests.get(f"{DIRECTUS_URL}/items/{PAGES_TABLE}", params=params, headers=headers)
        response.raise_for_status()
        data = response.json()['data']
        return data[0]['id'] if data else None
    except (requests.exceptions.RequestException, ValueError) as e:
        logging.error(f"查询页面失败 (app: {app_id}, hash: {image_hash}, activity: {activity_name}): {e}")
        return None

def get_control_by_attributes(page_id, control_image_hash, bounds):
    """
    查询控件
    通过页面ID和控件哈希值是否存在,新增控件位置
    """
    params = {
        'filter[control_image_hash][_eq]': control_image_hash,
        'filter[Page_id][_eq]': page_id,
        'fields': 'id,bound'
    } # 如果觉得不靠谱可以考虑增加text,id,classtag
    try:
        headers = get_auth_headers()
        response = requests.get(f"{DIRECTUS_URL}/items/{CONTROLS_TABLE}", params=params, headers=headers)
        response.raise_for_status()
        candidates = response.json()['data']

        for candidate in candidates:
            if candidate.get('bound') == bounds:
                return candidate['id']
        return None
    except (requests.exceptions.RequestException, ValueError) as e:
        logging.error(f"查询控件失败 (page: {page_id}, hash: {control_image_hash}): {e}")
        return None

def create_page(app_id, activity_name, image_path, image_hash):
    """
    创建页面
    在Directus中创建一个新的页面记录。
    """
    screenshot_id = upload_file(image_path)
    if not screenshot_id:
        return None
    
    payload = {
        'app_id': app_id,
        'activity_name': activity_name,
        'image_hash': image_hash,
        'screen_shot': screenshot_id
    }
    try:
        headers = get_auth_headers()
        response = requests.post(f"{DIRECTUS_URL}/items/{PAGES_TABLE}", json=payload, headers=headers)
        response.raise_for_status()
        return response.json()['data']['id']
    except (requests.exceptions.RequestException, ValueError) as e:
        logging.error(f"创建页面失败: {e}")
        return None

def get_or_create_page(app_id, activity_name, image_path, image_hash, cache, summary):
    """
    创建页面结合
    获取或创建页面，并返回其ID。
    使用 app_id, image_hash 和 activity_name 作为复合键。
    """
    
    # 1. 使用复合键
    page_unique_key = f"{app_id}--{image_hash}-{activity_name}"

    # 2. 优先从本地缓存中查找
    if page_unique_key in cache:
        logging.info(f"页面已在缓存中找到 (key: {page_unique_key})")
        return cache[page_unique_key]

    # 3. 如果缓存中没有，则查询数据库
    existing_page_id = get_page_by_hash_and_activity(app_id, image_hash, activity_name)
    if existing_page_id:
        logging.info(f"页面已经在数据库中找到(key: {page_unique_key})")
        cache[page_unique_key] = existing_page_id
        return existing_page_id

    # 4. 如果数据库中没有，则创建
    print(f"(key: {page_unique_key}), 创建新页面...")
    page_id = create_page(app_id, activity_name, image_path, image_hash)

    # 5. 如果创建成功，更新缓存
    if page_id:
        print(f"新页面已创建并缓存 (key: {page_unique_key[:16]}...), ID: {page_id[:8]}...")
        cache[page_unique_key] = page_id
    
    return page_id

def create_control(app_id, page_id, bound, text, resource_id, classtag, control_image_hash, control_image_path):
    """
    创建控件
    在Directus中创建一个新的控件记录。
    """
    # 上传控件截图
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
        'control_image': control_image
    }
    try:
        headers = get_auth_headers()
        response = requests.post(f"{DIRECTUS_URL}/items/{CONTROLS_TABLE}", json=payload, headers=headers)
        response.raise_for_status()
        print(f"创建控件成功")
        return response.json()['data']['id']
    except requests.exceptions.HTTPError as e:
        # 打印出服务器返回的详细错误信息
        error_details = e.response.json() if e.response else str(e)
        control_data = payload
        logging.error(f"创建控件失败: {e} - 详细信息: {error_details}, 控件信息: {control_data}")
        return None
    except (requests.exceptions.RequestException, ValueError) as e:
        control_data = payload
        logging.error(f"创建控件失败: {e}, 控件信息: {control_data}")
        return None

def update_control_with_action_id(control_id, action_id):
    """
    补充控件action_id
    更新控件记录，为其关联上一个操作ID。
    """
    payload = {
        'Action_id': action_id
    }
    try:
        headers = get_auth_headers()
        response = requests.patch(f"{DIRECTUS_URL}/items/{CONTROLS_TABLE}/{control_id}", json=payload, headers=headers)
        response.raise_for_status()
        logging.info(f"成功将Action ID ({action_id[:8]}...) 关联到Control ID ({control_id[:8]}...)")
        return True
    except (requests.exceptions.RequestException, ValueError) as e:
        logging.error(f"更新控件失败 (Control: {control_id}, Action: {action_id}): {e}")
        return False

def create_action(source_page_id, destination_page_id, control_id, screenshot_with_box_path, current_index, control_operation, upload_version):
    """在Directus中创建一个新的操作记录。"""
    screenshot_id = upload_file(screenshot_with_box_path)
    if not screenshot_id:
        return None

    payload = {
        'source_page_id': source_page_id,
        'destination_page_id': destination_page_id,
        'control_id': control_id,
        'control_screenshot_with_box': screenshot_id,
        'operation': control_operation,
        'current_index': current_index,
        'upload_version': upload_version
    }
    try:
        headers = get_auth_headers()
        response = requests.post(f"{DIRECTUS_URL}/items/{ACTIONS_TABLE}", json=payload, headers=headers)
        response.raise_for_status()
        print(f"成功创建操作: source_page={source_page_id}, dest_page={destination_page_id}, control={control_id}")
        return response.json()['data']['id']
    except (requests.exceptions.RequestException, ValueError) as e:
        logging.error(f"创建操作失败: {e}")
        return None

def check_action_exists(source_page_id, destination_page_id, control_id):
    """
    查询action
    查询具有相同源、目标和控件的操作是否已存在。
    """
    params = {
        'filter[source_page_id][_eq]': source_page_id,
        'filter[destination_page_id][_eq]': destination_page_id,
        'filter[control_id][_eq]': control_id,
        'limit': 1
    }
    try:
        headers = get_auth_headers()
        response = requests.get(f"{DIRECTUS_URL}/items/{ACTIONS_TABLE}", params=params, headers=headers)
        response.raise_for_status()
        data = response.json()['data']
        return len(data) > 0
    except (requests.exceptions.RequestException, ValueError) as e:
        logging.error(f"查询操作失败: {e}")
        # 查询失败时，保守起见返回False，继续尝试创建
        return False

def get_total_item_count(table_name):
    """
    获取指定表中项目总数。
    """
    params = {'aggregate[count]': '*'}
    try:
        headers = get_auth_headers()
        response = requests.get(f"{DIRECTUS_URL}/items/{table_name}", params=params, headers=headers)
        response.raise_for_status()
        data = response.json()['data']
        return int(data[0]['count'])
    except (requests.exceptions.RequestException, ValueError, KeyError, IndexError) as e:
        logging.error(f"查询 {table_name} 总数失败: {e}")
        return -1 # 返回-1表示查询失败

def find_image_by_index_and_suffix(image_dir, index, suffix):
    """
    文件夹图片查询函数
    根据索引和后缀在目录中查找图片。
    """
    search_prefix = f"{index}_"
    for filename in os.listdir(image_dir):
        if filename.startswith(search_prefix) and filename.endswith(suffix):
            return os.path.join(image_dir, filename), None
    return None, f"未找到以 '{search_prefix}' 开头并以 '{suffix}' 结尾的图片。"

def build_url_lookup_map(element_store_map):
    """
    预处理elements.yml，构建一个索引到URL的快速查找字典。
    """
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

# --- 主逻辑 ---
def main():
    """主函数，执行数据采集和上传。"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        filename=LOG_FILE_PATH,
        filemode='w', # 'w' 模式会在每次运行时覆盖日志文件
        encoding='utf-8'
    )
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(formatter)
    logging.getLogger().addHandler(console_handler)

    # --- 初始化统计 ---
    db_counts_before = {
        'apps': get_total_item_count(APP_TABLE),
        'pages': get_total_item_count(PAGES_TABLE),
        'controls': get_total_item_count(CONTROLS_TABLE),
        'actions': get_total_item_count(ACTIONS_TABLE)
    }
    logging.info(f"--- 运行前数据库状态 ---")
    logging.info(f"总应用数: {db_counts_before['apps']}")
    logging.info(f"总页面数: {db_counts_before['pages']}")
    logging.info(f"总控件数: {db_counts_before['controls']}")
    logging.info(f"总操作数: {db_counts_before['actions']}")
    logging.info("-" * 25)

    if not os.path.exists(ELEMENTS_FILE_PATH):
        logging.error(f"错误: elements.yml 文件未找到 at {ELEMENTS_FILE_PATH}")
        return

    with open(ELEMENTS_FILE_PATH, 'r', encoding=ELEMENTS_ENCODING) as f:
        data = yaml.safe_load(f)

    element_store_map = data.get('elementStoreMap', {})
    if not element_store_map:
        logging.info("在elements.yml中未找到elementStoreMap或其为空。")
        return

    # --- 预处理步骤 ---
    logging.info("正在预处理elements.yml以构建URL查找表...")
    url_lookup = build_url_lookup_map(element_store_map)
    logging.info("URL查找表构建完成。")
    # 将索引->URL 查找表导出到文件，便于排查 unknownactivity 的来源
    try:
        with open(URL_LOOKUP_FILE_PATH, 'w', encoding='utf-8') as f:
            json.dump(url_lookup, f, indent=2, ensure_ascii=False)
        logging.info(f"索引→URL 查找表已导出: {URL_LOOKUP_FILE_PATH} (共 {len(url_lookup)} 条)")
    except Exception as e:
        logging.error(f"导出索引→URL 查找表失败: {e}")

    # app表格上传
    app_id = get_or_create_app(APP_NAME, APP_VERSION, APP_PLATFORM, APP_PACKAGE)
    if not app_id:
        logging.error("无法获取或创建App记录，程序终止。")
        return 
    
    # 页面缓存
    page_cache = {} # 已更改为dict属性

    # 日志缓存
    upload_summary = {
        'created': {'pages': 0, 'controls': 0, 'actions': 0},
        'skipped': {'pages': 0, 'controls': 0, 'actions': 0, 'elements': 0},
        'errors': 0
    }

    for key, value in element_store_map.items():
        element = value.get('element', {})
        action = value.get('action', '')

        if action == 'SKIPPED' or action == 'READY':
            logging.info(f"跳过元素 (标记为 {action}): {key}")
            upload_summary['skipped']['elements'] += 1
            continue

        logging.info(f"\n--- 正在处理元素: {key} ---")

        # 从 resImg 中提取当前索引
        res_img_name_raw = os.path.basename(value.get('resImg', '').replace('\\\\', '\\'))
        match = re.match(r'(\d+)_', res_img_name_raw)
        if not match:
            logging.error(f"警告: 无法从目标图片名 '{res_img_name_raw}' 中解析出数字索引。跳过此元素。")
            upload_summary['errors'] += 1
            continue
        current_index = int(match.group(1))

        # 1. 处理目标页面 (Destination Page)
        dest_img_suffix = ".clicked.png"
        dest_img_path, error_msg = find_image_by_index_and_suffix(IMAGE_DIR, current_index, dest_img_suffix)
        if error_msg:
            logging.error(f"错误: {error_msg} 跳过此元素。")
            upload_summary['errors'] += 1
            continue
        
        dest_img_name = os.path.basename(dest_img_path)
        dest_image_hash = generate_image_hash(dest_img_path)
        if not dest_image_hash:
            logging.error(f"错误: 无法为目标图片生成哈希值: {dest_img_name}。跳过此元素。")
            upload_summary['errors'] += 1
            continue
            
        # 2. 处理源页面 (Source Page)
        req_img_suffix = ".click.png"
        req_img_full_path, error_msg = find_image_by_index_and_suffix(IMAGE_DIR, current_index, req_img_suffix)
        if error_msg:
            logging.warning(f"警告: 未找到操作带框截图 (.click.png)，将跳过创建此控件和操作。原因: {error_msg}")
            upload_summary['skipped']['controls'] += 1
            upload_summary['skipped']['actions'] += 1
            continue

        if current_index == 0:
            logging.warning("警告: 当前图片索引为0，没有源页面。无法创建操作。")
            continue
        source_index = current_index - 1
        source_img_suffix = ".clicked.png"
        source_activity_name = url_lookup.get(current_index, 'Unknownactivity')
        source_img_full_path, error_msg = find_image_by_index_and_suffix(IMAGE_DIR, source_index, source_img_suffix)
        if error_msg:
            logging.warning(f"警告: {error_msg} 无法创建控件。")
            continue

        source_image_hash = generate_image_hash(source_img_full_path)
        if not source_image_hash:
            logging.warning(f"警告: 无法为源图片 '{os.path.basename(source_img_full_path)}' 生成哈希值。无法创建操作。")
            continue

        # 若destination_activity查找失败则进行判断，若Page_hash一致则沿用source_activity反之则跳过所有创建
        destination_activity_name = url_lookup.get(current_index + 1, 'Unknownactivity')
        if destination_activity_name == "Unknownactivity":
            if dest_image_hash == source_image_hash:
                destination_activity_name = source_activity_name
            else:
                logging.error(f"警告: destnation_activity无法查找且前后点击后发生了变化")
                continue
        
        is_source_page_new = get_page_by_hash_and_activity(app_id, source_image_hash, source_activity_name) is None
        source_page_id = get_or_create_page(app_id, source_activity_name, source_img_full_path, source_image_hash, page_cache, upload_summary)
        
        if not source_page_id:
            logging.warning(f"警告: 无法获取或创建源页面。无法创建控件。")
            continue
        elif is_source_page_new and source_image_hash not in page_cache:
             upload_summary['created']['pages'] += 1
        elif not is_source_page_new:
             upload_summary['skipped']['pages'] += 1
        
        is_page_new = get_page_by_hash_and_activity(app_id, dest_image_hash, destination_activity_name) is None
        destination_page_id = get_or_create_page(app_id, destination_activity_name, dest_img_path, dest_image_hash, page_cache, upload_summary)
        
        if not destination_page_id:
            logging.error(f"错误: 无法获取或创建目标页面。跳过此元素。")
            upload_summary['errors'] += 1
            continue
        elif is_page_new:
            upload_summary['created']['pages'] += 1
        else:
            upload_summary['skipped']['pages'] += 1


        # 3. 创建控件 (Control)
        x, y, width, height = element.get('x', 0), element.get('y', 0), element.get('width', 0), element.get('height', 0)
        bounds = {
            "x1": x / SCREEN_WIDTH, "y1": y / SCREEN_HEIGHT,
            "x2": (x + width) / SCREEN_WIDTH, "y2": (y + height) / SCREEN_HEIGHT
        }
        text = element.get('text', '')
        resource_id = element.get('id', '')
        classtag = element.get('className', '')
        control_operation = 'click'

        # 生成控件的唯一可视化哈希
        control_image_hash, control_image_temp_path = generate_cropped_image_hash(source_img_full_path, bounds)
        if not control_image_hash:
            logging.error(f"错误: 无法为控件生成哈希值 (element: {key})。跳过此元素。")
            upload_summary['errors'] += 1
            continue

        # 通过哈希检查控件是否已存在
        control_id = get_control_by_attributes(source_page_id, control_image_hash, bounds)
        
        if control_id:
            logging.info(f"控件已在数据库中找到 (hash: {control_image_hash[:8]}...), ID: {control_id[:8]}...")
            upload_summary['skipped']['controls'] += 1
            if control_image_temp_path and os.path.exists(control_image_temp_path):
                os.remove(control_image_temp_path)
        else:
            # 如果不存在，则创建新控件
            logging.info(f"数据库中未找到控件，正在创建新控件 (hash: {control_image_hash[:8]}...)")
            control_id = create_control(app_id, source_page_id, bounds, text, resource_id, classtag, control_image_hash, control_image_temp_path)
            if control_id:
                upload_summary['created']['controls'] += 1
            if control_image_temp_path and os.path.exists(control_image_temp_path):
                os.remove(control_image_temp_path)

        if not control_id:
            logging.error(f"错误: 无法获取或创建控件。跳过此元素。")
            upload_summary['errors'] += 1
            continue


        # 4. 创建操作 (Action)
        if check_action_exists(source_page_id, destination_page_id, control_id):
            logging.info(f"操作已存在，跳过创建: source={source_page_id[:8]}..., control={control_id[:8]}..., dest={destination_page_id[:8]}...")
            upload_summary['skipped']['actions'] += 1
            continue

        action_id = create_action(source_page_id, destination_page_id, control_id, req_img_full_path, current_index, control_operation, upload_version)
        if action_id:
            upload_summary['created']['actions'] += 1
            # 成功创建Action后，回写更新Control
            update_control_with_action_id(control_id, action_id)
        else:
            upload_summary['errors'] += 1

    # --- 结束时再次统计 ---
    db_counts_after = {
        'pages': get_total_item_count(PAGES_TABLE),
        'controls': get_total_item_count(CONTROLS_TABLE),
        'actions': get_total_item_count(ACTIONS_TABLE)
    }
    
    # --- 生成并打印总结报告 ---
    logging.info("\n" + "="*40)
    logging.info("--- 上传完成 ---")
    logging.info(f"本次运行创建: {upload_summary['created']['pages']} 页面, {upload_summary['created']['controls']} 控件, {upload_summary['created']['actions']} 操作")
    logging.info(f"本次运行跳过: {upload_summary['skipped']['pages']} 页面, {upload_summary['skipped']['controls']} 控件, {upload_summary['skipped']['actions']} 操作")
    logging.info(f"被标记为SKIPPED/READY的元素数量: {upload_summary['skipped']['elements']}")
    logging.info(f"处理期间发生错误数: {upload_summary['errors']}")
    logging.info("-" * 25)
    logging.info("--- 运行后数据库状态 ---")
    logging.info(f"总页面数: {db_counts_after['pages']} (新增: {db_counts_after['pages'] - db_counts_before['pages']})")
    logging.info(f"总控件数: {db_counts_after['controls']} (新增: {db_counts_after['controls'] - db_counts_before['controls']})")
    logging.info(f"总操作数: {db_counts_after['actions']} (新增: {db_counts_after['actions'] - db_counts_before['actions']})")
    logging.info("="*40)


    try:
        # 将最终统计数据也加入到json文件中
        upload_summary['db_counts_before'] = db_counts_before
        upload_summary['db_counts_after'] = db_counts_after
        with open(SUMMARY_FILE_PATH, 'w', encoding='utf-8') as f:
            json.dump(upload_summary, f, indent=4, ensure_ascii=False)
        logging.info(f"详细上传总结报告已保存至: {SUMMARY_FILE_PATH}")
        logging.info(f"错误日志已保存至: {LOG_FILE_PATH}")
    except Exception as e:
        logging.error(f"\n错误: 无法保存上传总结报告: {e}")
    

if __name__ == "__main__":
    main()
