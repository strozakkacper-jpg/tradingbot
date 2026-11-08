"""Przypięta wiadomość „na żywo” u góry czatu: ceny, portfel i stan kopiowania, odświeżane co minutę."""
import asyncio
import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden

from .engine import _resolve, fmt_money, fmt_price  # noqa: F401 (fmt_price używane też przez demo)

log = logging.getLogger(__name__)


def fmt_change(ch: float | None) -> str:
    if ch is None:
        return ""
    return f" {'🟢' if ch >= 0 else '🔴'}{ch:+.1f}%"


async def prices_line(s, market, max_age: float = 30) -> str:
    q = s.cfg.quote
    parts = []
    for coin in s.cfg.live_coins:
        try:
            sym = await _resolve(market, f"{coin}/{q}")
            if not sym:
                continue
            price, change = await market.ticker(sym, max_age) if hasattr(market, "ticker") else (
                await market.price(sym), None)
            parts.append(f"<b>{coin}</b> {fmt_price(price)}{fmt_change(change)}")
        except Exception:  # noqa: BLE001
            log.debug("Brak ceny %s", coin)
    return " · ".join(parts)


def cached_prices_line(s, market) -> str:
    """To samo, ale tylko z pamięci podręcznej – bez łączenia z giełdą (szybki pulpit)."""
    if not hasattr(market, "cached"):
        return ""
    parts = []
    for coin in s.cfg.live_coins:
        for q in (s.cfg.quote, "USDC", "USD"):
            hit = market.cached(f"{coin}/{q}")
            if hit:
                parts.append(f"<b>{coin}</b> {fmt_price(hit[0])}{fmt_change(hit[1])}")
                break
    return " · ".join(parts)


async def live_text(s, u) -> str:
    q = s.cfg.quote
    market = s.engine.m(u)
    now = datetime.now(ZoneInfo(s.cfg.timezone)).strftime("%H:%M")
    lines = [f"📊 <b>Na żywo</b> · {now}"]
    prices = await prices_line(s, market)
    if prices:
        lines.append(prices)
    try:
        equity = await asyncio.wait_for(s.engine.equity_of(u), 20)
        base = s.cfg.paper_start if u["paper"] else (u["start_equity"] or equity)
        pct = (equity / base - 1) * 100 if base else 0
        lines.append(f"💼 {fmt_money(equity)} {q} ({'📈' if pct >= 0 else '📉'} {pct:+.2f}%) · "
                     f"{'🧪 demo' if u['paper'] else '💰 LIVE'}")
    except Exception:  # noqa: BLE001
        lines.append("💼 saldo chwilowo niedostępne")
    copied = s.db.copied_leaders(u["tg_id"])
    names = ", ".join(html.escape(c["name"]) for c in copied)
    state = "▶️" if u["active"] else "⏸"
    lines.append(f"{state} " + (f"Kopiujesz: {names}" if names else "Nikogo nie kopiujesz"))
    last = next((r for r in s.db.history(u["tg_id"], 5) if r["status"] == "filled"), None)
    if last:
        t = datetime.fromtimestamp(last["ts"] / 1000, ZoneInfo(s.cfg.timezone)).strftime("%H:%M")
        lines.append(f"Ostatnio: {'🟢' if last['side'] == 'buy' else '🔴'} {last['symbol']} "
                     f"{fmt_money(last['cost'])} {q} · {t}")
    return "\n".join(lines)


async def refresh(bot, s, u) -> None:
    """Aktualizuje przypiętą wiadomość (albo tworzy i przypina nową)."""
    text = await live_text(s, u)
    uid = u["tg_id"]
    if u["pin_msg_id"]:
        try:
            await bot.edit_message_text(text, chat_id=uid, message_id=u["pin_msg_id"], parse_mode=ParseMode.HTML)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
            log.info("Przypięta wiadomość %s zniknęła (%s) – tworzę nową", uid, e)
    msg = await bot.send_message(uid, text, parse_mode=ParseMode.HTML, disable_notification=True)
    s.db.update_user(uid, pin_msg_id=msg.message_id)
    try:
        await bot.pin_chat_message(uid, msg.message_id, disable_notification=True)
    except BadRequest as e:
        log.warning("Nie przypięto wiadomości u %s: %s", uid, e)


async def disable(bot, s, u) -> None:
    s.db.update_user(u["tg_id"], live_pin=0, pin_msg_id=None)
    if u["pin_msg_id"]:
        try:
            await bot.unpin_chat_message(u["tg_id"], message_id=u["pin_msg_id"])
            await bot.delete_message(u["tg_id"], u["pin_msg_id"])
        except Exception:  # noqa: BLE001
            pass


async def live_job(ctx) -> None:
    s = ctx.application.bot_data["svc"]
    for u in s.db.live_pin_users():
        try:
            await refresh(ctx.bot, s, u)
        except Forbidden:  # użytkownik zablokował bota
            s.db.update_user(u["tg_id"], live_pin=0, pin_msg_id=None)
        except Exception:  # noqa: BLE001
            log.exception("Nie odświeżono informacji na żywo dla %s", u["tg_id"])
