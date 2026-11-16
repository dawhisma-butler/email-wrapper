@echo off
rem Windows launcher for sermail.py so callers can just run:  sermail --to ...
rem Passes all arguments through and returns sermail.py's exit code.
setlocal
where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py -3 "%~dp0sermail.py" %*
) else (
    python "%~dp0sermail.py" %*
)
exit /b %ERRORLEVEL%
