"""Pulpit główny w stylu Axiom: kopiowanie portfeli Solany."""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_private import OWNER, B, last_text, onboard, upd  # noqa: E402
from test_sol import TOKEN, WALLET, sol_setup, swap  # noqa: E402

from copybot.sol import axiom as AX  # noqa: E402
from copybot.sol.parser import LAMPORTS, SOL_MINT  # noqa: E402


def ready(tmp_path):
    s, ctx, sent = sol_setup(tmp_path)
    onboard(s, ctx, OWNER)
    return s, ctx, sent


def btns(u):
    return str(u.callback_query.edit_message_text.call_args or u.effective_chat.send_message.call_args)


def test_start_opens_solana_dashboard_and_cex_is_second(tmp_path):
    s, ctx, _ = ready(tmp_path)
    u = upd(OWNER)
    asyncio.run(B.cmd_start(u, ctx))
    text = u.effective_chat.send_message.call_args.args[0]
    assert "Copy Trading · Solana" in text and "SOL $100.00" in text and "Dodaj portfel" in str(
        u.effective_chat.send_message.call_args)
    assert "Giełdy (CEX)" in str(u.effective_chat.send_message.call_args)
    u = upd(OWNER, data="menu")
    asyncio.run(B.on_button(u, ctx))
    assert "Pulpit giełd (CEX)" in last_text(u) and "Pulpit Solana" in btns(u)


def test_add_wallet_copy_and_dashboard_feed(tmp_path):
    s, ctx, sent = ready(tmp_path)

    async def add():
        assert await AX.add_start(upd(OWNER, data="ax:add"), ctx) == AX.ADD_ADDR
        assert await AX.add_addr(upd(OWNER, text="zły adres"), ctx) == AX.ADD_ADDR
        await AX.add_addr(upd(OWNER, text=f"{WALLET} Kowal"), ctx)
    asyncio.run(add())
    lead = s.db.leaders("active")[0]
    assert lead["name"] == "Kowal" and lead["wallet"] == WALLET and s.db.follows_of(OWNER) == {lead["id"]: True}
    u = upd(OWNER, data="ax:tog")
    asyncio.run(AX.on_button(u, ctx))
    assert s.db.get_user(OWNER)["active"] == 1
    asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1), time.monotonic()))
    u = upd(OWNER, data="home")
    asyncio.run(AX.on_button(u, ctx))
    text = last_text(u)
    assert "✅ <b>Kowal</b>" in text and "kopii 24h: 1" in text and "Ostatnie ruchy" in text and "⚡" in text
    u = upd(OWNER, data="ax:feed")
    asyncio.run(AX.on_button(u, ctx))
    assert "kupiono <b>MEME</b>" in last_text(u)


def test_per_wallet_amount_overrides_default(tmp_path):
    s, ctx, _ = ready(tmp_path)
    lid = s.db.create_leader("A", "sol", OWNER, "active", wallet=WALLET)
    s.db.follow(OWNER, lid, True)
    s.sol.wallet(OWNER)
    s.db.update_user(OWNER, active=1, sol_buy=0.05)
    u = upd(OWNER, data=f"aa:{lid}:0.5")
    asyncio.run(AX.on_button(u, ctx))
    assert s.db.follow_row(OWNER, lid)["sol_buy"] == 0.5 and "0.5 SOL" in last_text(u)
    asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.01), time.monotonic()))
    assert s.sol.paper(OWNER)[TOKEN] == 500_000_000  # 0,5 SOL zamiast domyślnych 0,05
    asyncio.run(AX.on_button(upd(OWNER, data=f"aa:{lid}:0"), ctx))
    assert s.db.follow_row(OWNER, lid)["sol_buy"] is None


def test_paste_token_and_wallet(tmp_path):
    s, ctx, _ = ready(tmp_path)
    u = upd(OWNER, text=TOKEN)
    asyncio.run(AX.on_paste(u, ctx))
    card = u.effective_chat.send_message.call_args.args[0]
    assert "MEME" in card and "Wartość całego tokena: $1.20M" in card and "Posiadacze: 4 321" in card and "✅ mint wył." in card
    assert "Ryzyko:" in card
    u = upd(OWNER, text=WALLET)
    asyncio.run(AX.on_paste(u, ctx))
    assert "Kopiuj jego transakcje" in str(u.effective_chat.send_message.call_args)
    asyncio.run(AX.on_button(upd(OWNER, data=f"aq:copy:{WALLET}"), ctx))
    lead = s.db.leaders("active")[0]
    assert lead["wallet"] == WALLET and s.db.follows_of(OWNER) == {lead["id"]: True}


