import asyncio
import hashlib
import hmac
import json
import time
import urllib.parse
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

from copybot import jobs
from copybot.billing import has_access
from copybot.bot import Services
from copybot.config import Config
from copybot.db import DB, now_ms, ref_id
from copybot.engine import CopyEngine
from copybot.reports import leader_report, leader_text
from copybot.security import Vault
from copybot.sizing import LeaderOrder
from copybot.stats import downsample, max_drawdown, period_return, trade_stats
from copybot.web import build_app, verify_init_data

TOKEN = "123:ABC"


class FakeMarket:
    def __init__(self, price):
        self.p = price

    async def price(self, symbol):
        return self.p

    async def min_cost(self, symbol):
        return 5.0

    async def has(self, symbol):
        return True

    exchange_id = "binance"

    async def equity(self, bal):
        return sum(v if k == "USDT" else v * self.p for k, v in bal.items())


def make(tmp_path, **cfg_kw):
    cfg = Config(bot_token=TOKEN, encryption_key=Fernet.generate_key().decode(), db_path=str(tmp_path / "t.db"),
                 **cfg_kw)
    db = DB(cfg.db_path)
    db.create_leader("Trader", "manual", None, "active")
    db.ensure_reference(1, cfg.paper_start, cfg.quote)
    sent = []

    async def notify(uid, text):
        sent.append((uid, text))

    market = FakeMarket(100.0)
    eng = CopyEngine(cfg, db, Vault(cfg.encryption_key), market, notify)
    return Services(cfg, db, eng.vault, market, eng, None), sent


def user(s, uid, **kw):
    s.db.ensure_user(uid, None, s.cfg.paper_start, s.cfg.quote)
    s.db.update_user(uid, accepted=1, approved=1, active=1, max_trade=10_000, **kw)
    s.db.follow(uid, 1, True)
    return s.db.get_user(uid)


# ---------- statystyki ----------

def test_max_drawdown_and_returns():
    assert max_drawdown([100, 120, 90, 130, 117]) == pytest.approx(0.25)
    series = [(1, 100.0), (5, 110.0), (9, 120.0)]
    assert period_return(series, 4, 132.0) == pytest.approx(0.2)
    assert period_return(series, 10, 1.0) is None
    assert len(downsample([(i, float(i)) for i in range(1000)], 100)) <= 101


def test_trade_stats_average_cost():
    fills = [
        {"symbol": "BTC/USDT", "side": "buy", "amount": 1, "price": 100, "cost": 100},
        {"symbol": "BTC/USDT", "side": "buy", "amount": 1, "price": 200, "cost": 200},
        {"symbol": "BTC/USDT", "side": "sell", "amount": 1, "price": 180, "cost": 180},  # +30
        {"symbol": "ETH/USDT", "side": "buy", "amount": 2, "price": 50, "cost": 100},
        {"symbol": "ETH/USDT", "side": "sell", "amount": 2, "price": 40, "cost": 80},  # -20
    ]
    t = trade_stats(fills)
    assert t["closed"] == 2 and t["wins"] == 1 and t["win_rate"] == 0.5
    assert t["realized"] == pytest.approx(10) and t["best"] == pytest.approx(30)


# ---------- portfel wzorcowy i wyniki lidera ----------

def test_reference_portfolio_tracks_leader(tmp_path):
    s, _ = make(tmp_path)
    asyncio.run(s.engine.copy(LeaderOrder("A", "BTC/USDT", "buy", 0, 0, 100, 1), 0.5, 1))
    s.market.p = 120.0
    asyncio.run(s.engine.copy(LeaderOrder("B", "BTC/USDT", "sell", 0, 0, 120, 2), 1.0, 1))
    rep = asyncio.run(leader_report(s, 1, max_age=0))
    assert rep["closed"] == 1 and rep["wins"] == 1
    assert rep["total"] == pytest.approx((500 + 5 * 0.999 * 120 * 0.999) / 1000 - 1)
    assert "Wyniki lidera" in leader_text(rep, "USDT")


