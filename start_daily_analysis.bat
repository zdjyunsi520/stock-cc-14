@echo off
cd /d E:\github\daily_stock_analysis

:: 每日18:00 增量同步 + 规律选股
:: 概念缓存由16/19/22点任务提供

echo [%date% %time%] 开始增量同步...
python -X utf8 main.py --sync-incremental
echo [%date% %time%] 增量同步完成

echo [%date% %time%] 开始洗盘精选+推送...
python -X utf8 main.py --pattern-screen --wash --notify
echo [%date% %time%] 洗盘精选完成
