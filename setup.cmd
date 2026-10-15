@echo off
setlocal
powershell.exe -NoLogo -NoProfile -File "%~dp0setup.ps1" %*
set "TwitchCutExit=%ERRORLEVEL%"
if not "%TwitchCutExit%"=="0" (
  echo.
  echo Setup did not finish. Keep this output and ask Codex to read README.md.
  echo If PowerShell policy blocked the script, ask Codex to use the documented
  echo direct Python setup steps. Do not disable your execution policy.
)
exit /b %TwitchCutExit%
