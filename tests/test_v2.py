"""Wersja 2: wielu traderów, wiele giełd, czas rzeczywisty, ochrona ceny, migracja."""
import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet

from copybot import bot as B
from copybot import traders as T
from copybot.config import Config
from copybot.db import DB, now_ms, ref_id
from copybot.engine import CopyEngine, Markets
from copybot.reports import ranking
from copybot.security import Vault
from copybot.sizing import LeaderOrder
from copybot.watchers import LeaderWatcher


class FakeMarket:
    def __init__(self, price=100.0, symbols=("BTC/USDT", "ETH/USDT"), exchange_id="binance"):
        self.p, self.symbols, self.exchange_id = price, set(symbols), exchange_id
        self.ex = SimpleNamespace(markets={s: {} for s in symbols}, currencies={})

    async def load(self):
        pass

    async def price(self, symbol, max_age=10):
        return self.p

    async def min_cost(self, symbol):
        return 5.0

    async def has(self, symbol):
        return symbol in self.symbols

    async def equity(self, bal):
        return sum(v if k == "USDT" else v * self.p for k, v in bal.items())


def setup(tmp_path, markets=None):
    cfg = Config(bot_token="1:X", encryption_key=Fernet.generate_key().decode(), db_path=str(tmp_path / "t.db"))
    db = DB(cfg.db_path)
    sent = []

    async def notify(uid, text):
        sent.append((uid, text))

    eng = CopyEngine(cfg, db, Vault(cfg.encryption_key), markets or FakeMarket(), notify)
    s = B.Services(cfg, db, eng.vault, eng.market, eng, None)
    return s, sent


def add_user(s, uid, leader_id, **kw):
    s.db.ensure_user(uid, None, 1000, "USDT")
    s.db.update_user(uid, accepted=1, active=1, max_trade=10_000, leader_id=leader_id, **kw)


def order(side="buy", price=100.0, oid="A", symbol="BTC/USDT"):
    return LeaderOrder(oid, symbol, side, 0, 0, price, now_ms())


# ---------- wielu traderów ----------

def test_copy_goes_only_to_followers_of_that_trader(tmp_path):
    s, _ = setup(tmp_path)
    a = s.db.create_leader("Alfa", "manual", None, "active")
    b = s.db.create_leader("Beta", "manual", None, "active")
    for lid in (a, b):
        s.db.ensure_reference(lid, 1000, "USDT")
    add_user(s, 1, a)
    add_user(s, 2, b)
    res = asyncio.run(s.engine.copy(order(), 0.5, a))
    assert res == {"filled": 1}
    assert "BTC" in s.db.paper_balances(1) and "BTC" not in s.db.paper_balances(2)
    assert "BTC" in s.db.paper_balances(ref_id(a)) and "BTC" not in s.db.paper_balances(ref_id(b))


def test_ranking_orders_by_result(tmp_path):
    s, _ = setup(tmp_path)
    a = s.db.create_leader("Słaby", "manual", None, "active")
    b = s.db.create_leader("Mocny", "manual", None, "active")
    s.db.create_leader("Czeka", "manual", None, "pending")
    for lid in (a, b):
        s.db.ensure_reference(lid, 1000, "USDT")
    asyncio.run(s.engine.copy(order(), 1.0, a))
    asyncio.run(s.engine.copy(order(), 1.0, b))
    s.market.p = 150.0
    asyncio.run(s.engine.copy(order("sell", 150, "B"), 1.0, b))  # Mocny realizuje zysk
    s.market.p = 80.0  # Słaby trzyma i traci
    rows = asyncio.run(ranking(s))
    assert [r[0]["name"] for r in rows] == ["Mocny", "Słaby"]


# ---------- ochrona ceny ----------

