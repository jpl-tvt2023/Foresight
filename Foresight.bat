@echo off
rem Foresight launcher — double-click to start the local dashboard.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m foresight serve --port 8000 --open
pause
