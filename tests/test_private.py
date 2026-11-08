"""Wersja prywatna: dostęp na zaproszenie, dodawanie traderów, kopiuj / obserwuj."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from cryptography.fernet import Fernet

from copybot import bot as B
from copybot import traders as T
from copybot.config import Config
from copybot.db import DB
from copybot.engine import CopyEngine
from copybot.security import Vault
from copybot.sizing import LeaderOrder

OWNER, FRIEND, STRANGER = 100, 200, 300


class FakeMarket:
    exchange_id = "binance"

    def __init__(self):
        self.p = 100.0

    async def price(self, symbol, max_age=10):
        return self.p

    async def min_cost(self, symbol):
        return 5.0

    async def has(self, symbol):
        return True

    async def resolve(self, symbol):
        return symbol

    async def equity(self, bal):
        return sum(v if k == "USDT" else v * self.p for k, v in bal.items())


def setup(tmp_path):
    cfg = Config(bot_token="1:X", encryption_key=Fernet.generate_key().decode(), db_path=str(tmp_path / "t.db"))
    db = DB(cfg.db_path)
    sent = []

    async def notify(uid, text):
        sent.append((uid, text))

    eng = CopyEngine(cfg, db, Vault(cfg.encryption_key), FakeMarket(), notify)
    watchers = MagicMock()
    s = B.Services(cfg, db, eng.vault, eng.market, eng, watchers)
    bot = AsyncMock()
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"svc": s}), user_data={}, bot=bot, args=[])
    return s, ctx, sent


def upd(uid, data=None, text="", username=None):
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
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid, username=username, full_name="X"),
                           callback_query=cq, effective_message=msg, message=msg, effective_chat=chat)


def last_text(u):
    if u.callback_query and u.callback_query.edit_message_text.called:
        return u.callback_query.edit_message_text.call_args.args[0]
    return u.effective_chat.send_message.call_args.args[0]


def onboard(s, ctx, uid):
    """Właściciel/kumpel z dostępem i zaakceptowanym ostrzeżeniem."""
    asyncio.run(B.cmd_start(upd(uid), ctx))
    asyncio.run(B.on_button(upd(uid, data="accept"), ctx))


# ---------- dostęp ----------

def test_private_access_flow(tmp_path):
    s, ctx, _ = setup(tmp_path)
    onboard(s, ctx, OWNER)
    assert s.db.get_state("owner") == str(OWNER) and s.db.get_user(OWNER)["approved"] == 1

    u = upd(FRIEND, username="ziomek")
    asyncio.run(B.cmd_start(u, ctx))
    assert "prywatny" in last_text(u)
    req = ctx.bot.send_message.call_args
    assert req.args[0] == OWNER and "ziomek" in req.args[1]
    asyncio.run(B.cmd_start(upd(FRIEND), ctx))  # druga próba – bez spamowania właściciela
    assert ctx.bot.send_message.await_count == 1

    asyncio.run(B.on_button(upd(STRANGER, data=f"usr_ok:{FRIEND}"), ctx))  # obcy nie może wpuszczać
    assert s.db.get_user(FRIEND)["approved"] == 0
    asyncio.run(B.on_button(upd(OWNER, data=f"usr_ok:{FRIEND}"), ctx))
    assert s.db.get_user(FRIEND)["approved"] == 1
    assert ctx.bot.send_message.call_args.args[0] == FRIEND  # kumpel dostał wiadomość

    u = upd(FRIEND)
    asyncio.run(B.cmd_start(u, ctx))
    assert "Zanim zaczniesz" in last_text(u)

    u = upd(OWNER, data="admin")
    asyncio.run(B.on_button(u, ctx))
    assert "ziomek" in last_text(u)
    asyncio.run(B.on_button(upd(OWNER, data=f"usr_del:{FRIEND}"), ctx))
    assert s.db.get_user(FRIEND)["approved"] == 0


# ---------- dodawanie traderów ----------

def test_add_manual_trader_and_signal(tmp_path):
    s, ctx, sent = setup(tmp_path)
    onboard(s, ctx, OWNER)
    s.db.ensure_user(FRIEND, "ziomek", 1000, "USDT")
    s.db.update_user(FRIEND, approved=1, accepted=1)

    async def flow():
        assert await T.add_start(upd(OWNER, data="add"), ctx) == T.A_NAME
        assert await T.add_name(upd(OWNER, text="Moje sygnały"), ctx) == T.A_SOURCE
        await T.add_source(upd(OWNER, data="src:manual"), ctx)

    asyncio.run(flow())
    lead = s.db.leaders("active")[0]
    assert lead["name"] == "Moje sygnały" and lead["owner_tg_id"] == OWNER and lead["source"] == "manual"
    assert s.db.follows_of(OWNER) == {lead["id"]: False}  # dodający obserwuje
    assert any(c.args[0] == FRIEND and "Moje sygnały" in c.args[1] for c in ctx.bot.send_message.call_args_list)
    s.watchers.sync.assert_called()

    # kumpel kopiuje, właściciel wysyła sygnał
    asyncio.run(T.on_trader_button(upd(FRIEND, data=f"cp:{lead['id']}"), ctx))
    s.db.update_user(FRIEND, active=1)
    ctx.args = ["buy", "BTC/USDT", "10"]
    u = upd(OWNER)
    asyncio.run(T.cmd_signal(u, ctx))
    reply = u.message.reply_text.call_args.args[0]
    assert "skopiowano: 1" in reply and "powiadomienia: 1" in reply
    assert "BTC" in s.db.paper_balances(FRIEND) and "BTC" not in s.db.paper_balances(OWNER)
    assert any(uid == OWNER and "👁" in t for uid, t in sent)


def test_add_exchange_trader(tmp_path, monkeypatch):
    s, ctx, _ = setup(tmp_path)
    onboard(s, ctx, OWNER)
    fake = MagicMock()
    fake.fetch_balance = AsyncMock(return_value={"total": {}})
    fake.close = AsyncMock()
    seen = {}

    def make(cfg, key, secret, pw, exchange_id=None):
        seen.update(key=key, secret=secret, pw=pw, ex=exchange_id)
        return fake
    monkeypatch.setattr(T, "make_exchange", make)

    async def flow():
        await T.add_start(upd(OWNER, data="add"), ctx)
        await T.add_name(upd(OWNER, text="Kacper"), ctx)
        assert await T.add_source(upd(OWNER, data="src:api"), ctx) == T.A_EXCH
        assert await T.add_exchange(upd(OWNER, data="aex:okx"), ctx) == T.A_KEY
        await T.add_key(upd(OWNER, text="KEY"), ctx)
        assert await T.add_secret(upd(OWNER, text=" SEC "), ctx) == T.A_PASS  # OKX: passphrase
        await T.add_pass(upd(OWNER, text="PW"), ctx)

    asyncio.run(flow())
    assert seen == {"key": "KEY", "secret": "SEC", "pw": "PW", "ex": "okx"}
    lead = s.db.leaders("active")[0]
    assert lead["exchange"] == "okx" and s.vault.dec(lead["api_secret"]) == "SEC"


# ---------- kopiuj / obserwuj / usuń / zmień nazwę ----------

def test_follow_buttons_delete_and_rename(tmp_path):
    s, ctx, _ = setup(tmp_path)
    onboard(s, ctx, OWNER)
    s.db.ensure_user(FRIEND, None, 1000, "USDT")
    s.db.update_user(FRIEND, approved=1, accepted=1)
    lid = s.db.create_leader("Ania", "manual", OWNER, "active")

    for data, expected in ((f"cp:{lid}", {lid: True}), (f"wt:{lid}", {lid: False}), (f"uf:{lid}", {})):
        asyncio.run(T.on_trader_button(upd(FRIEND, data=data), ctx))
        assert s.db.follows_of(FRIEND) == expected

    u = upd(FRIEND, data="traders")
    asyncio.run(T.on_trader_button(u, ctx))
    assert "Ania" in last_text(u) and "Dodaj tradera" in str(u.callback_query.edit_message_text.call_args)

    asyncio.run(T.on_trader_button(upd(FRIEND, data=f"delok:{lid}"), ctx))  # kumpel nie dodał – nie usunie
    assert s.db.get_leader(lid)["status"] == "active"

    async def rename():
        assert await T.rename_start(upd(OWNER, data=f"ren:{lid}"), ctx) == T.R_NAME
        await T.rename_done(upd(OWNER, text="Ania Scalp"), ctx)
    asyncio.run(rename())
    assert s.db.get_leader(lid)["name"] == "Ania Scalp"

    s.db.follow(FRIEND, lid, True)
    asyncio.run(T.on_trader_button(upd(OWNER, data=f"delok:{lid}"), ctx))
    assert s.db.get_leader(lid)["status"] == "deleted" and s.db.follows_of(FRIEND) == {}
    assert s.db.leaders(None) == []


def test_copy_many_traders_at_once(tmp_path):
    s, ctx, _ = setup(tmp_path)
    onboard(s, ctx, OWNER)
    a = s.db.create_leader("A", "manual", OWNER, "active")
    b = s.db.create_leader("B", "manual", OWNER, "active")
    s.db.follow(OWNER, a, True)
    s.db.follow(OWNER, b, True)
    s.db.update_user(OWNER, active=1, max_trade=10_000)
    o = lambda sym: LeaderOrder("x" + sym, sym, "buy", 0, 0, 100, 1)  # noqa: E731
    asyncio.run(s.engine.copy(o("BTC/USDT"), 0.1, a))
    asyncio.run(s.engine.copy(o("ETH/USDT"), 0.1, b))
    bal = s.db.paper_balances(OWNER)
    assert "BTC" in bal and "ETH" in bal


def test_dashboard_and_toggle(tmp_path):
    s, ctx, _ = setup(tmp_path)
    onboard(s, ctx, OWNER)
    u = upd(OWNER, data="toggle")
    asyncio.run(B.on_button(u, ctx))  # bez tradera – nie włączy
    assert s.db.get_user(OWNER)["active"] == 0
    lid = s.db.create_leader("Kacper", "manual", OWNER, "active")
    s.db.follow(OWNER, lid, True)
    u = upd(OWNER, data="toggle")
    asyncio.run(B.on_button(u, ctx))
    assert s.db.get_user(OWNER)["active"] == 1
    text = last_text(u)
    assert "Kopiujesz:</b> Kacper" in text and "włączone" in text and "Admin" in str(
        u.callback_query.edit_message_text.call_args)
