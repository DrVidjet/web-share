@echo off
rem web-share: установка / обновление / удаление на Windows. Параметры - как у start.ps1: -Yes, -Answers файл, -Uninstall
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 pause
