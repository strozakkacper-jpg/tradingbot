"""Handel demo i przypięta wiadomość „na żywo”."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

sys.path.insert(0, str(Path(__file__).parent))
from test_private import OWNER, B, last_text, onboard, setup, upd  # noqa: E402

from copybot import demo, live  # noqa: E402
from copybot.engine import Skip  # noqa: E402


class TickerMarket:
    exchange_id = "binance"

    def __init__(self):
        self.p = {"BTC/USDT": 50_000.0, "ETH/USDT": 2_500.0, "SOL/USDT": 150.0}

    async def resolve(self, symbol):
        return symbol if symbol in self.p or symbol.startswith(("XRP", "DOGE", "BNB")) else None

    async def has(self, symbol):
        return await self.resolve(symbol) is not None

    async def price(self, symbol, max_age=10):
        return self.p.get(symbol, 1.0)

    async def ticker(self, symbol, max_age=30):
        return await self.price(symbol), 2.5

    def cached(self, symbol, max_age=300):
        return (self.p[symbol], -1.0) if symbol in self.p else None

    async def min_cost(self, symbol):
        return 5.0

    async def equity(self, bal):
        return sum(v if k == "USDT" else v * self.p.get(f"{k}/USDT", 1.0) for k, v in bal.items())


def ready(tmp_path):
    s, ctx, sent = setup(tmp_path)
    s.engine.market = TickerMarket()
    onboard(s, ctx, OWNER)
    return s, ctx, sent


# ---------- handel demo ----------

def test_demo_buy_and_sell(tmp_path):
    s, ctx, _ = ready(tmp_path)
    u = s.db.get_user(OWNER)
    assert "Kupiono" in asyncio.run(demo.trade(s, u, "BTC", "buy", "500"))
    bal = s.db.paper_balances(OWNER)
    assert bal["USDT"] == pytest.approx(500) and bal["BTC"] == pytest.approx(0.01 * 0.999)
    asyncio.run(demo.trade(s, u, "ETH", "buy", "p50"))  # 50% gotówki
    assert s.db.paper_balances(OWNER)["USDT"] == pytest.approx(250)
    asyncio.run(demo.trade(s, u, "BTC", "sell", "50%"))
    assert s.db.paper_balances(OWNER)["BTC"] == pytest.approx(0.01 * 0.999 / 2)
    asyncio.run(demo.trade(s, u, "BTC", "sell", "100%"))
    assert s.db.paper_balances(OWNER).get("BTC", 0) == pytest.approx(0, abs=1e-12)
    with pytest.raises(Skip):
        asyncio.run(demo.trade(s, u, "BTC", "sell", "50%"))
    with pytest.raises(Skip):
        asyncio.run(demo.trade(s, u, "NOPE", "buy", "10"))
    assert s.db.history(OWNER)[0]["info"] == "manual"


def test_demo_uses_demo_account_even_in_live_mode(tmp_path):
    s, ctx, _ = ready(tmp_path)
    s.db.update_user(OWNER, paper=0)
    asyncio.run(demo.trade(s, s.db.get_user(OWNER), "SOL", "buy", "150"))
    assert s.db.paper_balances(OWNER)["SOL"] == pytest.approx(0.999)


def test_demo_buttons_and_commands(tmp_path):
    s, ctx, _ = ready(tmp_path)
    u = upd(OWNER, data="dt")
    asyncio.run(demo.on_demo_button(u, ctx))
    text = last_text(u)
    assert "Handel demo" in text and "BTC" in text and "1 000,00" not in text and "1 000.00 USDT" in text
    u = upd(OWNER, data="db:BTC:100")
    asyncio.run(demo.on_demo_button(u, ctx))
    assert "Kupiono" in last_text(u) and "Sprzedaj 100%" in str(u.callback_query.edit_message_text.call_args)
    u = upd(OWNER, data="ds:BTC:100")
    asyncio.run(demo.on_demo_button(u, ctx))
    assert "Sprzedano" in last_text(u)
    ctx.args = ["eth", "25%"]
    u = upd(OWNER)
    asyncio.run(demo.cmd_buy(u, ctx))
    assert "Kupiono" in u.effective_chat.send_message.call_args.args[0]
    ctx.args = []
    u = upd(OWNER)
    asyncio.run(demo.cmd_sell(u, ctx))
    assert "Użycie" in u.message.reply_text.call_args.args[0]


# ---------- na żywo u góry ----------

def test_live_text_and_pinned_message(tmp_path):
    s, ctx, _ = ready(tmp_path)
    lid = s.db.create_leader("Kacper", "manual", OWNER, "active")
    s.db.follow(OWNER, lid, True)
    asyncio.run(demo.trade(s, s.db.get_user(OWNER), "BTC", "buy", "100"))
    text = asyncio.run(live.live_text(s, s.db.get_user(OWNER)))
    assert "Na żywo" in text and "BTC</b> 50 000 🟢+2.5%" in text and "Kopiujesz: Kacper" in text
    assert "Ostatnio: 🟢 BTC/USDT" in text and "demo" in text

    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=77)
    asyncio.run(live.refresh(bot, s, s.db.get_user(OWNER)))
    bot.pin_chat_message.assert_awaited_once()
    assert s.db.get_user(OWNER)["pin_msg_id"] == 77
    asyncio.run(live.refresh(bot, s, s.db.get_user(OWNER)))  # kolejne odświeżenie = edycja
    bot.edit_message_text.assert_awaited()
    assert bot.send_message.await_count == 1
    bot.edit_message_text.side_effect = BadRequest("Message to edit not found")
    bot.send_message.return_value = SimpleNamespace(message_id=78)
    asyncio.run(live.refresh(bot, s, s.db.get_user(OWNER)))  # usunięta – przypina nową
    assert s.db.get_user(OWNER)["pin_msg_id"] == 78

    ctx.bot = bot
    asyncio.run(B.on_button(upd(OWNER, data="pin"), ctx))  # wyłączenie
    assert s.db.get_user(OWNER)["live_pin"] == 0
    bot.unpin_chat_message.assert_awaited()


def test_dashboard_shows_prices_and_demo_button(tmp_path):
    s, ctx, _ = ready(tmp_path)
    u = upd(OWNER, data="menu")
    asyncio.run(B.on_button(u, ctx))
    text = last_text(u)
    assert "<b>BTC</b> 50 000 🔴-1.0%" in text and "Handel demo" in str(u.callback_query.edit_message_text.call_args)
