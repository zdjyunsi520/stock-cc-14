@echo off
cd /d E:\github\daily_stock_analysis

:: 每日早晨推送规律选股结果
:: 8:30 执行

echo [%date% %time%] 开始洗盘精选推送...
python -X utf8 main.py --pattern-screen --wash --notify
echo [%date% %time%] 洗盘精选推送完成
