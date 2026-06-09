@echo off
cd /d E:\github\daily_stock_analysis

:: 仅缓存当天概念板块数据（不依赖日线）
echo [%date% %time%] 开始缓存概念板块...
python -X utf8 main.py --cache-concepts
echo [%date% %time%] 概念缓存完成
