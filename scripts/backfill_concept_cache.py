# -*- coding: utf-8 -*-
"""
补指定日期的概念缓存。

用法：
    python scripts/backfill_concept_cache.py 20260612

前置条件：
    1. 在 Chrome 中打开 https://q.10jqka.com.cn/gn/
    2. 确保扩展已加载并连接（localhost:3123）
    3. 如果补的是周一到周五的数据，要在对应日期之后访问页面
       （如补周五数据，周六或周日访问即可）
"""

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_provider.ths_fetcher import ThsFetcher


def main():
    if len(sys.argv) < 2:
        print("用法: python backfill_concept_cache.py YYYYMMDD")
        print("示例: python backfill_concept_cache.py 20260612")
        sys.exit(1)

    date_key = sys.argv[1]
    print(f"补 {date_key} 的概念缓存...")

    # 1. 从扩展获取页面快照
    print("步骤1: 获取页面快照...")
    import requests
    try:
        resp = requests.get("http://127.0.0.1:3123/tabs", timeout=5)
        resp.raise_for_status()
        tabs = resp.json().get("tabs", [])

        # 找到同花顺概念板块的 tab
        target_tab = None
        for tab in tabs:
            if "q.10jqka.com.cn/gn" in tab.get("url", ""):
                target_tab = tab["tabId"]
                break

        if not target_tab:
            print("错误: 未找到同花顺概念板块页面，请在 Chrome 中打开:")
            print("       https://q.10jqka.com.cn/gn/")
            sys.exit(1)

        # 获取快照
        resp = requests.get(f"http://127.0.0.1:3123/snapshot?tabId={target_tab}", timeout=10)
        resp.raise_for_status()
        snapshot = resp.json()

        if not snapshot.get("success"):
            print("错误: 获取快照失败")
            sys.exit(1)

    except Exception as e:
        print(f"错误: 无法连接到扩展服务器: {e}")
        print("请确保:")
        print("  1. 在 E:/github/daily_stock_analysis/google-kefu 目录运行 node server.js")
        print("  2. 在 Chrome 中打开 https://q.10jqka.com.cn/gn/")
        sys.exit(1)

    # 2. 解析概念排行
    print("步骤2: 解析概念排行...")
    concepts_rank = None
    for inp in snapshot.get("data", {}).get("inputs", []):
        if inp.get("selector") == "#gnSection":
            concepts_rank = json.loads(inp.get("value"))
            break

    if not concepts_rank:
        print("错误: 页面未加载概念数据")
        sys.exit(1)

    # 取 TOP 20 热门概念
    sorted_concepts = sorted(
        concepts_rank.items(),
        key=lambda x: float(x[1].get("199112", 0)),
        reverse=True
    )
    top_20 = sorted_concepts[:20]
    concept_names = [v.get("platename") for k, v in top_20]

    print(f"  获取到 TOP 20 热门概念:")
    for i, name in enumerate(concept_names[:5]):
        print(f"    {i+1}. {name}")
    print(f"    ... 共 {len(concept_names)} 个")

    # 3. 获取成分股
    print("步骤3: 获取成分股...")
    ths = ThsFetcher()
    universe = {}

    for i, concept_name in enumerate(concept_names):
        try:
            members = ths.get_board_members(concept_name, board_type="concept", max_members=100)
            if members:
                stock_codes = [m["code"] for m in members if m.get("code")]
                universe[concept_name] = stock_codes
                print(f"  [{i+1}/{len(concept_names)}] {concept_name}: {len(stock_codes)}只")
            else:
                print(f"  [{i+1}/{len(concept_names)}] {concept_name}: 无数据")
                universe[concept_name] = []
        except Exception as e:
            print(f"  [{i+1}/{len(concept_names)}] {concept_name}: 失败 - {e}")
            universe[concept_name] = []

    # 4. 转换格式: {概念: [股票]} -> {股票: [概念]}
    print("步骤4: 转换格式...")
    stock_to_concepts = {}
    for concept, stocks in universe.items():
        for stock in stocks:
            if stock not in stock_to_concepts:
                stock_to_concepts[stock] = []
            stock_to_concepts[stock].append(concept)

    # 5. 保存缓存
    print("步骤5: 保存缓存...")
    cache_dir = "data/concept_cache"
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, f"concept_universe_{date_key}.json")

    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(stock_to_concepts, f, ensure_ascii=False, indent=2)

    print(f"\n✓ 缓存已保存: {cache_file}")
    print(f"  股票数: {len(stock_to_concepts)}")


if __name__ == "__main__":
    main()
