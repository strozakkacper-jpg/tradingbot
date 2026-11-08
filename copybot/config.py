import os
from dataclasses import dataclass, field

from cryptography.fernet import Fernet
from dotenv import load_dotenv

PASSWORD_EXCHANGES = {"okx", "kucoin", "bitget"}  # te giełdy wymagają dodatkowo hasła (passphrase) do klucza API
EXCHANGE_NAMES = {"binance": "Binance", "bybit": "Bybit", "okx": "OKX", "kucoin": "KuCoin", "bitget": "Bitget",
                  "gate": "Gate.io", "mexc": "MEXC", "htx": "HTX", "kraken": "Kraken", "coinbase": "Coinbase"}
DEFAULT_EXCHANGES = ("binance", "bybit", "okx", "coinbase", "kucoin", "bitget", "gate", "mexc")
# podpowiedzi, jak wygląda klucz na danej giełdzie (pokazywane przy podłączaniu)
KEY_HINTS = {
    "coinbase": ("Coinbase: Settings → API → Create API key (Coinbase Developer Platform). Uprawnienia: "
                 "<b>View</b> + <b>Trade</b>, bez Transfer. Pasuje każdy typ klucza:\n"
                 "• <b>Ed25519</b> (domyślny): API Key = <b>Key ID</b> (np. <code>d2efa49a-…</code>), "
                 "API Secret = <b>Secret</b> (długi ciąg zakończony na <code>==</code>).\n"
                 "• <b>ECDSA</b>: API Key = <code>organizations/…/apiKeys/…</code>, API Secret = cały Private Key "
                 "od <code>-----BEGIN EC PRIVATE KEY-----</code> do <code>-----END EC PRIVATE KEY-----</code>.\n"
                 "Jeśli ustawiłeś listę dozwolonych IP, dopisz IP serwera bota."),
}


def normalize_secret(text: str) -> str:
    """Porządkuje sekret wklejony w Telegramie: spacje, dosłowne „\\n” albo PEM sklejony w jedną linię."""
    import re
    text = text.strip().strip('"').strip("'").strip()
    if "-----BEGIN" not in text:
        return re.sub(r"\s+", "", text)  # zwykły sekret nie ma spacji ani nowych linii
    text = text.replace("\\n", "\n")
    m = re.match(r"(-----BEGIN [A-Z ]+-----)(.*?)(-----END [A-Z ]+-----)", text, re.S)
    if not m:
        return text
    body = re.sub(r"\s+", "", m.group(2))
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return "\n".join([m.group(1), *lines, m.group(3)]) + "\n"


def friendly_error(e: Exception) -> str:
    """Zrozumiały komunikat zamiast technicznego błędu biblioteki giełdy."""
    import ccxt
    msg = str(e)
    if isinstance(e, ccxt.AuthenticationError) or "401" in msg or "Unauthorized" in msg:
        return ("giełda odrzuciła klucz. Sprawdź, czy skopiowałeś cały klucz i sekret, czy klucz jest aktywny, "
                "ma uprawnienie do odczytu/handlu i czy nie ma ograniczenia IP")
    if isinstance(e, ccxt.PermissionDenied):
        return "klucz nie ma wymaganych uprawnień (potrzebny odczyt salda i handel spot)"
    if "base16" in msg or "PEM" in msg or "Could not deserialize" in msg or "Incorrect padding" in msg:
        return "zły format sekretu – wklej go w całości, dokładnie tak, jak pokazała giełda"
    if isinstance(e, (ccxt.NetworkError, TimeoutError)):
        return "giełda nie odpowiada – spróbuj ponownie za chwilę"
    return msg[:200]


def exchange_name(ex_id: str) -> str:
    return EXCHANGE_NAMES.get(ex_id, ex_id.capitalize())


def needs_password(ex_id: str) -> bool:
    return ex_id in PASSWORD_EXCHANGES


def _bool(v: str | None, default=False) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "tak"}


