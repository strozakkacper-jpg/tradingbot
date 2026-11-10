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
    mirror_minutes: int = 5
    # --- Solana (kopiowanie portfeli, np. traderów z Axiom / Pump.fun) ---
    sol_enabled: bool = True
    helius_api_key: str = ""  # Helius: szybki RPC + WebSocket z pełnymi transakcjami
    jupiter_api_key: str = ""  # Jupiter: wymiany; bez klucza – darmowe lite-api (niższe limity)
    sol_rpc_url: str = ""  # nadpisanie RPC (domyślnie Helius albo publiczny)
    sol_paper_start: float = 5.0  # wirtualne SOL na start w trybie demo
    sol_priority_max_lamports: int = 2_000_000  # maks. 0,002 SOL opłaty priorytetowej (szybsze wejście)
    sol_reserve: float = 0.01  # SOL zostawiane na opłaty sieci
    # tryb turbo: Jito Block Engine najbliżej serwera (Railway europe-west4 = Holandia → Amsterdam)
    jito_url: str = "https://amsterdam.mainnet.block-engine.jito.wtf/api/v1/transactions"
    sol_rpc_rps: float = 9.0  # limit zapytań RPC na sekundę (darmowy Helius ~10/s; płatny – ustaw więcej)
    sol_max_delay_s: float = 10.0  # kopie spóźnione bardziej niż tyle sekund są pomijane (cena mogła uciec)
    live_seconds: int = 60  # co ile sekund odświeżać przypiętą wiadomość „na żywo”
    live_coins: tuple[str, ...] = ("BTC", "ETH", "SOL")  # ceny pokazywane u góry
    demo_coins: tuple[str, ...] = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB")  # szybki wybór w handlu demo  # co ile minut lustro sprawdza zgodność portfeli (poza reakcją na ruch tradera)
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
    def sol_rpc(self) -> str:
        if self.sol_rpc_url:
            return self.sol_rpc_url
        if self.helius_api_key:
            return f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"
        return "https://api.mainnet-beta.solana.com"

    @property
    def sol_ws(self) -> str:
        return self.sol_rpc.replace("https://", "wss://").replace("http://", "ws://")

    @property
    def sol_ws_enhanced(self) -> str:
        """Helius Enhanced WebSocket (transactionSubscribe) – pełna transakcja w powiadomieniu."""
        return f"wss://atlas-mainnet.helius-rpc.com/?api-key={self.helius_api_key}" if self.helius_api_key else ""

    @property
    def jup_base(self) -> str:
        return "https://api.jup.ag" if self.jupiter_api_key else "https://lite-api.jup.ag"

    @property
    def needs_password(self) -> bool:
        return self.exchange_id in PASSWORD_EXCHANGES

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "")
        if not token or token.startswith("123456:"):
            raise SystemExit("BRAK BOT_TOKEN. Na Railway: zakładka Variables → dodaj BOT_TOKEN (token od @BotFather) "
                             "i kliknij Deploy. Na własnym serwerze: wpisz BOT_TOKEN w pliku .env.")
        db_path = _db_path()
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
            mirror_minutes=int(os.getenv("MIRROR_MINUTES", "5")),
            live_seconds=max(30, int(os.getenv("LIVE_SECONDS", "60"))),
            sol_enabled=_bool(os.getenv("SOL_ENABLED"), True),
            helius_api_key=os.getenv("HELIUS_API_KEY", "").strip(),
            jupiter_api_key=os.getenv("JUPITER_API_KEY", "").strip(),
            sol_rpc_url=os.getenv("SOL_RPC_URL", "").strip(),
            sol_paper_start=float(os.getenv("SOL_PAPER_START", "5")),
            sol_max_delay_s=float(os.getenv("SOL_MAX_DELAY_S", "10")),
            sol_rpc_rps=float(os.getenv("SOL_RPC_RPS", "9")),
            sol_priority_max_lamports=int(os.getenv("SOL_PRIORITY_MAX_LAMPORTS", "2000000")),
            jito_url=os.getenv("JITO_URL", "https://amsterdam.mainnet.block-engine.jito.wtf/api/v1/transactions"),
            live_coins=tuple(c.strip().upper() for c in os.getenv("LIVE_COINS", "BTC,ETH,SOL").split(",") if c.strip()),
            demo_coins=tuple(c.strip().upper() for c in os.getenv("DEMO_COINS", "BTC,ETH,SOL,XRP,DOGE,BNB").split(",")
                             if c.strip()),
            daily_report_hour=int(os.getenv("DAILY_REPORT_HOUR", "20")),
            timezone=os.getenv("TIMEZONE", "Europe/Warsaw"),
            # Telegram otwiera Mini App tylko przez HTTPS
            webapp_url=_webapp_url(),
            # hostingi typu Railway/Render podają port w zmiennej PORT i wymagają nasłuchu na 0.0.0.0
            web_host=os.getenv("WEB_HOST") or ("0.0.0.0" if os.getenv("PORT") else "127.0.0.1"),
            web_port=int(os.getenv("PORT") or os.getenv("WEB_PORT") or "8080"),  # PORT od hostingu ma pierwszeństwo
            sub_price_stars=int(os.getenv("SUB_PRICE_STARS", "0")),
            sub_scope=os.getenv("SUB_SCOPE", "live").strip().lower(),
        )


def _db_path() -> str:
    """Na hostingu z trwałym dyskiem (/data) baza MUSI leżeć na nim – inaczej każda aktualizacja ją kasuje."""
    path = os.getenv("DB_PATH", "").strip() or "copybot.db"
    if os.path.isdir("/data") and not path.startswith("/data/"):
        path = os.path.join("/data", os.path.basename(path))
    return path


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