# ---------- stop-loss ----------

def test_stop_loss_disables_and_closes_positions(tmp_path):
    s, sent = make(tmp_path)
    u = user(s, 1, stop_loss=20)
    asyncio.run(s.engine.copy(LeaderOrder("A", "BTC/USDT", "buy", 0, 0, 100, 1), 1.0, 1))
    s.db.update_user(1, start_equity=1000)
    s.market.p = 70.0  # portfel ~700 USDT => -30%
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"svc": s}), bot=AsyncMock())
    asyncio.run(jobs.snapshot_job(ctx))
    u = s.db.get_user(1)
    assert u["active"] == 0
    assert "BTC" not in s.db.paper_balances(1)
    assert any("Stop-loss zadziałał" in t for _, t in sent)
    assert s.db.equity_series(1, True)  # zapisano wartość


def test_stop_loss_not_triggered_above_threshold(tmp_path):
    s, _ = make(tmp_path)
    user(s, 1, stop_loss=50, start_equity=1000)
    asyncio.run(s.engine.copy(LeaderOrder("A", "BTC/USDT", "buy", 0, 0, 100, 1), 1.0, 1))
    s.market.p = 80.0
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"svc": s}), bot=AsyncMock())
    asyncio.run(jobs.snapshot_job(ctx))
    assert s.db.get_user(1)["active"] == 1


# ---------- subskrypcja ----------

def test_paywall_only_for_live_by_default(tmp_path):
    s, _ = make(tmp_path, sub_price_stars=100)
    u = user(s, 1)
    assert has_access(s.cfg, u)  # demo darmowe
    assert not has_access(s.cfg, u, paper=False)
    s.db.add_payment(1, "ch_1", 100, False, now_ms() + 10_000)
    assert has_access(s.cfg, s.db.get_user(1), paper=False)
    s.db.add_payment(1, "ch_1", 100, False, now_ms() + 10_000)  # duplikat ignorowany
    assert s.db.stats()["stars"] == 100


def test_expired_subscription_stops_copying(tmp_path):
    s, _ = make(tmp_path, sub_price_stars=100, sub_scope="all")
    user(s, 1, paid_until=now_ms() - 1)
    bot = AsyncMock()
    bot.create_invoice_link.return_value = "https://t.me/$inv"
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"svc": s}), bot=bot)
    res = asyncio.run(s.engine.copy(LeaderOrder("A", "BTC/USDT", "buy", 0, 0, 100, 1), 0.5, 1))
    assert res.get("filled", 0) == 0  # bez dostępu nic nie kupujemy
    asyncio.run(jobs.snapshot_job(ctx))
    assert s.db.get_user(1)["active"] == 0
    assert bot.create_invoice_link.call_args.kwargs["currency"] == "XTR"


# ---------- panel ----------

def sign(user_obj: dict, token: str = TOKEN, auth_date: int | None = None) -> str:
    data = {"auth_date": str(auth_date or int(time.time())), "query_id": "q", "user": json.dumps(user_obj)}
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(data)


def test_init_data_verification():
    assert verify_init_data(sign({"id": 5}), TOKEN) == {"id": 5}
    assert verify_init_data(sign({"id": 5}, token="999:X"), TOKEN) is None
    assert verify_init_data(sign({"id": 5}, auth_date=int(time.time()) - 3 * 86400), TOKEN) is None
    assert verify_init_data("", TOKEN) is None


def test_web_api(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    s, _ = make(tmp_path)
    user(s, 7)

    async def run():
        async with TestClient(TestServer(build_app(s))) as c:
            assert (await c.get("/api/data")).status == 401
            r = await c.get("/api/data", headers={"X-Init-Data": sign({"id": 7})})
            j = await r.json()
            assert r.status == 200 and j["me"]["equity"] == pytest.approx(1000) and "total" in j["leader"]
            assert (await c.get("/api/data", headers={"X-Init-Data": sign({"id": 8})})).status == 403
            assert (await c.get("/app")).status == 200

    asyncio.run(run())
