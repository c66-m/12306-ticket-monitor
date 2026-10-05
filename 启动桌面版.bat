@echo off
rem 12306 车票监控与自动购票系统 —— 桌面版启动入口
rem 双击本文件或在文件上右键运行，即打开图形界面（无命令行窗口）
cd /d "%~dp0"
start "" pythonw gui.py