"""Tryb lustra: portfel kopiującego trzyma te same proporcje co trader."""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_private import OWNER, FRIEND, B, onboard, setup, upd, last_text  # noqa: E402

from copybot import mirror  # noqa: E402
from copybot.db import ref_id  # noqa: E402
from copybot.sizing import LeaderOrder  # noqa: E402


def trader_with_btc(s, half=True):
    """Trader „sygnały”: portfel wzorcowy 1000 USDT, kupił BTC za połowę (cena 100)."""
    lid = s.db.create_leader("Lider", "manual", OWNER, "active")
    s.db.ensure_reference(lid, 1000, "USDT")
    if half:
        s.db.paper_apply(ref_id(lid), {"USDT": -500, "BTC": 5})
    return lid


def mirror_user(s, ctx, uid, lid, **kw):
    onboard(s, ctx, uid) if uid == OWNER else None
    s.db.ensure_user(uid, None, 1000, "USDT")
    s.db.update_user(uid, approved=1, accepted=1, active=1, copy_mode="mirror", **kw)
    s.db.follow(uid, lid, True)
    return s.db.get_user(uid)


def test_trader_weights_from_reference(tmp_path):
    s, ctx, _ = setup(tmp_path)
    lid = trader_with_btc(s)
    w = asyncio.run(mirror.trader_weights(s, s.db.get_leader(lid)))
    assert w == pytest.approx({"USDT": 0.5, "BTC": 0.5})


def test_sync_aligns_portfolio_and_respects_tolerance(tmp_path):
    s, ctx, sent = setup(tmp_path)
    lid = trader_with_btc(s)
    u = mirror_user(s, ctx, OWNER, lid)
    p = asyncio.run(mirror.plan(s, u))
    assert p.match == pytest.approx(0.5) and p.orders == [("BTC", "buy", pytest.approx(500))]
    res = asyncio.run(mirror.sync(s, u, "test"))
    assert res["filled"] == 1
    after = asyncio.run(mirror.plan(s, s.db.get_user(OWNER)))
    assert after.match > 0.99 and after.orders == []
    assert any("Lustro" in t and "Zgodność" in t for _, t in sent)
    # drobne wahnięcie ceny (+1%) – poniżej tolerancji 2%, brak zleceń
    s.engine.market.p = 101.0
    assert asyncio.run(mirror.plan(s, s.db.get_user(OWNER))).orders == []


def test_sync_sells_when_trader_sells_and_keeps_foreign_coins(tmp_path):
    s, ctx, _ = setup(tmp_path)
    lid = trader_with_btc(s)
    u = mirror_user(s, ctx, OWNER, lid)
    s.db.paper_apply(OWNER, {"DOGE": 1000})  # moneta, której bot nigdy nie kupował
    asyncio.run(mirror.sync(s, u))
    # trader sprzedaje całe BTC
    s.db.paper_apply(ref_id(lid), {"BTC": -5, "USDT": 500})
    asyncio.run(mirror.sync(s, s.db.get_user(OWNER)))
    bal = s.db.paper_balances(OWNER)
    assert bal.get("BTC", 0) < 1e-9 and bal["DOGE"] == 1000


def _hook(s, called):
    async def after(leader_id):
        called.append(leader_id)
        for u in s.db.mirror_followers(leader_id):
            await mirror.sync(s, u)
    return after


def test_mirror_copies_trade_immediately_without_doubling(tmp_path):
    s, ctx, sent = setup(tmp_path)
    lid = trader_with_btc(s, half=False)
    mirror_user(s, ctx, OWNER, lid, multiplier=3.0, max_trade=10)  # mnożnik i limit nie psują lustra
    s.db.ensure_user(FRIEND, None, 1000, "USDT")
    s.db.update_user(FRIEND, approved=1, accepted=1, active=1, max_trade=10_000)
    s.db.follow(FRIEND, lid, True)
    called = []
    s.engine.after_trade = _hook(s, called)
    res = asyncio.run(s.engine.copy(LeaderOrder("A", "BTC/USDT", "buy", 0, 0, 100, 1), 0.5, lid))
    assert res["filled"] == 2 and called == [lid]  # właściciel (lustro) też dostał transakcję od razu
    assert any(uid == OWNER and "Skopiowano" in t for uid, t in sent)
    p = asyncio.run(mirror.plan(s, s.db.get_user(OWNER)))
    assert p.current.get("BTC", 0) == pytest.approx(0.5, abs=0.02)  # 50%, a nie 100% – bez dublowania
    assert p.orders == []


def test_mirror_without_trade_copy_only_rebalances(tmp_path):
    s, ctx, sent = setup(tmp_path)
    lid = trader_with_btc(s, half=False)
    mirror_user(s, ctx, OWNER, lid, mirror_trades=0)
    called = []
    s.engine.after_trade = _hook(s, called)
    res = asyncio.run(s.engine.copy(LeaderOrder("A", "BTC/USDT", "buy", 0, 0, 100, 1), 0.5, lid))
    assert res.get("filled", 0) == 0 and called == [lid]
    assert not any("Skopiowano" in t for _, t in sent) and any("Lustro" in t for _, t in sent)
    p = asyncio.run(mirror.plan(s, s.db.get_user(OWNER)))
    assert p.current.get("BTC", 0) == pytest.approx(0.5, abs=0.02)


def test_mirror_ui(tmp_path):
    s, ctx, _ = setup(tmp_path)
    onboard(s, ctx, OWNER)
    lid = trader_with_btc(s)
    s.db.follow(OWNER, lid, True)
    u = upd(OWNER, data="cm:mirror")
    asyncio.run(B.on_button(u, ctx))
    assert s.db.get_user(OWNER)["copy_mode"] == "mirror" and "Lustro 1:1" in last_text(u)
    asyncio.run(B.on_button(upd(OWNER, data="tol:5"), ctx))
    assert s.db.get_user(OWNER)["mirror_tolerance"] == 5
    u = upd(OWNER, data="mtr")
    asyncio.run(B.on_button(u, ctx))
    assert s.db.get_user(OWNER)["mirror_trades"] == 0 and "tylko wyrównanie" in last_text(u)
    asyncio.run(B.on_button(upd(OWNER, data="mtr"), ctx))
    assert s.db.get_user(OWNER)["mirror_trades"] == 1
    u = upd(OWNER, data="compare")
    asyncio.run(B.on_button(u, ctx))
    text = last_text(u)
    assert "Porównanie z traderem" in text and "BTC" in text and "50.0%" in text
    assert "Wyrównaj teraz" in str(u.callback_query.edit_message_text.call_args)
    u = upd(OWNER, data="mirror_now")
    asyncio.run(B.on_button(u, ctx))
    assert "Wyrównano: zleceń 1" in last_text(u)
    assert "BTC" in s.db.paper_balances(OWNER)
