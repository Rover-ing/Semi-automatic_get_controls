import requests
import json
import os

# --- 从 colector.py 中获取配置 ---
# 假设 finder.py 和 colector.py 在同一个目录下，如果不是，请调整路径
try:
    from colector import DIRECTUS_URL, DIRECTUS_TOKEN, PAGES_TABLE, CONTROLS_TABLE
except (ImportError, ModuleNotFoundError):
    print("警告: 无法从 colector.py 导入配置。将使用文件内定义的默认值。")
    print("请确保 colector.py 在 Python 路径中，或者将此脚本放在与 colector.py 相同的目录中。")
    DIRECTUS_URL = "http://114.132.210.199:8055"
    DIRECTUS_TOKEN = "rVVSnBl-HPR2xOvr9-4nD15GZkY04DC7"
    PAGES_TABLE = "Pages"
    CONTROLS_TABLE = "Controls"


def get_auth_headers():
    """返回包含认证令牌的请求头。"""
    if not DIRECTUS_TOKEN:
        raise ValueError("错误: 请设置好您的Directus静态访问令牌 (DIRECTUS_TOKEN)。")
    return {
        'Authorization': f'Bearer {DIRECTUS_TOKEN}',
        'Content-Type': 'application/json'
    }

def get_all_items(table_name, fields=None):
    """
    从指定的Directus表中获取所有项目。
    通过设置 limit=-1 来避免分页问题。
    """
    params = {'limit': -1}
    if fields:
        params['fields'] = ','.join(fields)
        
    try:
        headers = get_auth_headers()
        print(f"正在从 '{table_name}' 表中获取数据...")
        response = requests.get(f"{DIRECTUS_URL}/items/{table_name}", params=params, headers=headers)
        response.raise_for_status()
        print(f"成功获取 {len(response.json()['data'])} 条记录。")
        return response.json()['data']
    except requests.exceptions.RequestException as e:
        print(f"错误: 查询 '{table_name}' 表失败: {e}")
        if e.response:
            print(f"响应内容: {e.response.text}")
        return None
    except ValueError:
        print("错误: 无法解析响应内容。")
        return None

def count_controls_per_activity():
    """
    计算每个Activity中的控件数量。
    """
    # 1. 获取所有页面，只需要id和activity_name字段
    pages = get_all_items(PAGES_TABLE, fields=['id', 'activity_name'])
    if pages is None:
        return

    # 2. 获取所有控件，只需要Page_id字段
    controls = get_all_items(CONTROLS_TABLE, fields=['Page_id'])
    if controls is None:
        return

    # 3. 创建从 page_id 到 activity_name 的映射
    page_id_to_activity_map = {page['id']: page['activity_name'] for page in pages}

    # 4. 统计每个 activity 的控件数
    activity_counts = {}
    for control in controls:
        # 确保控件关联的页面ID存在
        page_id_info = control.get('Page_id')
        if page_id_info:
            # Page_id 可能是一个字典 {'id': '...'} 或直接是ID字符串
            page_id = page_id_info['id'] if isinstance(page_id_info, dict) else page_id_info
            
            activity_name = page_id_to_activity_map.get(page_id)
            if activity_name:
                activity_counts[activity_name] = activity_counts.get(activity_name, 0) + 1
            else:
                # 如果找不到对应的activity，可以记录下来
                unknown_activity_key = "未知或已删除的页面"
                activity_counts[unknown_activity_key] = activity_counts.get(unknown_activity_key, 0) + 1

    # 5. 打印结果
    print("\n--- Activity 控件数量统计 ---")
    if not activity_counts:
        print("未找到任何控件，或者控件没有关联到任何页面。")
    else:
        # 按控件数量降序排序
        sorted_activities = sorted(activity_counts.items(), key=lambda item: item[1], reverse=True)
        for activity, count in sorted_activities:
            print(f"- {activity}: {count} 个控件")
    print("="*30)


if __name__ == "__main__":
    count_controls_per_activity()