def test_price_protection_skips_chasing(tmp_path):
    s, sent = setup(tmp_path, FakeMarket(price=103.0))
    lid = s.db.create_leader("T", "manual", None, "active")
    add_user(s, 1, lid, max_price_move=2)
    add_user(s, 2, lid, max_price_move=0)
    res = asyncio.run(s.engine.copy(order(price=100.0), 0.5, lid))
    assert res == {"skipped": 1, "filled": 1}
    assert any("cena uciekła" in t for uid, t in sent if uid == 1)


# ---------- wiele giełd ----------

def test_each_user_trades_on_own_exchange(tmp_path):
    cfg = Config(bot_token="1:X", encryption_key="x", db_path="x")
    hub = Markets(cfg)
    hub._m = {"binance": FakeMarket(100, exchange_id="binance"),
              "bybit": FakeMarket(101, exchange_id="bybit"),
              "mexc": FakeMarket(100, symbols=("ETH/USDT",), exchange_id="mexc")}
    s, sent = setup(tmp_path, hub)
    lid = s.db.create_leader("T", "api", None, "active", exchange="binance")
    add_user(s, 1, lid, exchange="bybit", max_price_move=5)
    add_user(s, 2, lid, exchange="mexc")
    add_user(s, 3, lid)  # domyślna giełda
    res = asyncio.run(s.engine.copy(order(), 0.1, lid))
    assert res == {"filled": 2, "skipped": 1}
    assert s.db.history(1)[0]["price"] == 101  # cena z Bybit
    assert any("niedostępna na MEXC" in t for uid, t in sent if uid == 2)


# ---------- obserwator w czasie rzeczywistym ----------

def trade(i, side, amount, price, ts, symbol="BTC/USDT", order_id=None):
    return {"id": str(i), "order": order_id or str(i), "symbol": symbol, "side": side, "amount": amount,
            "price": price, "cost": amount * price, "timestamp": ts}


def make_watcher(s, rest, pro=None):
    lid = s.db.create_leader("WS", "api", None, "active", api_key=s.vault.enc("k"), api_secret=s.vault.enc("s"))
    got = []

    async def on_orders(leader_id, pairs):
        got.append((leader_id, pairs))

    w = LeaderWatcher(s.cfg, s.db, s.vault, FakeMarket(), s.db.get_leader(lid), on_orders, rest=rest, pro=pro)
    return w, got


def test_watcher_processes_new_trades_once(tmp_path):
    s, _ = setup(tmp_path)
    rest = MagicMock()
    rest.fetch_balance = AsyncMock(return_value={"total": {"USDT": 800.0, "BTC": 2.0}})
    w, got = make_watcher(s, rest)
    t0 = w.start_ts
    trades = [trade(1, "buy", 1.0, 100, t0 + 1, order_id="X"), trade(2, "buy", 1.0, 100, t0 + 2, order_id="X"),
              trade(3, "buy", 5, 1, t0 + 3, symbol="ETH/BTC"),  # nie-USDT – pomijamy
              trade(4, "buy", 1.0, 100, t0 - 10)]  # sprzed startu – nie kopiujemy historii
    asyncio.run(w.process(trades))
    asyncio.run(w.process(trades))  # duplikaty z WebSocketu
    assert len(got) == 1
    (o, frac), = got[0][1]
    assert o.amount == 2.0 and frac == pytest.approx(0.2)  # 200 z 1000 USDT


def test_watcher_falls_back_to_polling_when_ws_dies(tmp_path):
    s, _ = setup(tmp_path)
    rest = MagicMock()
    rest.markets = {"BTC/USDT": {}}
    rest.fetch_balance = AsyncMock(return_value={"total": {"USDT": 900.0, "BTC": 1.0}})
    rest.fetch_my_trades = AsyncMock(return_value=[])
    pro = MagicMock()
    pro.has = {"watchMyTrades": True}
    calls = {"n": 0}

    async def watch():
        calls["n"] += 1
        if calls["n"] == 1:
            return [trade(9, "buy", 1.0, 100, w.start_ts + 5)]
        raise ConnectionError("rozłączono")

    pro.watch_my_trades = watch
    w, got = make_watcher(s, rest, pro)
    s.cfg.__dict__["poll_interval"] = 0.01

    async def go():
        task = asyncio.create_task(w.run())
        await asyncio.sleep(0.8)
        task.cancel()

    asyncio.run(go())
    assert len(got) == 1 and w.mode == "poll" and "rozłączono" in w.last_error
    assert rest.fetch_my_trades.await_count >= 2  # odpytywanie działa dalej


