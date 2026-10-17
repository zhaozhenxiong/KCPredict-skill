@echo off
setlocal
where py >nul 2>nul
if not errorlevel 1 goto use_launcher
where python >nul 2>nul
if not errorlevel 1 goto use_python
echo Python 3.12 is required. Install it and run py -3.12 install.py.
exit /b 1
:use_launcher
py -3.12 "%~dp0install.py" %*
exit /b %errorlevel%
:use_python
python "%~dp0install.py" %*
exit /b %errorlevel%