def test_token_card_buy_sell_and_positions_pnl(tmp_path):
    s, ctx, _ = ready(tmp_path)
    s.sol.wallet(OWNER)
    u = upd(OWNER, data=f"tb:0.5:{TOKEN}")
    asyncio.run(AX.on_button(u, ctx))
    assert "Kupiono" in last_text(u) and s.sol.paper(OWNER)[TOKEN] == 500_000_000
    s.sol.jup.token_usd = 0.2  # cena x2 – PnL ok. +100%
    u = upd(OWNER, data="ax:pos")
    asyncio.run(AX.on_button(u, ctx))
    assert "zysk/strata 🟢+100.0%" in last_text(u)
    u = upd(OWNER, data=f"ts:100:{TOKEN}")
    asyncio.run(AX.on_button(u, ctx))
    assert "Sprzedano" in last_text(u) and TOKEN not in s.sol.paper(OWNER)


def test_auto_take_profit(tmp_path):
    s, ctx, sent = ready(tmp_path)
    s.sol.wallet(OWNER)
    asyncio.run(AX.on_button(upd(OWNER, data=f"tb:0.5:{TOKEN}"), ctx))
    asyncio.run(AX.on_button(upd(OWNER, data="ax:tp:50"), ctx))
    assert s.db.get_user(OWNER)["sol_tp"] == 50
    jctx = SimpleNamespace(application=ctx.application, bot=AsyncMock())
    asyncio.run(AX.tpsl_job(jctx))
    assert TOKEN in s.sol.paper(OWNER)  # bez zysku – trzymamy
    s.sol.jup.token_usd = 0.16  # +60%
    asyncio.run(AX.tpsl_job(jctx))
    assert TOKEN not in s.sol.paper(OWNER) and any("Take-profit" in t for _, t in sent)


def test_trending_and_settings(tmp_path):
    s, ctx, _ = ready(tmp_path)
    u = upd(OWNER, data="ax:trend")
    asyncio.run(AX.on_button(u, ctx))
    assert "Popularne teraz" in last_text(u) and "MEME" in last_text(u)
    u = upd(OWNER, data="ax:set")
    asyncio.run(AX.on_button(u, ctx))
    assert "take-profit" in last_text(u) and "Ile wydać" in last_text(u)


def test_buy_before_wallet_exists_keeps_position(tmp_path):
    """Regresja: zakup z karty tokena jako pierwsza akcja – utworzenie portfela nie może wyzerować pozycji."""
    s, ctx, _ = ready(tmp_path)
    assert not s.sol.has_wallet(OWNER)
    asyncio.run(AX.on_button(upd(OWNER, data=f"tb:0.1:{TOKEN}"), ctx))
    u = upd(OWNER, data="home")
    asyncio.run(AX.on_button(u, ctx))
    bal = s.sol.paper(OWNER)
    assert bal[TOKEN] == 100_000_000 and bal[SOL_MINT] == 5 * LAMPORTS - LAMPORTS // 10 - 200_000
    assert "pozycje 1" in last_text(u)


