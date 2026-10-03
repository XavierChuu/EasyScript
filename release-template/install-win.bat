@echo off
REM EasyScript for Premiere Pro - Windows installer. Double-click to run.
setlocal
set "HERE=%~dp0"
echo === Installing EasyScript ===

REM 1) Enable CEP debug mode (loads the panel)
for %%v in (9 10 11 12 13 14 15) do (
  reg add "HKCU\Software\Adobe\CSXS.%%v" /v PlayerDebugMode /t REG_SZ /d 1 /f >nul 2>&1
)

REM 2) Install the panel (extract the .zxp into the CEP extensions folder)
set "EXT=%APPDATA%\Adobe\CEP\extensions\com.easyscript.premiere"
if exist "%EXT%" rmdir /s /q "%EXT%"
mkdir "%EXT%"
tar -xf "%HERE%EasyScript-Premiere.zxp" -C "%EXT%"
echo   Panel   -^> %EXT%

REM 3) Install the backend
set "BK=%USERPROFILE%\.easyscript\backend"
if exist "%BK%" rmdir /s /q "%BK%"
mkdir "%BK%"
xcopy "%HERE%backend\*" "%BK%\" /E /I /Y /Q >nul
echo   Backend -^> %BK%

echo.
echo === Done ===
echo 1) RESTART Premiere Pro
echo 2) Window ^> Extensions ^> EasyScript
echo The panel starts the backend automatically on first open.
echo.
pause
endlocal
