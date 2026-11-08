"""Handel demo: ręczne kupno i sprzedaż za wirtualne środki, po prawdziwych cenach z giełdy."""
import asyncio
import html
import logging

from telegram import InlineKeyboardButton as B
from telegram import InlineKeyboardMarkup as KB
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from .bot import HOME, S, UI_TIMEOUT, require_accepted, show
from .config import friendly_error
from .engine import Skip, _resolve, fmt_amount, fmt_money
from .live import fmt_change, fmt_price

log = logging.getLogger(__name__)
BUY_FIXED = (10, 50, 100)
PCTS = (25, 50, 100)
DT = [B("« Handel demo", callback_data="dt")]


def demo_user(u):
    """Handel demo zawsze na koncie demo – także gdy kopiowanie działa na LIVE."""
    return dict(u, paper=1)


def coin_of(text: str) -> str:
    return text.strip().upper().split("/")[0]


async def _ack(update: Update, text=None, alert=False):
    if update.callback_query:
        try:
            await update.callback_query.answer(text, show_alert=alert)
        except BadRequest:
            pass


def _warning(u) -> str:
    if u["active"] and u["copy_mode"] == "mirror" and u["paper"]:
        return ("\n⚠️ <i>Masz włączone lustro na demo – bot wyrówna portfel do tradera i może cofnąć "
                "ręczne transakcje.</i>")
    return ""


async def menu_view(s, u) -> tuple[str, KB]:
    q = s.cfg.quote
    du = demo_user(u)
    market = s.engine.m(du)
    bal = s.db.paper_balances(u["tg_id"])
    cash = bal.get(q, 0)
    lines = ["🎮 <b>Handel demo</b>", f"Wirtualne środki, prawdziwe ceny · gotówka: <b>{fmt_money(cash)} {q}</b>", ""]
    buttons = []
    for coin in s.cfg.demo_coins:
        sym = await _resolve(market, f"{coin}/{q}")
        if not sym:
            continue
        try:
            price, change = await market.ticker(sym) if hasattr(market, "ticker") else (await market.price(sym), None)
        except Exception:  # noqa: BLE001
            continue
        held = bal.get(coin, 0)
        own = f" · masz {fmt_amount(held)} ≈ {fmt_money(held * price)} {q}" if held > 0 else ""
        lines.append(f"<b>{coin}</b> {fmt_price(price)}{fmt_change(change)}{own}")
        buttons.append(B(coin, callback_data=f"dt:{coin}"))
    others = [a for a, v in bal.items() if a != q and a not in s.cfg.demo_coins and v > 0]
    for coin in others[:6]:
        buttons.append(B(f"{coin} 💼", callback_data=f"dt:{coin}"))
    lines += ["", "Wybierz monetę albo wpisz komendę:", "<code>/kup PEPE 20</code> – kup za 20 " + q,
              "<code>/sprzedaj BTC 50%</code> – sprzedaj połowę"]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows += [[B("🔄 Odśwież", callback_data="dt"), B("💼 Portfel", callback_data="status")], HOME]
    return "\n".join(lines) + _warning(u), KB(rows)


async def coin_view(s, u, coin: str, notice: str = "") -> tuple[str, KB]:
    q = s.cfg.quote
    market = s.engine.m(demo_user(u))
    sym = await _resolve(market, f"{coin}/{q}")
    if not sym:
        return f"❌ {html.escape(coin)} nie jest dostępna na tej giełdzie.", KB([DT, HOME])
    price, change = await market.ticker(sym) if hasattr(market, "ticker") else (await market.price(sym), None)
    bal = s.db.paper_balances(u["tg_id"])
    held, cash = bal.get(coin, 0), bal.get(q, 0)
    lines = ([f"ℹ️ {html.escape(notice)}", ""] if notice else []) + [
             f"🎮 <b>{coin}</b> · {sym}",
             f"Cena: <b>{fmt_price(price)} {q}</b>{fmt_change(change)} (24 h)", "",
             f"Masz: <b>{fmt_amount(held)} {coin}</b> ≈ {fmt_money(held * price)} {q}",
             f"Gotówka: {fmt_money(cash)} {q}"]
    rows = [[B(f"🟢 Kup {v} {q}", callback_data=f"db:{coin}:{v}") for v in BUY_FIXED],
            [B(f"🟢 Kup {p}% gotówki", callback_data=f"db:{coin}:p{p}") for p in PCTS]]
    if held > 0:
        rows.append([B(f"🔴 Sprzedaj {p}%", callback_data=f"ds:{coin}:{p}") for p in PCTS])
    rows += [[B("🔄 Odśwież", callback_data=f"dt:{coin}")], DT + HOME]
    return "\n".join(lines) + _warning(u), KB(rows)