@dataclass(frozen=True)
class Config:
    bot_token: str
    encryption_key: str
    admin_ids: frozenset[int] = frozenset()
    exchange_id: str = "binance"  # domyślna giełda (ceny w trybie demo, konto bez wybranej giełdy)
    exchanges: tuple[str, ...] = DEFAULT_EXCHANGES  # giełdy do wyboru dla użytkowników i traderów
    sandbox: bool = False
    leader_api_key: str = ""
    leader_api_secret: str = ""
    leader_api_password: str = ""
    leader_symbols: tuple[str, ...] = field(default_factory=tuple)
    quote: str = "USDT"
    poll_interval: int = 15
    paper_start: float = 1000.0
    db_path: str = "copybot.db"
    snapshot_minutes: int = 15
    realtime: bool = True  # WebSocket giełdy zamiast odpytywania
    daily_report_hour: int = 20
    timezone: str = "Europe/Warsaw"
    # Panel (Telegram Mini App): publiczny adres HTTPS, pod którym widać serwer www bota
    webapp_url: str = ""
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    # Subskrypcja w Telegram Stars: 0 = bot darmowy
    sub_price_stars: int = 0
    sub_scope: str = "live"  # "live" = płatne tylko kopiowanie na prawdziwych środkach, "all" = również demo

    @property
    def leader_enabled(self) -> bool:
        return bool(self.leader_api_key and self.leader_api_secret)

    @property
    def needs_password(self) -> bool:
        return self.exchange_id in PASSWORD_EXCHANGES

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "")
        if not token:
            raise SystemExit("BRAK BOT_TOKEN. Na Railway: zakładka Variables → dodaj BOT_TOKEN (token od @BotFather) "
                             "i kliknij Deploy. Na własnym serwerze: wpisz BOT_TOKEN w pliku .env.")
        db_path = os.getenv("DB_PATH", "copybot.db")
        key = os.getenv("ENCRYPTION_KEY", "") or load_or_create_key(db_path)
        return cls(
            bot_token=token,
            encryption_key=key,
            admin_ids=frozenset(int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()),
            exchange_id=(default_ex := os.getenv("EXCHANGE", "binance").strip().lower()),
            exchanges=_exchanges(os.getenv("EXCHANGES", ""), default_ex),
            sandbox=_bool(os.getenv("SANDBOX")),
            leader_api_key=os.getenv("LEADER_API_KEY", ""),
            leader_api_secret=os.getenv("LEADER_API_SECRET", ""),
            leader_api_password=os.getenv("LEADER_API_PASSWORD", ""),
            leader_symbols=tuple(s.strip().upper() for s in os.getenv("LEADER_SYMBOLS", "").split(",") if s.strip()),
            quote=os.getenv("QUOTE", "USDT").upper(),
            poll_interval=int(os.getenv("POLL_INTERVAL", "15")),
            paper_start=float(os.getenv("PAPER_START", "1000")),
            db_path=db_path,
            snapshot_minutes=int(os.getenv("SNAPSHOT_MINUTES", "15")),
            realtime=_bool(os.getenv("REALTIME"), True),
            daily_report_hour=int(os.getenv("DAILY_REPORT_HOUR", "20")),
            timezone=os.getenv("TIMEZONE", "Europe/Warsaw"),
            # Telegram otwiera Mini App tylko przez HTTPS
            webapp_url=_webapp_url(),
            # hostingi typu Railway/Render podają port w zmiennej PORT i wymagają nasłuchu na 0.0.0.0
            web_host=os.getenv("WEB_HOST") or ("0.0.0.0" if os.getenv("PORT") else "127.0.0.1"),
            web_port=int(os.getenv("WEB_PORT") or os.getenv("PORT") or "8080"),
            sub_price_stars=int(os.getenv("SUB_PRICE_STARS", "0")),
            sub_scope=os.getenv("SUB_SCOPE", "live").strip().lower(),
        )


def _webapp_url() -> str:
    """Publiczny adres panelu. Railway podaje swoją domenę sam (RAILWAY_PUBLIC_DOMAIN)."""
    url = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
    if not url and os.getenv("RAILWAY_PUBLIC_DOMAIN"):
        url = "https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"]
    return url if url.startswith("https://") else ""  # Telegram otwiera Mini App tylko przez HTTPS


def _exchanges(raw: str, default_ex: str) -> tuple[str, ...]:
    import ccxt
    wanted = [x.strip().lower() for x in raw.split(",") if x.strip()] or list(DEFAULT_EXCHANGES)
    if default_ex not in wanted:
        wanted.insert(0, default_ex)
    return tuple(x for x in dict.fromkeys(wanted) if x in ccxt.exchanges)


def load_or_create_key(db_path: str) -> str:
    """Bez ENCRYPTION_KEY w .env generujemy klucz raz i trzymamy go obok bazy."""
    path = os.path.join(os.path.dirname(os.path.abspath(db_path)), "secret.key")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    key = Fernet.generate_key().decode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    return key
