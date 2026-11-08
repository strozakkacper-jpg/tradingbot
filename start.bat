@echo off
REM Uruchomienie na Windows: kliknij dwa razy. Wymaga Pythona z python.org (zaznacz "Add to PATH").
cd /d "%~dp0"
if not exist .venv python -m venv .venv
.venv\Scripts\pip install -q -r requirements.txt
if exist .env goto run
set /p TOKEN=Wklej token bota od @BotFather: 
copy .env.example .env >nul
powershell -NoProfile -Command "(Get-Content .env) -replace '^BOT_TOKEN=.*', ('BOT_TOKEN=' + $env:TOKEN) | Set-Content .env"
:run
echo Bot startuje. Napisz do niego /start w Telegramie. Zamknij okno, zeby zatrzymac.
.venv\Scripts\python -m copybot.main
pause