# ---------- migracja z wersji 1 ----------

def test_migration_from_v1(tmp_path):
    path = str(tmp_path / "old.db")
    db = DB(path)
    db.ensure_user(5, None, 1000, "USDT")
    db.paper_reset(0, 1000, "USDT")
    db.add_equity(0, 1234.0, True)
    db.set_state("reference_start", "111")
    db._q("INSERT INTO leader_trades VALUES ('T1', 1)")
    lid = db.migrate_v2(lambda: db.create_leader("Trader #1", "manual", None, "active"))
    assert lid == 1 and db.get_user(5)["leader_id"] == 1
    assert db.paper_balances(-1) == {"USDT": 1000} and db.paper_balances(0) == {}
    assert db.get_state("reference_start:1") == "111"
    assert db.unseen_trades(1, [{"id": "T1", "timestamp": 1}]) == []  # stary klucz nadal rozpoznany
    assert db.migrate_v2(lambda: 99) is None  # drugi raz nic nie robi


# ---------- interfejs: zgłoszenie tradera, zatwierdzenie, wybór ----------

def tg_update(uid, data=None, text=""):
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    msg.delete = AsyncMock()
    msg.text = text
    cq = None
    if data:
        cq = MagicMock()
        cq.data = data
        cq.answer = AsyncMock()
        cq.edit_message_text = AsyncMock()
    chat = MagicMock()
    chat.send_message = AsyncMock()
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid, username=None), callback_query=cq,
                           effective_message=msg, message=msg, effective_chat=chat)


def test_apply_approve_and_follow_flow(tmp_path):
    s, _ = setup(tmp_path)
    s.db.set_state("owner", "1")
    for uid in (1, 2):
        s.db.ensure_user(uid, None, 1000, "USDT")
        s.db.update_user(uid, accepted=1)
    bot = AsyncMock()
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"svc": s}), user_data={}, bot=bot, args=[])

    async def flow():
        assert await T.apply_start(tg_update(2), ctx) == T.A_NAME
        assert await T.apply_name(tg_update(2, text="Kacper Swing"), ctx) == T.A_BIO
        assert await T.apply_bio(tg_update(2, text="BTC swing"), ctx) == T.A_SOURCE
        await T.apply_source(tg_update(2, data="src:manual"), ctx)
        lead = s.db.leaders("pending")[0]
        assert lead["name"] == "Kacper Swing" and lead["owner_tg_id"] == 2
        assert bot.send_message.call_args.args[0] == 1  # admin powiadomiony
        await T.on_admin_button(tg_update(1, data=f"adm_ok:{lead['id']}"), ctx)
        assert s.db.get_leader(lead["id"])["status"] == "active"
        await T.on_admin_button(tg_update(2, data=f"adm_ok:{lead['id']}"), ctx)  # nie-admin: odmowa
        await T.on_trader_button(tg_update(1, data=f"follow:{lead['id']}"), ctx)
        assert s.db.get_user(1)["leader_id"] == lead["id"]
        u = tg_update(1, data="traders")
        await T.on_trader_button(u, ctx)
        assert "Kacper Swing" in u.callback_query.edit_message_text.call_args.args[0]
        # trader wysyła sygnał
        s.db.update_user(1, active=1)
        ctx.args = ["buy", "BTC/USDT", "10"]
        u = tg_update(2)
        await T.cmd_signal(u, ctx)
        assert "wysłany" in u.message.reply_text.call_args.args[0]
        assert "BTC" in s.db.paper_balances(1)

    asyncio.run(flow())


