"""Zadania okresowe: zapis wartości portfeli, stop-loss, wygasanie subskrypcji."""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from .billing import has_access, invoice_link
from .db import now_ms, ref_id
from .engine import fmt_money
from .reports import fmt_pct

log = logging.getLogger(__name__)


async def snapshot_job(ctx: ContextTypes.DEFAULT_TYPE):
    s = ctx.application.bot_data["svc"]
    for lead in s.db.leaders("active"):
        if lead["source"] == "sol":  # portfel wzorcowy Solany – wartość w SOL
            if getattr(s, "sol", None):
                try:
                    s.db.add_equity(ref_id(lead["id"]), (await s.sol.report(lead["id"], max_age=0))["equity"], True)
                except Exception:  # noqa: BLE001
                    log.exception("Nie zapisano wartości portfela Solany tradera %s", lead["id"])
            continue
        try:
            s.db.ensure_reference(lead["id"], s.cfg.paper_start, s.cfg.quote)
            rid = ref_id(lead["id"])
            s.db.add_equity(rid, await s.engine.m(lead).equity(s.db.paper_balances(rid)), True)
        except Exception:  # noqa: BLE001
            log.exception("Nie zapisano wartości portfela wzorcowego tradera %s", lead["id"])

    # wartość portfela zapisujemy też u osób z wyłączonym kopiowaniem – pulpit i wykres nie pokazują wtedy starych liczb
    for u in s.db.approved_users():
        if u["active"] or not u["accepted"]:
            continue
        try:
            s.db.add_equity(u["tg_id"], await s.engine.equity_of(u), bool(u["paper"]))
        except Exception:  # noqa: BLE001
            log.debug("Nie wyceniono portfela %s", u["tg_id"])

    for u in s.db.active_users():
        if not has_access(s.cfg, u):
            await expire_subscription(ctx, s, u)
            continue
        try:
            equity = await s.engine.equity_of(u)
        except Exception:  # noqa: BLE001
            log.exception("Nie wyceniono portfela %s", u["tg_id"])
            continue
        s.db.add_equity(u["tg_id"], equity, bool(u["paper"]))
        sl, start = u["stop_loss"] or 0, u["start_equity"] or 0
        if sl > 0 and start > 0 and equity <= start * (1 - sl / 100):
            await trigger_stop_loss(s, u, equity)


async def trigger_stop_loss(s, u, equity: float):
    uid = u["tg_id"]
    s.db.update_user(uid, active=0)
    drop = (1 - equity / u["start_equity"]) * 100
    log.warning("Stop-loss u %s: -%.1f%%", uid, drop)
    await s.engine.notify(uid, (
        f"🛑 Stop-loss zadziałał\n\nWartość portfela spadła o {drop:.1f}% "
        f"(z {fmt_money(u['start_equity'])} do {fmt_money(equity)} {s.cfg.quote}).\n"
        "Kopiowanie wyłączone, sprzedaję pozycje otwarte przez bota…"))
    res = await s.engine.close_positions(u)
    try:
        s.db.add_equity(uid, await s.engine.equity_of(u), bool(u["paper"]))
    except Exception:  # noqa: BLE001
        pass
    await s.engine.notify(uid, f"Gotowe. Sprzedane pozycje: {res.get('filled', 0)}, błędy: {res.get('error', 0)}.\n"
                               "Możesz włączyć kopiowanie ponownie w /menu.")


async def expire_subscription(ctx, s, u):
    s.db.update_user(u["tg_id"], active=0)
    try:
        link = await invoice_link(ctx.bot, s.cfg, u["tg_id"])
        await ctx.bot.send_message(
            u["tg_id"], "⏰ Twoja subskrypcja wygasła, więc kopiowanie zostało wstrzymane.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                f"💎 Odnów za {s.cfg.sub_price_stars} ⭐", url=link)]]))
    except Exception:  # noqa: BLE001
        log.exception("Nie wysłano informacji o wygaśnięciu do %s", u["tg_id"])


async def daily_report_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Wieczorne podsumowanie dnia dla kopiujących."""
    s = ctx.application.bot_data["svc"]
    day_ago = now_ms() - 24 * 3600 * 1000
    for u in s.db.active_users():
        if not u["daily_report"]:
            continue
        try:
            equity = await s.engine.equity_of(u)
        except Exception:  # noqa: BLE001
            continue
        series = s.db.equity_series(u["tg_id"], bool(u["paper"]), since=day_ago)
        trades = [r for r in s.db.history(u["tg_id"], 50) if r["ts"] >= day_ago and r["status"] == "filled"]
        copied = s.db.copied_leaders(u["tg_id"])
        lines = [f"🌙 <b>Podsumowanie dnia</b>{' (demo)' if u['paper'] else ''}", ""]
        lines.append(f"Wartość portfela: <b>{fmt_money(equity)} {s.cfg.quote}</b>")
        if series and series[0][1] > 0:
            ch = equity / series[0][1] - 1
            lines.append(f"Zmiana 24 h: {'📈' if ch >= 0 else '📉'} {fmt_pct(ch)}")
        lines.append(f"Transakcje dziś: {len(trades)}")
        if copied:
            lines.append("Kopiujesz: " + ", ".join(c["name"] for c in copied))
        lines.append("\nWyłączysz te raporty w ⚙️ Ustawieniach.")
        try:
            await ctx.bot.send_message(u["tg_id"], "\n".join(lines), parse_mode="HTML")
        except Exception:  # noqa: BLE001
            log.debug("Raport dzienny nie dotarł do %s", u["tg_id"])
