# -*- coding: utf-8 -*-
"""
简易定时器：指定首次执行时间和重复间隔。

用法:
    python timer_hello.py "2026/06/12 05:50" 5

参数:
    start_time  — 首次执行时间，格式 YYYY/MM/DD HH:MM
    interval_h  — 后续每次执行的间隔（小时）
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta

# 加载项目环境
from src.config import setup_env
setup_env()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def job():
    """给大模型发一次 hello。"""
    from src.services.anthropic_direct_client import AnthropicDirectClient
    client = AnthropicDirectClient()
    logger.info(">>> 发送 hello 给 %s @ %s", client.model, client.base_url)
    resp = client.create_message("hello", system="简短回复即可")
    logger.info("<<< 回复: %s", resp.content[:200])


def parse_start_time(s: str) -> datetime:
    return datetime.strptime(s.strip(), "%Y/%m/%d %H:%M")


def main():
    parser = argparse.ArgumentParser(description="简易定时器")
    parser.add_argument("start_time", help="首次执行时间，格式 YYYY/MM/DD HH:MM")
    parser.add_argument("interval_hours", type=float, help="间隔小时数")
    args = parser.parse_args()

    start = parse_start_time(args.start_time)
    interval = timedelta(hours=args.interval_hours, minutes=1)
    now = datetime.now()

    logger.info("首次执行时间: %s", start.strftime("%Y-%m-%d %H:%M"))
    logger.info("重复间隔: %s 小时 1 分钟", args.interval_hours)

    # 计算首次等待
    if now >= start:
        # 已过首次时间，按间隔补算下一次
        elapsed = now - start
        missed = int(elapsed / interval) + 1
        next_run = start + missed * interval
        logger.info("已过首次时间，调整为: %s", next_run.strftime("%Y-%m-%d %H:%M"))
    else:
        next_run = start

    while True:
        wait = (next_run - datetime.now()).total_seconds()
        if wait > 0:
            logger.info("下次执行: %s（等待 %.0f 秒）", next_run.strftime("%Y-%m-%d %H:%M"), wait)
            time.sleep(wait)

        job()
        next_run += interval


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("已停止")
        sys.exit(0)
