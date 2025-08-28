# -*- coding: utf-8 -*-
"""
清洗与归纳脚本
- 读取 collected_data.json（或指定输入）
- 以 activity 分组
- 以 (bounds, text, class, resource-id, activity, action) 作为去重键，保留首次出现
- 导出两个结果：
  1) 分组后的控件：{ activity: [items...] }
  2) 计数汇总：{ activity: count, ..., _total_controls, _total_activities }

用法:
  python clean_and_group.py --input collected_data.json \
                           --out grouped_controls.json \
                           --counts activity_counts.json

如果不指定 --out / --counts，将在输入文件同目录生成默认文件名：
  <input_dir>/collected_grouped.json 和 <input_dir>/collected_activity_counts.json
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict, OrderedDict
from typing import Any, Dict, Iterable, List, Tuple


DedupKey = Tuple[str, str, str, str, str, str]


def _norm(v: Any) -> str:
    """规范化键值：None->"", 其他转为去首尾空白的字符串。"""
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        # 罕见情况，确保可序列化且稳定
        try:
            return json.dumps(v, ensure_ascii=False, sort_keys=True)
        except Exception:
            return str(v)
    return str(v).strip()


def make_dedup_key(item: Dict[str, Any]) -> DedupKey:
    node = item.get("node", {}) or {}
    return (
        _norm(node.get("bounds")),
        _norm(node.get("text")),
        _norm(node.get("class")),
        _norm(node.get("resource-id")),
        _norm(item.get("activity")),
        _norm(item.get("action")),
    )


def clean_and_group(items: Iterable[Dict[str, Any]]) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, int]]:
    """去重并按 activity 分组。
    返回 (grouped, counts) 两个字典。
    """
    grouped: Dict[str, List[Dict[str, Any]]] = OrderedDict()
    seen: set[DedupKey] = set()

    for it in items:
        activity = _norm(it.get("activity"))
        if not activity:
            # 若缺失 activity，归到 "<unknown>"
            activity = "<unknown>"
        key = make_dedup_key(it)
        if key in seen:
            continue  # 去重：丢弃重复项，仅保留首次出现
        seen.add(key)
        grouped.setdefault(activity, []).append(it)

    counts = OrderedDict((act, len(lst)) for act, lst in grouped.items())
    return grouped, counts


def load_json_list(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"输入数据应为列表，但实际为: {type(data)}")
    return data


def save_json(obj: Any, path: str) -> None:
    # ensure_ascii=False 以保留中文
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="控件数据去重与按 activity 分组")
    parser.add_argument("--input", required=True, help="输入 JSON 文件路径，例如 collected_data.json")
    parser.add_argument("--out", default=None, help="分组结果输出路径，默认在输入同目录生成 collected_grouped.json")
    parser.add_argument("--counts", default=None, help="计数结果输出路径，默认在输入同目录生成 collected_activity_counts.json")
    args = parser.parse_args()

    in_path = os.path.abspath(args.input)
    in_dir = os.path.dirname(in_path)

    out_group_path = args.out or os.path.join(in_dir, "collected_grouped.json")
    out_counts_path = args.counts or os.path.join(in_dir, "collected_activity_counts.json")

    items = load_json_list(in_path)

    grouped, counts = clean_and_group(items)

    # 输出1：按 activity 分组的控件（值为列表，契合期望示例结构）
    save_json(grouped, out_group_path)

    # 输出2：计数（附带总览）
    summary = OrderedDict(counts)
    summary["_total_controls"] = sum(counts.values())
    summary["_total_activities"] = len(counts)
    save_json(summary, out_counts_path)

    # 终端摘要
    print("输入条目:", len(items))
    print("去重后条目:", summary["_total_controls"])  # 与分组后所有列表长度之和一致
    print("去重后 activity 数:", summary["_total_activities"]) 
    print("按控件数量排序的前若干 activity:")
    for act, cnt in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"  {act}: {cnt}")


if __name__ == "__main__":
    main()
