#!/usr/bin/env bash
# Instalacja bota na serwerze Ubuntu/Debian jako usługa działająca 24/7.
# Użycie (w rozpakowanym folderze):  sudo bash install.sh
set -euo pipefail

APP_DIR=/opt/copybot
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then echo "Uruchom przez sudo: sudo bash install.sh"; exit 1; fi

echo "==> Instaluję Pythona"
apt-get update -qq
apt-get install -y -qq python3 python3-venv curl >/dev/null

echo "==> Kopiuję pliki do $APP_DIR"
id copybot &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin copybot
mkdir -p "$APP_DIR"
cp -r "$SRC_DIR/copybot" "$SRC_DIR/requirements.txt" "$APP_DIR/"

echo "==> Instaluję biblioteki (może potrwać minutę)"
[ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
  echo
  echo "==> Konfiguracja"
  while true; do
    read -rsp "Wklej token bota od @BotFather (nie będzie widoczny): " TOKEN; echo
    NAME=$(curl -s "https://api.telegram.org/bot${TOKEN}/getMe" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["result"]["username"] if d.get("ok") else "")' 2>/dev/null || true)
    [ -n "$NAME" ] && break
    echo "Ten token nie działa, spróbuj jeszcze raz."
  done
  read -rp "Domyślna giełda – ceny w trybie demo [binance/bybit/okx/kucoin/bitget/gate/mexc] (Enter = binance): " EXCHANGE
  EXCHANGE=${EXCHANGE:-binance}
  echo "Użytkownicy i traderzy wybiorą własną giełdę w bocie."
  echo "(Opcjonalnie) klucz TWOJEGO konta, jeśli chcesz od razu być traderem #1 (wystarczy klucz do odczytu)."
  echo "Enter = pomiń – traderzy zgłoszą się sami przez /zostan_traderem, a Ty możesz wysyłać sygnały /sygnal."
  read -rp "  API Key: " LKEY
  LSECRET=""; LPASS=""
  if [ -n "$LKEY" ]; then
    read -rsp "  API Secret: " LSECRET; echo
    case "$EXCHANGE" in okx|kucoin|bitget) read -rsp "  Passphrase: " LPASS; echo;; esac
  fi
  SYMS=""
  read -rp "Cena subskrypcji w gwiazdkach Telegrama za 30 dni (Enter = 0, bot darmowy): " PRICE
  PRICE=${PRICE:-0}
  WEBAPP_URL=""
  read -rp "Włączyć panel z wykresami (Mini App)? Wymaga otwartych portów 80 i 443 [T/n]: " PANEL
  if [[ ! "$PANEL" =~ ^[nN] ]]; then
    IP=$(curl -s --max-time 10 https://api.ipify.org || true)
    DEFAULT_DOMAIN="${IP//./-}.sslip.io"
    read -rp "Domena panelu (Enter = darmowa $DEFAULT_DOMAIN): " DOMAIN
    DOMAIN=${DOMAIN:-$DEFAULT_DOMAIN}
    WEBAPP_URL="https://$DOMAIN"
  fi
  umask 077
  cat > "$APP_DIR/.env" <<ENV
BOT_TOKEN=$TOKEN
EXCHANGE=$EXCHANGE
EXCHANGES=binance,bybit,okx,coinbase,kucoin,bitget,gate,mexc
REALTIME=true
SANDBOX=false
LEADER_API_KEY=$LKEY
LEADER_API_SECRET=$LSECRET
LEADER_API_PASSWORD=$LPASS
LEADER_SYMBOLS=$SYMS
QUOTE=USDT
POLL_INTERVAL=15
PAPER_START=1000
DB_PATH=$APP_DIR/data/copybot.db
SUB_PRICE_STARS=$PRICE
SUB_SCOPE=live
WEBAPP_URL=$WEBAPP_URL
WEB_PORT=8080
ENV
else
  NAME=$(grep -E '^BOT_TOKEN=' "$APP_DIR/.env" | cut -d= -f2- | xargs -I{} curl -s "https://api.telegram.org/bot{}/getMe" | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["username"])' 2>/dev/null || echo "twoj_bot")
  echo "==> Zostawiam istniejącą konfigurację $APP_DIR/.env"
fi

WEBAPP_URL=$(grep -E '^WEBAPP_URL=' "$APP_DIR/.env" | cut -d= -f2- || true)
if [ -n "$WEBAPP_URL" ]; then
  DOMAIN=${WEBAPP_URL#https://}
  echo "==> Panel: instaluję Caddy (automatyczny certyfikat HTTPS dla $DOMAIN)"
  if ! command -v caddy >/dev/null; then
    apt-get install -y -qq caddy >/dev/null 2>&1 || {
      apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null
      curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
      curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
      apt-get update -qq && apt-get install -y -qq caddy >/dev/null
    }
  fi
  mkdir -p /etc/caddy
  cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
    reverse_proxy 127.0.0.1:8080
}
CADDY
  if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; fi
  systemctl enable caddy >/dev/null 2>&1 || true
  systemctl restart caddy
fi

mkdir -p "$APP_DIR/data"
chown -R copybot:copybot "$APP_DIR"
chmod 600 "$APP_DIR/.env"

cat > /etc/systemd/system/copybot.service <<UNIT
[Unit]
Description=Copy Trading Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
User=copybot
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/python -m copybot.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now copybot >/dev/null 2>&1
systemctl restart copybot
sleep 5

if systemctl is-active --quiet copybot; then
  echo
  echo "✅ Bot działa 24/7 i sam wstanie po restarcie serwera."
  echo "👉 Otwórz https://t.me/$NAME i napisz /start – pierwsza osoba zostaje administratorem."
  [ -n "$WEBAPP_URL" ] && echo "📈 Panel: $WEBAPP_URL/app (otwiera się przyciskiem w bocie; certyfikat może się tworzyć ~1 min)"
  echo
  echo "Logi:     journalctl -u copybot -f"
  echo "Restart:  systemctl restart copybot"
  echo "Ustawienia: nano $APP_DIR/.env  (potem restart)"
else
  echo "❌ Bot nie wystartował. Ostatnie logi:"
  journalctl -u copybot -n 30 --no-pager
  exit 1
fi