def test_toggle_requires_trader(tmp_path):
    s, _ = setup(tmp_path)
    s.db.ensure_user(1, None, 1000, "USDT")
    s.db.update_user(1, accepted=1)
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"svc": s}), user_data={}, bot=AsyncMock())
    u = tg_update(1, data="toggle")
    asyncio.run(B.on_button(u, ctx))
    assert s.db.get_user(1)["active"] == 0
    assert "wybierz tradera" in u.callback_query.answer.call_args.args[0].lower()


def test_connect_saves_chosen_exchange(tmp_path):
    s, _ = setup(tmp_path)
    s.db.ensure_user(1, None, 1000, "USDT")
    s.db.update_user(1, accepted=1)
    s.engine.verify_keys = AsyncMock(return_value={"USDT": 50.0})
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"svc": s}), user_data={}, bot=AsyncMock())

    async def flow():
        assert await B.got_exchange(tg_update(1, data="cex:okx"), ctx) == B.KEY
        await B.got_key(tg_update(1, text="k"), ctx)
        assert await B.got_secret(tg_update(1, text="s"), ctx) == B.PASSWORD  # OKX wymaga passphrase
        await B.got_password(tg_update(1, text="p"), ctx)

    asyncio.run(flow())
    assert s.engine.verify_keys.call_args.args == ("k", "s", "p", "okx")
    u = s.db.get_user(1)
    assert u["exchange"] == "okx" and s.vault.dec(u["api_password"]) == "p"


# ---------- Coinbase i dopasowanie par (USDT ↔ USD/USDC) ----------

def test_resolve_symbol_across_stablecoins():
    from copybot.engine import resolve_symbol
    mk = {"BTC/USDT": {}, "PEPE/USD": {}, "ETH/USDC": {}}
    assert resolve_symbol(mk, "BTC/USDT") == "BTC/USDT"
    assert resolve_symbol(mk, "PEPE/USDT") == "PEPE/USD"
    assert resolve_symbol(mk, "ETH/USD") == "ETH/USDC"
    assert resolve_symbol(mk, "DOGE/USDT") is None
    assert resolve_symbol(mk, "ETH/BTC") is None


class ResolvingMarket(FakeMarket):
    async def resolve(self, symbol):
        from copybot.engine import resolve_symbol
        return resolve_symbol({s: {} for s in self.symbols}, symbol)

    async def has(self, symbol):
        return await self.resolve(symbol) is not None


def test_coinbase_user_gets_usd_pair_and_demo_uses_usdt(tmp_path):
    cfg = Config(bot_token="1:X", encryption_key="x", db_path="x")
    hub = Markets(cfg)
    hub._m = {"binance": ResolvingMarket(100), "coinbase": ResolvingMarket(100, symbols=("PEPE/USD",),
                                                                            exchange_id="coinbase")}
    s, sent = setup(tmp_path, hub)
    lid = s.db.create_leader("T", "manual", None, "active")
    add_user(s, 1, lid, exchange="coinbase")
    res = asyncio.run(s.engine.copy(order(symbol="PEPE/USDT"), 0.1, lid))
    assert res == {"filled": 1}
    assert s.db.history(1)[0]["symbol"] == "PEPE/USD"
    bal = s.db.paper_balances(1)
    assert bal["USDT"] == pytest.approx(900) and bal["PEPE"] > 0  # demo liczy USD jak USDT
    assert any("PEPE/USD" in t for _, t in sent)