def test_session_report_after_stopping(tmp_path):
    from copybot.sol import session
    s, ctx, sent = ready(tmp_path)
    w2 = "Wa11et2222222222222222222222222222222222222"
    a = s.db.create_leader("Kowal", "sol", OWNER, "active", wallet=WALLET)
    b = s.db.create_leader("Wieloryb", "sol", OWNER, "active", wallet=w2)
    for lid in (a, b):
        s.db.follow(OWNER, lid, True)
    s.sol.wallet(OWNER)
    asyncio.run(AX.on_button(upd(OWNER, data="ax:tog"), ctx))  # start sesji
    u = s.db.get_user(OWNER)
    assert u["active"] == 1 and u["session_start"] and u["session_sol0"] == pytest.approx(5.0)

    asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1, "k1"), time.monotonic()))
    s.sol.jup.token_usd = 0.15  # +50%
    asyncio.run(s.sol.on_swap(WALLET, swap("sell", 1.0, "k2"), time.monotonic()))  # zysk z portfela Kowal
    asyncio.run(s.sol.on_swap(w2, swap("buy", 0.1, "w1"), time.monotonic() - 60))  # za późno – pominięte
    u = upd(OWNER, data="ax:rep")
    asyncio.run(AX.on_button(u, ctx))
    assert "(trwa)" in last_text(u)

    asyncio.run(AX.on_button(upd(OWNER, data="ax:tog"), ctx))  # koniec sesji → raport
    report = ctx.bot.send_message.call_args.args[1]
    assert ctx.bot.send_message.call_args.args[0] == OWNER
    assert "Raport sesji</b> · kopiowanie wyłączone" in report and "Zarobiłeś" in report
    assert "Transakcje: <b>2</b> (🟢 kupna 1 · 🔴 sprzedaże 1)" in report and "pominięte 1" in report
    assert "Sprzedane tokeny: 1 · z zyskiem 1 (100%)" in report and "Najlepsza: MEME" in report
    assert "• Kowal: kopie 2" in report and "⚡ Szybkość kopii" in report and "za późno ×1" in report
    assert s.db.get_user(OWNER)["session_end"] is not None
    assert asyncio.run(session.end(s, s.db.get_user(OWNER), ctx.bot)) == ""  # drugi raz nic nie wysyła


def test_copy_percent_of_trader_amount(tmp_path):
    """Tryb „% kwoty tradera”: on kupi za 1 SOL, przy 25% Ty kupujesz za 0,25 SOL; ustawienie portfela wygrywa."""
    s, ctx, _ = ready(tmp_path)
    lid = s.db.create_leader("A", "sol", OWNER, "active", wallet=WALLET)
    s.db.follow(OWNER, lid, True)
    s.sol.wallet(OWNER)
    s.db.update_user(OWNER, active=1)
    u = upd(OWNER, data="sx:bm:pct")
    from copybot.sol import ui as SU
    asyncio.run(SU.on_sol_button(u, ctx))
    asyncio.run(SU.on_sol_button(upd(OWNER, data="sx:pct:25"), ctx))
    assert s.db.get_user(OWNER)["sol_buy_mode"] == "pct" and s.db.get_user(OWNER)["sol_pct"] == 25
    asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1), time.monotonic()))  # trader kupił za 1 SOL
    assert s.sol.paper(OWNER)[TOKEN] == 250_000_000
    u = upd(OWNER, data=f"ap:{lid}:50")  # dla tego portfela: 50% jego kwoty
    asyncio.run(AX.on_button(u, ctx))
    fr = s.db.follow_row(OWNER, lid)
    assert fr["sol_pct"] == 50 and fr["sol_buy"] is None and "50% jego kwoty" in last_text(u)
    asyncio.run(s.sol.on_swap(WALLET, swap("buy", 0.1, "s2"), time.monotonic()))
    assert s.sol.paper(OWNER)[TOKEN] == 750_000_000
    asyncio.run(AX.on_button(upd(OWNER, data=f"aa:{lid}:0.1"), ctx))  # stała kwota kasuje %
    fr = s.db.follow_row(OWNER, lid)
    assert fr["sol_buy"] == 0.1 and fr["sol_pct"] is None


def test_add_several_wallets_at_once(tmp_path):
    s, ctx, _ = ready(tmp_path)
    w2 = "Wa11et2222222222222222222222222222222222222"
    u = upd(OWNER, text=f"{WALLET} Kowal\n{w2} Wieloryb")
    asyncio.run(AX.on_paste(u, ctx))
    names = sorted(ld["name"] for ld in s.db.leaders("active"))
    assert names == ["Kowal", "Wieloryb"] and all(s.db.follows_of(OWNER).values())
    text = u.effective_chat.send_message.call_args.args[0]
    assert "Dodano 2 portfele" in text and "Kopiowane portfele</b> (2)" in text


def test_help_and_glossary(tmp_path):
    s, ctx, _ = ready(tmp_path)
    u = upd(OWNER, data="ax:help")
    asyncio.run(AX.on_button(u, ctx))
    assert "w 4 krokach" in last_text(u)
    u = upd(OWNER, data="ax:dict")
    asyncio.run(AX.on_button(u, ctx))
    assert "Słowniczek" in last_text(u) and "Stop-loss" in last_text(u)
