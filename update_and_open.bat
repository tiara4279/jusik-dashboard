@echo off
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python update_data.py
start "" "%~dp0index.html"