def test_equity_counts_all_stablecoins():
    from copybot.engine import Market
    cfg = Config(bot_token="1:X", encryption_key="x", db_path="x")
    m = Market.__new__(Market)
    m.cfg, m.exchange_id = cfg, "coinbase"
    m.ex = SimpleNamespace(markets={"BTC/USD": {}})
    m._lock = asyncio.Lock()
    m._prices = {}

    async def price(sym, max_age=10):
        return 50_000.0
    m.price = price
    assert asyncio.run(m.equity({"USD": 100.0, "USDC": 50.0, "BTC": 0.01})) == pytest.approx(650)


def test_watcher_accepts_usd_trades(tmp_path):
    s, _ = setup(tmp_path)
    rest = MagicMock()
    rest.fetch_balance = AsyncMock(return_value={"total": {"USD": 900.0, "BTC": 0.002}})
    w, got = make_watcher(s, rest)
    asyncio.run(w.process([trade(1, "buy", 0.002, 50_000, w.start_ts + 1, symbol="BTC/USD")]))
    (o, frac), = got[0][1]
    assert o.symbol == "BTC/USD" and frac == pytest.approx(0.1)


# ---------- przycisk odpowiada od razu, nawet gdy giełda wisi ----------

def test_button_acknowledged_before_slow_exchange(tmp_path):
    s, _ = setup(tmp_path)
    lid = s.db.create_leader("T", "manual", None, "active")
    s.db.ensure_user(1, None, 1000, "USDT")
    s.db.update_user(1, accepted=1, leader_id=lid)
    order_of_events = []

    async def slow_equity(u):
        order_of_events.append("equity")
        await asyncio.sleep(0.2)
        return 1000.0

    s.engine.equity_of = slow_equity
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"svc": s}), user_data={}, bot=AsyncMock())
    u = tg_update(1, data="toggle")
    u.callback_query.answer = AsyncMock(side_effect=lambda *a, **k: order_of_events.append("answer"))
    asyncio.run(B.on_button(u, ctx))
    assert order_of_events == ["answer", "equity"]
    assert u.callback_query.answer.await_count == 1
    assert s.db.get_user(1)["active"] == 1
    assert "Kopiowanie włączone" in u.callback_query.edit_message_text.call_args.args[0]


def test_button_shows_error_when_exchange_times_out(tmp_path, monkeypatch):
    s, _ = setup(tmp_path)
    lid = s.db.create_leader("T", "manual", None, "active")
    s.db.ensure_user(1, None, 1000, "USDT")
    s.db.update_user(1, accepted=1, leader_id=lid)
    monkeypatch.setattr(B, "UI_TIMEOUT", 0.05)

    async def hang(u):
        await asyncio.sleep(10)

    s.engine.equity_of = hang
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"svc": s}), user_data={}, bot=AsyncMock())
    u = tg_update(1, data="toggle")
    asyncio.run(B.on_button(u, ctx))
    assert s.db.get_user(1)["active"] == 0
    assert "przekroczony czas" in u.callback_query.edit_message_text.call_args.args[0]


# ---------- klucze Coinbase: różne formaty i sposoby wklejenia ----------

def test_normalize_secret_formats():
    from copybot.config import normalize_secret
    assert normalize_secret("  abc+/def==  \n") == "abc+/def=="
    assert normalize_secret('"abc def=="') == "abcdef=="
    body = "A" * 100
    pem = f"-----BEGIN EC PRIVATE KEY-----\n{body[:64]}\n{body[64:]}\n-----END EC PRIVATE KEY-----\n"
    assert normalize_secret(pem) == pem
    assert normalize_secret(pem.replace("\n", " ")) == pem  # sklejony spacjami
    assert normalize_secret(pem.replace("\n", "\\n")) == pem  # dosłowne \n z pliku JSON


def test_friendly_errors():
    import ccxt
    from copybot.config import friendly_error
    assert "odrzuciła klucz" in friendly_error(ccxt.AuthenticationError("coinbase 401 Unauthorized"))
    assert "format" in friendly_error(ValueError("Non-base16 digit found"))
    assert "nie odpowiada" in friendly_error(ccxt.RequestTimeout("timeout"))
