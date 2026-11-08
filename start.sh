#!/usr/bin/env bash
# Uruchomienie na własnym komputerze (Mac/Linux): bash start.sh
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
if [ ! -f .env ]; then
  read -rsp "Wklej token bota od @BotFather: " TOKEN; echo
  cp .env.example .env
  sed -i.bak "s|^BOT_TOKEN=.*|BOT_TOKEN=$TOKEN|" .env && rm -f .env.bak
fi
echo "Bot startuje. Napisz do niego /start w Telegramie. Zatrzymanie: Ctrl+C"
.venv/bin/python -m copybot.main
