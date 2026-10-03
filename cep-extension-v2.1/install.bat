@echo off
REM EasyScript CEP extension - Windows dev install (copy + enable debug mode)
setlocal

echo [EasyScript] Enabling CEP debug mode (unsigned extensions)...
for %%v in (9 10 11 12 13 14 15) do (
  reg add "HKCU\Software\Adobe\CSXS.%%v" /v PlayerDebugMode /t REG_SZ /d 1 /f >nul 2>&1
)

set "DEST=%APPDATA%\Adobe\CEP\extensions\com.easyscript.premiere"
echo [EasyScript] Installing to %DEST%
if exist "%DEST%" rmdir /s /q "%DEST%"
mkdir "%DEST%"
xcopy "%~dp0*" "%DEST%\" /E /I /Y /Q ^
  /EXCLUDE:%~dp0\.installexclude >nul 2>&1 || xcopy "%~dp0*" "%DEST%\" /E /I /Y /Q >nul

echo [EasyScript] Done.
echo [EasyScript] 1) Start backend (server.exe or: python server.py)
echo [EasyScript] 2) Restart Premiere Pro
echo [EasyScript] 3) Window ^> Extensions ^> EasyScript
echo [EasyScript] Debug: open http://localhost:8088 in a browser while the panel is open
endlocal
pause
