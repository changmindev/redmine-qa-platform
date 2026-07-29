@echo off
rem ---------------------------------------------------------------------
rem  ASCII ONLY - do not put Korean text in this file.
rem
rem  cmd.exe decodes .bat bytes using the active console codepage (949 on
rem  Korean Windows, 65001 elsewhere). Any non-ASCII text here renders as
rem  garbage on whichever machine uses the other codepage.
rem  All Korean messages live in tools/bootstrap.py, where Python encodes
rem  them correctly for whatever codepage the console happens to use.
rem ---------------------------------------------------------------------
setlocal
cd /d "%~dp0"
title Redmine QA Platform

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo [ERROR] Python was not found.
    echo         Install it from https://www.python.org
    echo         Be sure to check "Add Python to PATH" during setup.
    echo.
    pause
    exit /b 1
)

python "tools\bootstrap.py" %*
set EXITCODE=%ERRORLEVEL%

echo.
pause
exit /b %EXITCODE%