async def trade(s, u, coin: str, side: str, amount: str) -> str:
    """amount: „50” = 50 USDT (kupno) albo „50%”/„p50” = procent gotówki (kupno) lub posiadanych monet (sprzedaż)."""
    q = s.cfg.quote
    du = demo_user(u)
    market = s.engine.m(du)
    sym = await _resolve(market, f"{coin}/{q}")
    if not sym:
        raise Skip(f"{coin} nie jest dostępna na tej giełdzie")
    bal = s.db.paper_balances(u["tg_id"])
    pct = amount.endswith("%") or amount.startswith("p")
    num = float(amount.strip("%p").replace(",", "."))
    if num <= 0:
        raise Skip("kwota musi być większa od zera")
    price = await market.price(sym)
    if side == "buy":
        value = bal.get(q, 0) * min(num, 100) / 100 if pct else num
    else:
        held = bal.get(coin, 0)
        if held <= 0:
            raise Skip(f"nie masz {coin}")
        if pct:  # „50%” = połowa posiadanych monet; 100% z zapasem na zaokrąglenia (więcej niż ma i tak nie sprzeda)
            value = held * price * (1.001 if num >= 100 else num / 100)
        else:  # „/sprzedaj BTC 50” = sprzedaj za 50 USDT
            value = min(num, held * price)
    fill = await s.engine.trade_value(du, sym, side, value, tag="manual")
    verb = "Kupiono" if side == "buy" else "Sprzedano"
    return f"{verb} {fmt_amount(fill.amount)} {coin} za {fmt_money(fill.cost)} {q} (cena {fmt_price(fill.price)})"


# ---------- obsługa ----------

async def on_demo_button(update: Update, ctx):
    if not (u := await require_accepted(update, ctx)):
        return await _ack(update)
    s = S(ctx)
    data = update.callback_query.data
    try:
        if data == "dt":
            await _ack(update)
            return await show(update, *await asyncio.wait_for(menu_view(s, u), UI_TIMEOUT))
        parts = data.split(":")
        coin = parts[1]
        if parts[0] == "dt":
            await _ack(update)
            return await show(update, *await asyncio.wait_for(coin_view(s, u, coin), UI_TIMEOUT))
        side = "buy" if parts[0] == "db" else "sell"
        amount = parts[2] if side == "buy" else parts[2] + "%"
        await _ack(update, "⏳")
        try:
            msg = "✅ " + await trade(s, u, coin, side, amount)
        except Skip as e:
            msg = f"⚠️ {e}"
        return await show(update, *await coin_view(s, s.db.get_user(u["tg_id"]), coin, msg))
    except Exception as e:  # noqa: BLE001
        log.exception("Handel demo")
        return await show(update, f"❌ {html.escape(friendly_error(e) if str(e) else 'giełda nie odpowiada')}",
                          KB([DT, HOME]))


async def _cmd(update: Update, ctx, side: str):
    if not (u := await require_accepted(update, ctx)):
        return
    s = S(ctx)
    q = s.cfg.quote
    try:
        coin, amount = coin_of(ctx.args[0]), ctx.args[1]
    except IndexError:
        example = f"/kup BTC 50 – kup za 50 {q}\n/kup BTC 25% – za 25% gotówki" if side == "buy" else \
            "/sprzedaj BTC 50% – sprzedaj połowę\n/sprzedaj BTC 100% – sprzedaj wszystko"
        return await update.message.reply_text("Użycie (konto demo):\n" + example)
    try:
        msg = "✅ " + await trade(s, u, coin, side, amount)
    except Skip as e:
        msg = f"⚠️ {e}"
    except ValueError:
        msg = "⚠️ Nieprawidłowa kwota"
    except Exception as e:  # noqa: BLE001
        msg = f"❌ {friendly_error(e)}"
    text, kb = await coin_view(s, s.db.get_user(u["tg_id"]), coin, msg.lstrip("✅⚠️❌ "))
    await update.effective_chat.send_message(text, parse_mode="HTML", reply_markup=kb)


async def cmd_buy(update: Update, ctx):
    await _cmd(update, ctx, "buy")


async def cmd_sell(update: Update, ctx):
    await _cmd(update, ctx, "sell")


def register(app: Application):
    """Rejestrować PRZED bot.register."""
    app.add_handler(CallbackQueryHandler(on_demo_button, pattern=r"^(dt|dt:[A-Z0-9]+|db:[A-Z0-9]+:p?\d+|ds:[A-Z0-9]+:\d+)$"))
    app.add_handler(CommandHandler("kup", cmd_buy))
    app.add_handler(CommandHandler("sprzedaj", cmd_sell))
