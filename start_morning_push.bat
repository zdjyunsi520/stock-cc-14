@echo off
cd /d E:\github\daily_stock_analysis

:: 每日早晨推送规律选股结果
:: 8:30 执行

echo [%date% %time%] 开始规律选股分析+推送...
python -X utf8 main.py --pattern-screen --notify
echo [%date% %time%] 规律选股推送完成
