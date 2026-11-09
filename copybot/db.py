import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,
    username TEXT,
    accepted INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 0,
    paper INTEGER NOT NULL DEFAULT 1,
    multiplier REAL NOT NULL DEFAULT 1.0,
    max_trade REAL NOT NULL DEFAULT 50,
    api_key TEXT, api_secret TEXT, api_password TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_balances (
    tg_id INTEGER NOT NULL, asset TEXT NOT NULL, amount REAL NOT NULL,
    PRIMARY KEY (tg_id, asset)
);
CREATE TABLE IF NOT EXISTS copies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    leader_order TEXT NOT NULL, tg_id INTEGER NOT NULL,
    symbol TEXT, side TEXT, amount REAL, price REAL, cost REAL,
    status TEXT NOT NULL, info TEXT, paper INTEGER NOT NULL, ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS leader_trades (id TEXT PRIMARY KEY, ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS equity (
    tg_id INTEGER NOT NULL, ts INTEGER NOT NULL, value REAL NOT NULL, paper INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS equity_idx ON equity (tg_id, paper, ts);
CREATE TABLE IF NOT EXISTS leaders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, bio TEXT, owner_tg_id INTEGER,
    source TEXT NOT NULL,                    -- api = konto na giełdzie (klucz do odczytu), manual = sygnały z bota
    exchange TEXT,                           -- id giełdy z ccxt (NULL = domyślna)
    api_key TEXT, api_secret TEXT, api_password TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | active | hidden
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS follows (
    tg_id INTEGER NOT NULL, leader_id INTEGER NOT NULL,
    copy INTEGER NOT NULL DEFAULT 1,         -- 1 = kopiuj transakcje, 0 = tylko powiadomienia
    created_at INTEGER NOT NULL,
    PRIMARY KEY (tg_id, leader_id)
);
CREATE TABLE IF NOT EXISTS sol_wallets (
    tg_id INTEGER PRIMARY KEY, pubkey TEXT NOT NULL, secret TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sol_paper (tg_id INTEGER NOT NULL, mint TEXT NOT NULL, raw INTEGER NOT NULL,
                                      PRIMARY KEY (tg_id, mint));
CREATE TABLE IF NOT EXISTS sol_tokens (mint TEXT PRIMARY KEY, symbol TEXT NOT NULL, decimals INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sol_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER NOT NULL, leader_id INTEGER, leader_sig TEXT,
    side TEXT NOT NULL, mint TEXT NOT NULL, sol REAL, tokens REAL, status TEXT NOT NULL, info TEXT,
    paper INTEGER NOT NULL, sig TEXT, latency_ms INTEGER, ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER NOT NULL, charge_id TEXT NOT NULL UNIQUE,
    amount INTEGER NOT NULL, recurring INTEGER NOT NULL DEFAULT 0, refunded INTEGER NOT NULL DEFAULT 0,
    ts INTEGER NOT NULL
);
"""

# kolumny dodane w kolejnych wersjach – dopisywane do istniejącej bazy
MIGRATIONS = {
    "stop_loss": "REAL NOT NULL DEFAULT 0",
    "start_equity": "REAL",
    "paid_until": "INTEGER NOT NULL DEFAULT 0",
    "leader_id": "INTEGER",
    "max_price_move": "REAL NOT NULL DEFAULT 2",  # % – nie kupuj, jeśli cena uciekła bardziej od ceny lidera
    "daily_report": "INTEGER NOT NULL DEFAULT 1",
    "exchange": "TEXT",  # giełda użytkownika (NULL = domyślna z .env)
    "approved": "INTEGER NOT NULL DEFAULT 0",  # bot prywatny: dostęp tylko dla wpuszczonych
    "requested": "INTEGER NOT NULL DEFAULT 0",  # czy właściciel dostał już prośbę o dostęp
    "copy_mode": "TEXT NOT NULL DEFAULT 'trades'",  # trades = kopiuj transakcje, mirror = lustro portfela 1:1
    "mirror_tolerance": "REAL NOT NULL DEFAULT 2",  # % – mniejszych odchyleń lustro nie wyrównuje (oszczędza prowizje)
    "mirror_trades": "INTEGER NOT NULL DEFAULT 1",
    "live_pin": "INTEGER NOT NULL DEFAULT 1",  # przypięta wiadomość „na żywo” u góry czatu
    "pin_msg_id": "INTEGER",  # id tej wiadomości (do edycji co minutę)
    # Solana
    "sol_live": "INTEGER NOT NULL DEFAULT 0",  # 0 = demo (wirtualne SOL), 1 = prawdziwy portfel
    "sol_buy_mode": "TEXT NOT NULL DEFAULT 'fixed'",  # fixed = stała kwota SOL, ratio = ten sam % SOL co trader
    "sol_buy": "REAL NOT NULL DEFAULT 0.05",
    "sol_mult": "REAL NOT NULL DEFAULT 1.0",
    "sol_max": "REAL NOT NULL DEFAULT 0.5",
    "sol_slippage_bps": "INTEGER NOT NULL DEFAULT 1000",
    "sol_presets": "TEXT NOT NULL DEFAULT '0.05,0.1,0.5,1'",  # szybkie kwoty zakupu na karcie tokena
    "sol_tp": "REAL NOT NULL DEFAULT 0",  # auto take-profit w % (0 = wył.)
    "sol_sl": "REAL NOT NULL DEFAULT 0",  # auto stop-loss w % (0 = wył.)
    # sesja = od włączenia do wyłączenia kopiowania – po niej bot wysyła szczegółowy raport
    "session_start": "INTEGER",
    "session_end": "INTEGER",
    "session_sol0": "REAL",  # wartość portfela Solany na starcie sesji (SOL)
    "session_cex0": "REAL",  # wartość portfela giełdowego na starcie sesji  # w lustrze kopiuj też każdą transakcję od razu (1) czy tylko wyrównuj (0)
}


def ref_id(leader_id: int) -> int:
    """Wirtualny „portfel wzorcowy” tradera (ujemne id): kopiuje każde jego zlecenie 1:1, bez limitów.
    Z niego liczymy publiczne wyniki, których nie da się podkręcić."""
    return -int(leader_id)

USER_FIELDS = {"username", "accepted", "active", "paper", "multiplier", "max_trade",
               "api_key", "api_secret", "api_password", "stop_loss", "start_equity", "paid_until",
               "leader_id", "max_price_move", "daily_report", "exchange", "approved", "requested",
               "copy_mode", "mirror_tolerance", "mirror_trades", "live_pin", "pin_msg_id",
               "sol_live", "sol_buy_mode", "sol_buy", "sol_mult", "sol_max", "sol_slippage_bps",
               "sol_presets", "sol_tp", "sol_sl", "session_start", "session_end", "session_sol0", "session_cex0"}
LEADER_MIGRATIONS = {"wallet": "TEXT"}
FOLLOW_MIGRATIONS = {"sol_buy": "REAL"}  # osobna kwota zakupu (SOL) dla konkretnego kopiowanego portfela  # adres portfela Solany (traderzy typu „sol”)
LEADER_FIELDS = {"name", "bio", "owner_tg_id", "source", "api_key", "api_secret", "api_password", "status",
                 "exchange", "wallet"}


def now_ms() -> int:
    return int(time.time() * 1000)


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.conn:
            self.conn.executescript(SCHEMA)
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(users)")}
            for name, ddl in MIGRATIONS.items():
                if name not in cols:
                    self.conn.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
                    if name == "approved":  # użytkownicy sprzed wersji prywatnej zachowują dostęp
                        self.conn.execute("UPDATE users SET approved = 1 WHERE accepted = 1")
            fcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(follows)")}
            for name, ddl in FOLLOW_MIGRATIONS.items():
                if name not in fcols:
                    self.conn.execute(f"ALTER TABLE follows ADD COLUMN {name} {ddl}")
            lcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(leaders)")}
            for name, ddl in LEADER_MIGRATIONS.items():
                if name not in lcols:
                    self.conn.execute(f"ALTER TABLE leaders ADD COLUMN {name} {ddl}")
            # wersja 2 miała jednego wybranego tradera (users.leader_id) – przenosimy do listy śledzonych
            self.conn.execute("""INSERT OR IGNORE INTO follows (tg_id, leader_id, copy, created_at)
                                 SELECT tg_id, leader_id, 1, created_at FROM users WHERE leader_id IS NOT NULL""")
            self.conn.execute("UPDATE users SET leader_id = NULL WHERE leader_id IS NOT NULL")

    def _q(self, sql, args=()):
        with self.lock, self.conn:
            return self.conn.execute(sql, args).fetchall()

    # --- użytkownicy ---
    def ensure_user(self, tg_id: int, username: str | None, paper_start: float, quote: str) -> sqlite3.Row:
        with self.lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO users (tg_id, username, created_at) VALUES (?, ?, ?)",
                (tg_id, username, now_ms()))
            if cur.rowcount:
                self.conn.execute("INSERT INTO paper_balances VALUES (?, ?, ?)", (tg_id, quote, paper_start))
        return self.get_user(tg_id)

    def get_user(self, tg_id: int) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
        return rows[0] if rows else None

    def update_user(self, tg_id: int, **fields):
        bad = set(fields) - USER_FIELDS
        if bad:
            raise ValueError(f"Nieznane pola: {bad}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._q(f"UPDATE users SET {sets} WHERE tg_id = ?", (*fields.values(), tg_id))

    def active_users(self) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM users WHERE active = 1 AND accepted = 1 AND approved = 1")

    def active_followers(self, leader_id: int) -> list[sqlite3.Row]:
        """Kto kopiuje tradera teraz: śledzi go z kopiowaniem i ma włączone kopiowanie."""
        return self._q("""SELECT u.* FROM users u JOIN follows f ON f.tg_id = u.tg_id
                          WHERE f.leader_id = ? AND f.copy = 1 AND u.active = 1 AND u.accepted = 1
                          AND u.approved = 1""", (leader_id,))

    def mirror_followers(self, leader_id: int | None = None) -> list[sqlite3.Row]:
        """Kopiujący w trybie lustra (wszyscy albo konkretnego tradera)."""
        if leader_id is None:
            return self._q("""SELECT * FROM users WHERE active = 1 AND accepted = 1 AND approved = 1
                              AND copy_mode = 'mirror'""")
        return self._q("""SELECT u.* FROM users u JOIN follows f ON f.tg_id = u.tg_id
                          WHERE f.leader_id = ? AND f.copy = 1 AND u.active = 1 AND u.accepted = 1
                          AND u.approved = 1 AND u.copy_mode = 'mirror'""", (leader_id,))

    def watchers(self, leader_id: int) -> list[sqlite3.Row]:
        """Kto dostaje tylko powiadomienie: śledzi bez kopiowania albo ma kopiowanie wyłączone."""
        return self._q("""SELECT u.* FROM users u JOIN follows f ON f.tg_id = u.tg_id
                          WHERE f.leader_id = ? AND u.accepted = 1 AND u.approved = 1
                          AND (f.copy = 0 OR u.active = 0)""", (leader_id,))

    def follower_counts(self) -> dict[int, tuple[int, int]]:
        """leader_id -> (śledzący, kopiujący)"""
        return {r["leader_id"]: (r["n"], r["c"] or 0) for r in self._q(
            "SELECT leader_id, COUNT(*) n, SUM(copy) c FROM follows GROUP BY leader_id")}

    # --- śledzeni traderzy ---
    def follow(self, tg_id: int, leader_id: int, copy: bool):
        self._q("""INSERT INTO follows (tg_id, leader_id, copy, created_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(tg_id, leader_id) DO UPDATE SET copy = excluded.copy""",
                (tg_id, leader_id, int(copy), now_ms()))

    def follow_row(self, tg_id: int, leader_id: int):
        rows = self._q("SELECT * FROM follows WHERE tg_id = ? AND leader_id = ?", (tg_id, leader_id))
        return rows[0] if rows else None

    def set_follow_amount(self, tg_id: int, leader_id: int, sol: float | None):
        self._q("UPDATE follows SET sol_buy = ? WHERE tg_id = ? AND leader_id = ?", (sol, tg_id, leader_id))

    def unfollow(self, tg_id: int, leader_id: int):
        self._q("DELETE FROM follows WHERE tg_id = ? AND leader_id = ?", (tg_id, leader_id))

    def follows_of(self, tg_id: int) -> dict[int, bool]:
        """leader_id -> czy kopiuje (tylko aktywni traderzy)"""
        return {r["leader_id"]: bool(r["copy"]) for r in self._q(
            """SELECT f.leader_id, f.copy FROM follows f JOIN leaders l ON l.id = f.leader_id
               WHERE f.tg_id = ? AND l.status = 'active' ORDER BY f.created_at""", (tg_id,))}

    def copied_leaders(self, tg_id: int) -> list[sqlite3.Row]:
        return self._q("""SELECT l.* FROM follows f JOIN leaders l ON l.id = f.leader_id
                          WHERE f.tg_id = ? AND f.copy = 1 AND l.status = 'active' ORDER BY f.created_at""", (tg_id,))

    def delete_leader(self, leader_id: int):
        with self.lock, self.conn:
            self.conn.execute("UPDATE leaders SET status = 'deleted', api_key = NULL, api_secret = NULL, "
                              "api_password = NULL WHERE id = ?", (leader_id,))
            self.conn.execute("DELETE FROM follows WHERE leader_id = ?", (leader_id,))

    # --- dostęp (bot prywatny) ---
    def live_pin_users(self) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM users WHERE approved = 1 AND accepted = 1 AND live_pin = 1")

    def approved_users(self) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM users WHERE approved = 1 ORDER BY created_at")

    # --- traderzy ---
    def create_leader(self, name: str, source: str, owner_tg_id: int | None, status: str = "pending",
                      bio: str | None = None, **keys) -> int:
        with self.lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO leaders (name, bio, owner_tg_id, source, exchange, api_key, api_secret, api_password, "
                "status, created_at, wallet) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, bio, owner_tg_id, source, keys.get("exchange"), keys.get("api_key"), keys.get("api_secret"),
                 keys.get("api_password"), status, now_ms(), keys.get("wallet")))
            return cur.lastrowid

    def get_leader(self, leader_id: int | None) -> sqlite3.Row | None:
        if leader_id is None:
            return None
        rows = self._q("SELECT * FROM leaders WHERE id = ?", (leader_id,))
        return rows[0] if rows else None

    def leaders(self, status: str | None = "active") -> list[sqlite3.Row]:
        if status is None:
            return self._q("SELECT * FROM leaders WHERE status != 'deleted' ORDER BY id")
        return self._q("SELECT * FROM leaders WHERE status = ? ORDER BY id", (status,))

    def leaders_owned(self, tg_id: int) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM leaders WHERE owner_tg_id = ? AND status != 'deleted' ORDER BY id", (tg_id,))

    def update_leader(self, leader_id: int, **fields):
        if bad := set(fields) - LEADER_FIELDS:
            raise ValueError(f"Nieznane pola: {bad}")
        self._q(f"UPDATE leaders SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                (*fields.values(), leader_id))

    def migrate_v2(self, make_first_leader) -> int | None:
        """Wersja 1 miała jednego lidera (portfel wzorcowy tg_id=0). Przenosimy go jako tradera #1."""
        if self._q("SELECT 1 FROM leaders LIMIT 1"):
            return None
        if not self._q("SELECT 1 FROM users LIMIT 1") and self.get_state("reference_start") is None:
            return None  # świeża instalacja – nie ma czego przenosić
        lid = make_first_leader()
        with self.lock, self.conn:
            for table in ("paper_balances", "equity", "copies"):
                self.conn.execute(f"UPDATE {table} SET tg_id = ? WHERE tg_id = 0", (ref_id(lid),))
            old = self.conn.execute("SELECT value FROM state WHERE key = 'reference_start'").fetchone()
            if old:
                self.conn.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (f"reference_start:{lid}", old[0]))
            since = self.conn.execute("SELECT value FROM state WHERE key = 'leader_since'").fetchone()
            if since:
                self.conn.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (f"leader_since:{lid}", since[0]))
            self.conn.execute("INSERT OR IGNORE INTO follows (tg_id, leader_id, copy, created_at) "
                              "SELECT tg_id, ?, 1, created_at FROM users", (lid,))
        return lid

    def all_user_ids(self) -> list[int]:
        return [r["tg_id"] for r in self._q("SELECT tg_id FROM users WHERE approved = 1")]

    def stats(self) -> dict:
        r = self._q("""SELECT COUNT(*) total, SUM(active) active,
                       SUM(CASE WHEN active = 1 AND paper = 0 THEN 1 ELSE 0 END) live,
                       SUM(CASE WHEN paid_until > ? THEN 1 ELSE 0 END) subs FROM users""", (now_ms(),))[0]
        p = self._q("SELECT COALESCE(SUM(amount), 0) stars FROM payments WHERE refunded = 0")[0]
        return {"total": r["total"] or 0, "active": r["active"] or 0, "live": r["live"] or 0,
                "subs": r["subs"] or 0, "stars": p["stars"]}

    # --- portfel wzorcowy ---
    def ensure_reference(self, leader_id: int, paper_start: float, quote: str):
        if self.get_state(f"reference_start:{leader_id}") is None:
            self.paper_reset(ref_id(leader_id), paper_start, quote)
            self.set_state(f"reference_start:{leader_id}", str(now_ms()))

    # --- historia wartości portfela ---
    def add_equity(self, tg_id: int, value: float, paper: bool, ts: int | None = None):
        self._q("INSERT INTO equity VALUES (?, ?, ?, ?)", (tg_id, ts or now_ms(), value, int(paper)))

    def equity_series(self, tg_id: int, paper: bool, since: int = 0) -> list[tuple[int, float]]:
        return [(r["ts"], r["value"]) for r in self._q(
            "SELECT ts, value FROM equity WHERE tg_id = ? AND paper = ? AND ts >= ? ORDER BY ts",
            (tg_id, int(paper), since))]

    def fills(self, tg_id: int) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT symbol, side, amount, price, cost, ts FROM copies WHERE tg_id = ? AND status = 'filled' ORDER BY id",
            (tg_id,))]

    def traded_symbols(self, tg_id: int) -> list[str]:
        return [r["symbol"] for r in self._q(
            "SELECT DISTINCT symbol FROM copies WHERE tg_id = ? AND status = 'filled'", (tg_id,))]

    # --- płatności ---
    def add_payment(self, tg_id: int, charge_id: str, amount: int, recurring: bool, paid_until: int):
        with self.lock, self.conn:
            self.conn.execute("INSERT OR IGNORE INTO payments (tg_id, charge_id, amount, recurring, ts) "
                              "VALUES (?, ?, ?, ?, ?)", (tg_id, charge_id, amount, int(recurring), now_ms()))
            self.conn.execute("UPDATE users SET paid_until = MAX(paid_until, ?) WHERE tg_id = ?", (paid_until, tg_id))

    def last_payment(self, tg_id: int) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM payments WHERE tg_id = ? AND refunded = 0 ORDER BY id DESC LIMIT 1", (tg_id,))
        return rows[0] if rows else None

    def mark_refunded(self, payment_id: int, tg_id: int):
        with self.lock, self.conn:
            self.conn.execute("UPDATE payments SET refunded = 1 WHERE id = ?", (payment_id,))
            self.conn.execute("UPDATE users SET paid_until = 0 WHERE tg_id = ?", (tg_id,))

    # --- konto demo ---
    def paper_balances(self, tg_id: int) -> dict[str, float]:
        return {r["asset"]: r["amount"] for r in
                self._q("SELECT asset, amount FROM paper_balances WHERE tg_id = ? AND amount > 1e-12", (tg_id,))}

    def paper_apply(self, tg_id: int, deltas: dict[str, float]):
        with self.lock, self.conn:
            for asset, d in deltas.items():
                self.conn.execute(
                    """INSERT INTO paper_balances VALUES (?, ?, ?)
                       ON CONFLICT(tg_id, asset) DO UPDATE SET amount = amount + excluded.amount""",
                    (tg_id, asset, d))

    def paper_reset(self, tg_id: int, paper_start: float, quote: str):
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM paper_balances WHERE tg_id = ?", (tg_id,))
            self.conn.execute("DELETE FROM equity WHERE tg_id = ? AND paper = 1", (tg_id,))
            self.conn.execute("DELETE FROM copies WHERE tg_id = ? AND paper = 1", (tg_id,))
            self.conn.execute("INSERT INTO paper_balances VALUES (?, ?, ?)", (tg_id, quote, paper_start))

    # --- historia ---
    def record_copy(self, leader_order: str, tg_id: int, symbol: str, side: str, amount: float,
                    price: float, cost: float, status: str, info: str | None, paper: bool):
        self._q("""INSERT INTO copies (leader_order, tg_id, symbol, side, amount, price, cost, status, info, paper, ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (leader_order, tg_id, symbol, side, amount, price, cost, status, info, int(paper), now_ms()))

    def history(self, tg_id: int, limit: int = 10) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM copies WHERE tg_id = ? ORDER BY id DESC LIMIT ?", (tg_id, limit))

    # --- stan lidera ---
    def get_state(self, key: str) -> str | None:
        rows = self._q("SELECT value FROM state WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_state(self, key: str, value: str):
        self._q("INSERT INTO state VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def unseen_trades(self, leader_id: int, trades: list[dict]) -> list[dict]:
        if not trades:
            return []
        keys = [f"{leader_id}:{t['id']}" for t in trades]
        legacy = [str(t["id"]) for t in trades]  # klucze z wersji 1 (bez numeru tradera)
        marks = ",".join("?" * (len(keys) * 2))
        seen = {r["id"] for r in self._q(f"SELECT id FROM leader_trades WHERE id IN ({marks})", keys + legacy)}
        return [t for t, k, old in zip(trades, keys, legacy) if k not in seen and old not in seen]

    def mark_trades(self, leader_id: int, trades: list[dict]):
        with self.lock, self.conn:
            self.conn.executemany("INSERT OR IGNORE INTO leader_trades VALUES (?, ?)",
                                  [(f"{leader_id}:{t['id']}", int(t["timestamp"])) for t in trades])
