@echo off
cd /d E:\github\daily_stock_analysis

:: 每日18:00 增量同步 + 规律选股
:: 概念缓存由16/19/22点任务提供

echo [%date% %time%] 开始增量同步...
python -X utf8 main.py --sync-incremental
echo [%date% %time%] 增量同步完成

echo [%date% %time%] 开始规律选股分析+推送...
python -X utf8 main.py --pattern-screen --notify
echo [%date% %time%] 规律选股完成
