"""Interfejs Telegram: menu, podłączanie giełdy, ustawienia, panel admina."""
import asyncio
import html
import logging
from dataclasses import dataclass

from telegram import InlineKeyboardButton as B
from telegram import InlineKeyboardMarkup as KB
from telegram import Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler,
                          MessageHandler, PreCheckoutQueryHandler, filters)

from .billing import PERIOD_SECONDS, has_access, invoice_link, needs_subscription, paywall_on
from .config import KEY_HINTS, Config, exchange_name, friendly_error, needs_password, normalize_secret
from .db import DB, now_ms
from .engine import SIDE_PL, CopyEngine, Market, fmt_amount, fmt_money, user_exchange
from .reports import fmt_date
from .security import Vault
from .sizing import LeaderOrder

log = logging.getLogger(__name__)

DISCLAIMER = (
    "⚠️ <b>Zanim zaczniesz</b>\n\n"
    "• Handel kryptowalutami jest ryzykowny – możesz stracić całość wpłaconych środków.\n"
    "• Wyniki lidera z przeszłości <b>nie gwarantują</b> przyszłych zysków.\n"
    "• Bot kopiuje transakcje automatycznie, ale nie jest doradcą inwestycyjnym.\n"
    "• Domyślnie działasz w <b>trybie demo</b> (wirtualne {start} {quote}). Na prawdziwe środki "
    "przełączasz się sam, po podłączeniu kluczy API.\n"
    "• Klucze API twórz <b>wyłącznie z uprawnieniem do handlu</b> – nigdy z prawem do wypłat."
)

KEY, SECRET, PASSWORD, CHOOSE = range(4)
MULTIPLIERS = (0.25, 0.5, 1.0, 1.5, 2.0)
LIMITS = (10, 25, 50, 100, 250, 1000)
STOP_LOSSES = (0, 10, 20, 30, 50)
PRICE_MOVES = (0, 1, 2, 5)
UI_TIMEOUT = 20  # s – dłużej nie czekamy na giełdę w odpowiedzi na kliknięcie


@dataclass
class Services:
    cfg: Config
    db: DB
    vault: Vault
    market: Market
    engine: CopyEngine
    watchers: object | None  # watchers.Watchers


def S(ctx: ContextTypes.DEFAULT_TYPE) -> Services:
    return ctx.application.bot_data["svc"]


def admin_ids(s: "Services") -> set[int]:
    """Admini z .env albo – jeśli nie podano – właściciel: pierwsza osoba, która napisała /start."""
    owner = s.db.get_state("owner")
    return set(s.cfg.admin_ids) | ({int(owner)} if owner else set())


def is_admin(ctx, update: Update) -> bool:
    return update.effective_user.id in admin_ids(S(ctx))


# ---------- widoki ----------

def menu_view(s: Services, u) -> tuple[str, KB]:
    paused = s.db.get_state("paused") == "1"
    connected = bool(u["api_key"])
    text = (
        "🤖 <b>Copy Trading</b>\n\n"
        f"Kopiowanie: {'▶️ <b>włączone</b>' if u['active'] else '⏸ wyłączone'}\n"
        f"Tryb: {'🧪 demo' if u['paper'] else '💰 <b>LIVE</b>'}\n"
        f"Mnożnik: {u['multiplier']:g}x · Limit: {u['max_trade']:g} {s.cfg.quote}/zlecenie\n"
        f"Giełda: {exchange_name(user_exchange(s.cfg, u))} {'✅ podłączona' if connected else '— niepodłączona'}\n"
        f"Stop-loss: {sl_txt(u)}"
    )
    lead = s.db.get_leader(u["leader_id"])
    if lead and lead["status"] == "active":
        text = text.replace("🤖 <b>Copy Trading</b>\n\n", f"🤖 <b>Copy Trading</b>\n\nKopiujesz: <b>"
                            f"{html.escape(lead['name'])}</b>\n", 1)
    else:
        text += "\n\n👉 Wybierz tradera do kopiowania: 🏆 Traderzy"
    if paused:
        text += "\n⏸ Kopiowanie wstrzymane przez admina"
    if paywall_on(s.cfg):
        text += "\nSubskrypcja: " + (f"💎 do {fmt_date(u['paid_until'])}" if (u["paid_until"] or 0) > now_ms()
                                       else "brak")
    rows = []
    if s.cfg.webapp_url:
        rows.append([B("📈 Otwórz panel", web_app=WebAppInfo(f"{s.cfg.webapp_url}/app"))])
    rows += [
        [B("📊 Portfel", callback_data="status"), B("🏆 Traderzy", callback_data="traders")],
        [B("⏸ Zatrzymaj kopiowanie" if u["active"] else "▶️ Zacznij kopiować", callback_data="toggle")],
        [B("⚙️ Ustawienia", callback_data="settings"), B("📜 Historia", callback_data="history")],
        [B("🔑 Podłącz giełdę", callback_data="connect"),
         B("Na 💰 LIVE" if u["paper"] else "Na 🧪 demo", callback_data="mode")],
    ]
    if paywall_on(s.cfg):
        rows.append([B("💎 Subskrypcja", callback_data="sub")])
    return text, KB(rows)


BACK = [B("« Menu", callback_data="menu")]


def pm_txt(u) -> str:
    return f"maks. +{u['max_price_move']:g}%" if u["max_price_move"] else "wyłączona"


def sl_txt(u) -> str:
    return f"-{u['stop_loss']:g}%" if u["stop_loss"] else "wyłączony"


def sub_view(s: Services, u) -> tuple[str, KB | None]:
    active = (u["paid_until"] or 0) > now_ms()
    scope = "kopiowania na prawdziwych środkach (demo jest za darmo)" if s.cfg.sub_scope == "live" else "kopiowania"
    text = (f"💎 <b>Subskrypcja</b>\n\n{s.cfg.sub_price_stars} ⭐ za 30 dni – wymagana do {scope}.\n"
            "Płacisz gwiazdkami Telegrama (kupisz je w aplikacji). Odnawia się co miesiąc, anulujesz w "
            "Ustawienia → Moje gwiazdki.\n\n"
            + (f"✅ Aktywna do <b>{fmt_date(u['paid_until'])}</b>" if active else "Status: brak subskrypcji"))
    return text, None


def settings_view(s: Services, u) -> tuple[str, KB]:
    text = (
        "⚙️ <b>Ustawienia</b>\n\n"
        f"<b>Mnożnik</b>: {u['multiplier']:g}x – gdy lider wyda 10% salda, Ty wydasz "
        f"{min(10 * u['multiplier'], 100):g}%.\n"
        f"<b>Limit</b>: {u['max_trade']:g} {s.cfg.quote} – maksymalna wartość pojedynczego zakupu. "
        "Sprzedaże nie są limitowane, żeby zawsze zamykać pozycje razem z liderem.\n"
        f"<b>Stop-loss</b>: {sl_txt(u)} – gdy portfel spadnie o tyle od momentu włączenia kopiowania, "
        "bot wyłączy kopiowanie i sprzeda pozycje, które sam otworzył.\n\n"
        f"<b>Ochrona ceny</b>: {pm_txt(u)} – nie kupuj, jeśli cena urosła już "
        "o więcej od ceny zakupu tradera (nie gonimy rynku).\n"
        f"<b>Raport dzienny</b>: {'włączony' if u['daily_report'] else 'wyłączony'}\n\n"
        "Własne wartości: /mnoznik 1.2 · /limit 75 · /stoploss 25"
    )
    mark = lambda cur, v: f"• {v:g} •" if abs(cur - v) < 1e-9 else f"{v:g}"  # noqa: E731
    kb = KB([
        [B(mark(u["multiplier"], m) + "x", callback_data=f"mult:{m}") for m in MULTIPLIERS],
        [B(mark(u["max_trade"], v), callback_data=f"lim:{v}") for v in LIMITS[:3]],
        [B(mark(u["max_trade"], v), callback_data=f"lim:{v}") for v in LIMITS[3:]],
        [B("SL: " + ("• wył •" if not u["stop_loss"] else "wył"), callback_data="sl:0")]
        + [B(mark(u["stop_loss"], v) + "%", callback_data=f"sl:{v}") for v in STOP_LOSSES[1:]],
        [B("Cena: " + ("• wył •" if not u["max_price_move"] else "wył"), callback_data="pm:0")]
        + [B(mark(u["max_price_move"], v) + "%", callback_data=f"pm:{v}") for v in PRICE_MOVES[1:]],
        [B(f"🌙 Raport dzienny: {'wł ✅' if u['daily_report'] else 'wył'}", callback_data="daily")],
        [B("🔄 Zresetuj konto demo", callback_data="paper_reset")],
        BACK,
    ])
    return text, kb


async def status_text(s: Services, u) -> str:
    q = s.cfg.quote
    if u["paper"]:
        bal = s.db.paper_balances(u["tg_id"])
        title = "🧪 <b>Portfel demo</b>"
    else:
        bal = await s.engine.live_balance(u)
        title = f"💰 <b>Portfel {exchange_name(user_exchange(s.cfg, u))}</b>"
    lines = [title, ""]
    if not bal:
        lines.append("Brak środków.")
    for asset, amount in sorted(bal.items(), key=lambda kv: kv[0] != q):
        lines.append(f"• {html.escape(asset)}: {fmt_amount(amount)}")
    try:
        equity = await s.engine.m(u).equity(bal)
        lines += ["", f"Wartość: <b>{fmt_money(equity)} {q}</b>"]
        if u["paper"]:
            pnl = equity - s.cfg.paper_start
            pct = pnl / s.cfg.paper_start * 100
            lines.append(f"Wynik: {'📈' if pnl >= 0 else '📉'} {pnl:+.2f} {q} ({pct:+.2f}%)")
        if u["active"] and u["start_equity"]:
            ch = (equity / u["start_equity"] - 1) * 100
            lines.append(f"Od włączenia kopiowania: {ch:+.2f}% (stop-loss: {sl_txt(u)})")
    except Exception:  # noqa: BLE001
        log.exception("Nie udało się wycenić portfela")
        lines += ["", "(nie udało się pobrać cen)"]
    return "\n".join(lines)


def history_text(s: Services, u) -> str:
    rows = s.db.history(u["tg_id"], 10)
    if not rows:
        return "📜 <b>Historia</b>\n\nJeszcze nic nie skopiowano."
    icon = {"filled": "✅", "skipped": "⚠️", "error": "❌"}
    lines = ["📜 <b>Ostatnie transakcje</b>", ""]
    for r in rows:
        mode = "demo" if r["paper"] else "LIVE"
        if r["status"] == "filled":
            lines.append(f"{icon['filled']} {SIDE_PL[r['side']]} {r['symbol']} {fmt_amount(r['amount'])} "
                         f"@ {fmt_money(r['price'])} ({fmt_money(r['cost'])} {s.cfg.quote}, {mode})")
        else:
            lines.append(f"{icon.get(r['status'], '•')} {SIDE_PL[r['side']]} {r['symbol']}: "
                         f"{html.escape((r['info'] or '')[:80])}")
    return "\n".join(lines)


# ---------- helpery ----------

def get_user(ctx, update: Update):
    s = S(ctx)
    tu = update.effective_user
    return s.db.ensure_user(tu.id, tu.username, s.cfg.paper_start, s.cfg.quote)


async def show(update: Update, text: str, kb: KB | None = None):
    """Edytuje wiadomość z przyciskami albo wysyła nową."""
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
    else:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def require_accepted(update: Update, ctx) -> object | None:
    u = get_user(ctx, update)
    if u["accepted"]:
        return u
    s = S(ctx)
    await show(update, DISCLAIMER.format(start=f"{s.cfg.paper_start:g}", quote=s.cfg.quote),
               KB([[B("✅ Rozumiem ryzyko, zaczynam", callback_data="accept")]]))
    return None


# ---------- komendy użytkownika ----------

async def cmd_start(update: Update, ctx):
    s = S(ctx)
    if not s.cfg.admin_ids and s.db.get_state("owner") is None:
        s.db.set_state("owner", str(update.effective_user.id))
        await update.effective_message.reply_text(
            "👑 Jesteś pierwszą osobą w tym bocie, więc zostajesz jego <b>administratorem</b>.\n"
            "Panel: /admin", parse_mode=ParseMode.HTML)
    if u := await require_accepted(update, ctx):
        await show(update, *menu_view(S(ctx), u))


async def cmd_help(update: Update, ctx):
    await update.effective_message.reply_text(
        "ℹ️ <b>Jak to działa?</b>\n\n"
        "Wybierasz tradera z rankingu (🏆 Traderzy). Gdy trader kupi za X% swojego salda, bot w około sekundę kupi "
        "u Ciebie za X% Twojego salda (× mnożnik, maks. limit). Gdy sprzeda część pozycji, sprzedasz ten sam "
        "procent.\n\nChcesz, żeby inni kopiowali Ciebie? /zostan_traderem\n\n"
        "/menu – menu główne\n/traderzy – ranking traderów\n/trader – Twój panel tradera\n/polacz – podłącz klucze API giełdy\n"
        "/mnoznik 1.5 – ustaw mnożnik\n/limit 100 – limit na zlecenie\n/stoploss 25 – stop-loss w %\n"
        "/subskrypcja – status subskrypcji\n/paysupport – pomoc w sprawie płatności\n"
        "/anuluj – przerwij podłączanie kluczy",
        parse_mode=ParseMode.HTML)


async def cmd_multiplier(update: Update, ctx):
    if not (u := await require_accepted(update, ctx)):
        return
    try:
        v = float(ctx.args[0].replace(",", "."))
        assert 0.01 <= v <= 10
    except (IndexError, ValueError, AssertionError):
        return await update.message.reply_text("Użycie: /mnoznik 1.5 (od 0.01 do 10)")
    S(ctx).db.update_user(u["tg_id"], multiplier=v)
    await update.message.reply_text(f"✅ Mnożnik ustawiony na {v:g}x")


async def cmd_limit(update: Update, ctx):
    if not (u := await require_accepted(update, ctx)):
        return
    try:
        v = float(ctx.args[0].replace(",", "."))
        assert v > 0
    except (IndexError, ValueError, AssertionError):
        return await update.message.reply_text(f"Użycie: /limit 100 (w {S(ctx).cfg.quote})")
    S(ctx).db.update_user(u["tg_id"], max_trade=v)
    await update.message.reply_text(f"✅ Limit na zlecenie: {v:g} {S(ctx).cfg.quote}")


async def cmd_stoploss(update: Update, ctx):
    if not (u := await require_accepted(update, ctx)):
        return
    try:
        v = float(ctx.args[0].replace(",", ".").rstrip("%"))
        assert 0 <= v < 100
    except (IndexError, ValueError, AssertionError):
        return await update.message.reply_text("Użycie: /stoploss 25 (procent, 0 = wyłączony)")
    S(ctx).db.update_user(u["tg_id"], stop_loss=v)
    await update.message.reply_text(f"✅ Stop-loss: {'-' + format(v, 'g') + '%' if v else 'wyłączony'}")


async def cmd_leader(update: Update, ctx):
    if await require_accepted(update, ctx):
        from .traders import show_traders
        await show_traders(update, ctx)


async def cmd_sub(update: Update, ctx):
    if u := await require_accepted(update, ctx):
        await show_sub(update, ctx, u)


async def show_sub(update: Update, ctx, u, reason: str = ""):
    s = S(ctx)
    if not paywall_on(s.cfg):
        return await show(update, "Bot jest obecnie darmowy 🎉", KB([BACK]))
    text, _ = sub_view(s, u)
    rows = []
    if (u["paid_until"] or 0) <= now_ms():
        try:
            link = await invoice_link(ctx.bot, s.cfg, u["tg_id"])
            rows.append([B(f"💎 Kup za {s.cfg.sub_price_stars} ⭐", url=link)])
        except Exception:  # noqa: BLE001
            log.exception("Nie utworzono faktury")
            text += "\n\n❌ Nie udało się utworzyć płatności, spróbuj później."
    await show(update, (reason + "\n\n" if reason else "") + text, KB(rows + [BACK]))


async def on_precheckout(update: Update, ctx):
    q = update.pre_checkout_query
    ok = q.invoice_payload == f"sub:{q.from_user.id}" and q.currency == "XTR"
    await q.answer(ok=ok, error_message=None if ok else "Ta płatność jest nieaktualna, otwórz /subskrypcja.")


async def on_payment(update: Update, ctx):
    s = S(ctx)
    sp = update.message.successful_payment
    exp = sp.subscription_expiration_date
    until = int(exp.timestamp() * 1000) if hasattr(exp, "timestamp") else int(exp * 1000) if exp else \
        now_ms() + PERIOD_SECONDS * 1000
    s.db.add_payment(update.effective_user.id, sp.telegram_payment_charge_id, sp.total_amount,
                     bool(sp.is_recurring), until)
    await update.message.reply_text(
        f"💎 Dziękujemy! Subskrypcja aktywna do <b>{fmt_date(until)}</b>.\nWłącz kopiowanie w /menu.",
        parse_mode=ParseMode.HTML)
    for admin in admin_ids(s):
        try:
            await ctx.bot.send_message(admin, f"💰 Płatność {sp.total_amount} ⭐ od {update.effective_user.id}"
                                              f"{' (odnowienie)' if sp.is_recurring else ''}")
        except Exception:  # noqa: BLE001
            pass


async def cmd_paysupport(update: Update, ctx):
    await update.effective_message.reply_text(
        "💬 Pomoc w sprawie płatności\n\nOpisz problem w odpowiedzi do tej wiadomości albo napisz do "
        "administratora bota. Subskrypcję anulujesz w Telegramie: Ustawienia → Moje gwiazdki. "
        "Zwroty rozpatrujemy indywidualnie.")


async def on_button(update: Update, ctx):
    q = update.callback_query
    s = S(ctx)
    data = q.data
    acked = False

    async def ack(text=None, show_alert=False):
        """Odpowiedź na kliknięcie – raz i jak najszybciej, żeby przycisk się nie „kręcił”."""
        nonlocal acked
        if acked:
            return
        acked = True
        try:
            await q.answer(text, show_alert=show_alert)
        except BadRequest:  # kliknięcie sprzed restartu bota – za stare, żeby odpowiedzieć
            pass
    if data == "accept":
        get_user(ctx, update)
        s.db.update_user(update.effective_user.id, accepted=1)
    if not (u := await require_accepted(update, ctx)):
        return await ack()
    uid = u["tg_id"]
    alert = None

    if data == "toggle":
        if u["active"]:
            s.db.update_user(uid, active=0)
            alert = "Kopiowanie wyłączone"
        elif not (lead := s.db.get_leader(u["leader_id"])) or lead["status"] != "active":
            await ack("Najpierw wybierz tradera, którego chcesz kopiować.", show_alert=True)
            from .traders import show_traders
            return await show_traders(update, ctx)
        elif not u["paper"] and not u["api_key"]:
            alert = "Najpierw podłącz giełdę albo przełącz się na demo."
        elif not has_access(s.cfg, u):
            await ack()
            return await show_sub(update, ctx, u, "Żeby włączyć kopiowanie, potrzebujesz subskrypcji.")
        else:
            await ack("⏳ Włączam…")
            try:
                start = await asyncio.wait_for(s.engine.equity_of(u), UI_TIMEOUT)
            except Exception as e:  # noqa: BLE001
                return await show(update, "❌ Nie udało się pobrać salda z giełdy"
                                          f"{': ' + html.escape(str(e)[:150]) if str(e) else ' (przekroczony czas)'}."
                                          "\nSpróbuj ponownie za chwilę.", KB([BACK]))
            s.db.update_user(uid, active=1, start_equity=start)
            alert = "Kopiowanie włączone ▶️"
    elif data == "mode":
        if u["paper"]:
            if not u["api_key"]:
                await ack("Najpierw podłącz giełdę (🔑).", show_alert=True)
                return
            await ack()
            return await show(update,
                              "💰 <b>Przełączyć na prawdziwe środki?</b>\n\nOd teraz bot będzie składał "
                              f"<b>prawdziwe zlecenia</b> na Twoim koncie {exchange_name(user_exchange(s.cfg, u))}, "
                              f"maks. {u['max_trade']:g} {s.cfg.quote} na zakup. Stop-loss: {sl_txt(u)}."
                              + ("\n\n💎 Kopiowanie na LIVE wymaga subskrypcji." if needs_subscription(s.cfg, False)
                                 and not has_access(s.cfg, u, paper=False) else ""),
                              KB([[B("Tak, przełącz na LIVE", callback_data="mode_live")], BACK]))
        s.db.update_user(uid, paper=1, active=0)
        alert = "Tryb demo 🧪 – włącz kopiowanie ponownie"
    elif data == "mode_live":
        if u["api_key"]:
            s.db.update_user(uid, paper=0, active=0)
            alert = "Tryb LIVE 💰 – włącz kopiowanie, gdy będziesz gotowy"
    elif data.startswith("mult:"):
        s.db.update_user(uid, multiplier=float(data[5:]))
        await ack()
        return await show(update, *settings_view(s, s.db.get_user(uid)))
    elif data.startswith("lim:"):
        s.db.update_user(uid, max_trade=float(data[4:]))
        await ack()
        return await show(update, *settings_view(s, s.db.get_user(uid)))
    elif data.startswith("sl:"):
        s.db.update_user(uid, stop_loss=float(data[3:]))
        await ack()
        return await show(update, *settings_view(s, s.db.get_user(uid)))
    elif data == "leader":
        await ack()
        from .traders import show_profile
        return await show_profile(update, ctx, u["leader_id"])
    elif data.startswith("pm:"):
        s.db.update_user(uid, max_price_move=float(data[3:]))
        await ack()
        return await show(update, *settings_view(s, s.db.get_user(uid)))
    elif data == "daily":
        s.db.update_user(uid, daily_report=0 if u["daily_report"] else 1)
        await ack()
        return await show(update, *settings_view(s, s.db.get_user(uid)))
    elif data == "closeall":
        await ack()
        return await show(update, "🧹 <b>Sprzedać wszystkie pozycje otwarte przez bota?</b>\n\nMonet, których bot "
                                  "nie kupował, nie ruszamy. Kopiowanie zostanie wyłączone.",
                          KB([[B("Tak, sprzedaj wszystko", callback_data="closeall_ok")], BACK]))
    elif data == "closeall_ok":
        await ack("Sprzedaję…")
        s.db.update_user(uid, active=0)
        res = await s.engine.close_positions(u)
        return await show(update, f"🧹 Gotowe. Sprzedane pozycje: {res.get('filled', 0)}, błędy: "
                                  f"{res.get('error', 0)}. Kopiowanie wyłączone.", KB([BACK]))
    elif data == "sub":
        await ack()
        return await show_sub(update, ctx, u)
    elif data == "paper_reset":
        s.db.paper_reset(uid, s.cfg.paper_start, s.cfg.quote)
        await ack(f"Konto demo: {s.cfg.paper_start:g} {s.cfg.quote}", show_alert=True)
        return await show(update, *settings_view(s, u))
    elif data == "settings":
        await ack()
        return await show(update, *settings_view(s, u))
    elif data == "status":
        await ack("Pobieram saldo…")
        try:
            text = await asyncio.wait_for(status_text(s, u), UI_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            text = f"❌ Nie udało się pobrać salda: {html.escape(str(e)[:200])}"
        return await show(update, text, KB([[B("🔄 Odśwież", callback_data="status"),
                                             B("🧹 Sprzedaj wszystko", callback_data="closeall")], BACK]))
    elif data == "history":
        await ack()
        return await show(update, history_text(s, u), KB([BACK]))

    notice = alert if acked else None  # gdy już odpowiedzieliśmy, komunikat trafia do treści menu
    await ack(alert)
    text, kb = menu_view(s, s.db.get_user(uid))
    await show(update, (f"ℹ️ {html.escape(notice)}\n\n" if notice else "") + text, kb)


# ---------- podłączanie kluczy API ----------

def exchange_kb(cfg: Config, prefix: str) -> KB:
    names = [B(exchange_name(e), callback_data=f"{prefix}{e}") for e in cfg.exchanges]
    return KB([names[i:i + 3] for i in range(0, len(names), 3)])


async def connect_start(update: Update, ctx):
    if update.callback_query:
        await update.callback_query.answer()
    if not await require_accepted(update, ctx):
        return ConversationHandler.END
    await update.effective_chat.send_message(
        "🔑 <b>Podłączenie giełdy</b>\n\nWybierz giełdę, na której masz konto. Trader może handlować na innej "
        "giełdzie niż Ty – bot kopiuje te same pary (np. BTC/USDT) na Twojej giełdzie.\n\n/anuluj – przerwij",
        parse_mode=ParseMode.HTML, reply_markup=exchange_kb(S(ctx).cfg, "cex:"))
    return CHOOSE


async def got_exchange(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    ex = q.data[4:]
    if ex not in S(ctx).cfg.exchanges:
        return CHOOSE
    ctx.user_data["exchange"] = ex
    extra = "\n4. Zapamiętaj <b>passphrase</b> (hasło klucza) – bot o nie zapyta." if needs_password(ex) else ""
    if ex in KEY_HINTS:
        extra += "\n\n" + KEY_HINTS[ex]
    await q.edit_message_text(
        f"🔑 <b>Podłączenie {exchange_name(ex)}</b>\n\n"
        "1. W ustawieniach giełdy utwórz klucz API.\n"
        "2. Zaznacz <b>tylko</b> handel spot. <b>Wyłącz wypłaty!</b>\n"
        f"3. Najlepiej ogranicz klucz do IP serwera bota.{extra}\n\n"
        "Wyślij teraz <b>API Key</b> (wiadomość zostanie od razu usunięta).\n/anuluj – przerwij",
        parse_mode=ParseMode.HTML)
    return KEY


async def _wipe(update: Update):
    try:
        await update.message.delete()
    except BadRequest:
        pass


async def got_key(update: Update, ctx):
    ctx.user_data["api_key"] = update.message.text.strip()
    await _wipe(update)
    await update.effective_chat.send_message("Teraz wyślij <b>API Secret</b>.", parse_mode=ParseMode.HTML)
    return SECRET


async def got_secret(update: Update, ctx):
    ctx.user_data["api_secret"] = normalize_secret(update.message.text)
    await _wipe(update)
    if needs_password(ctx.user_data.get("exchange") or S(ctx).cfg.exchange_id):
        await update.effective_chat.send_message("Wyślij <b>passphrase</b> klucza API.", parse_mode=ParseMode.HTML)
        return PASSWORD
    return await _finish_connect(update, ctx, None)


async def got_password(update: Update, ctx):
    pw = update.message.text.strip()
    await _wipe(update)
    return await _finish_connect(update, ctx, pw)


async def _finish_connect(update: Update, ctx, password):
    s = S(ctx)
    key, secret = ctx.user_data.pop("api_key", ""), ctx.user_data.pop("api_secret", "")
    ex = ctx.user_data.pop("exchange", None) or s.cfg.exchange_id
    chat = update.effective_chat
    msg = await chat.send_message(f"⏳ Sprawdzam klucze na {exchange_name(ex)}…")
    try:
        bal = await s.engine.verify_keys(key, secret, password, ex)
    except Exception as e:  # noqa: BLE001
        log.warning("Klucze %s odrzucone: %s", ex, e)
        await msg.edit_text(f"❌ Klucze nie działają: {html.escape(friendly_error(e))}.\nSpróbuj ponownie: /polacz")
        return ConversationHandler.END
    u = s.db.get_user(update.effective_user.id)
    changed = user_exchange(s.cfg, u) != ex
    s.db.update_user(update.effective_user.id, api_key=s.vault.enc(key), api_secret=s.vault.enc(secret),
                     api_password=s.vault.enc(password), exchange=ex,
                     **({"active": 0} if changed and not u["paper"] else {}))
    quote_bal = bal.get(s.cfg.quote, 0)
    await msg.edit_text(
        f"✅ {exchange_name(ex)} podłączona. Saldo {s.cfg.quote}: <b>{fmt_money(quote_bal)}</b>\n\n"
        + ("Zmieniłeś giełdę, więc kopiowanie LIVE zostało wstrzymane – włącz je ponownie w menu."
           if changed and not u["paper"] else "Na LIVE przełączysz się w menu (jeśli jeszcze jesteś w demo)."),
        parse_mode=ParseMode.HTML)
    text, kb = menu_view(s, s.db.get_user(update.effective_user.id))
    await chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    return ConversationHandler.END


async def connect_cancel(update: Update, ctx):
    for k in ("api_key", "api_secret", "exchange"):
        ctx.user_data.pop(k, None)
    await update.message.reply_text("Anulowano.")
    return ConversationHandler.END


# ---------- admin ----------

async def cmd_admin(update: Update, ctx):
    if not is_admin(ctx, update):
        return
    s = S(ctx)
    st = s.db.stats()
    lines = [
        "🛠 <b>Panel admina</b>", "",
        f"Użytkownicy: {st['total']} · kopiuje: {st['active']} · w tym LIVE: {st['live']}",
        f"Subskrypcje: {st['subs']} aktywnych · przychód: {st['stars']} ⭐"
        + ("" if paywall_on(s.cfg) else " (płatności wyłączone)"),
        f"Kopiowanie: {'⏸ wstrzymane' if s.db.get_state('paused') == '1' else '🟢 działa'}",
        f"Traderzy: {len(s.db.leaders('active'))} aktywnych · {len(s.db.leaders('pending'))} czeka na zatwierdzenie",
    ]
    lines += ["", "/traderzy – lista i zatwierdzanie · /sygnal buy BTC/USDT 10 #id", "/pauza · /wznow · "
              "/ogloszenie tekst · /zwrot ID_użytkownika"]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_pause(update: Update, ctx):
    if is_admin(ctx, update):
        S(ctx).db.set_state("paused", "1")
        await update.message.reply_text("⏸ Kopiowanie wstrzymane dla wszystkich.")


async def cmd_resume(update: Update, ctx):
    if is_admin(ctx, update):
        S(ctx).db.set_state("paused", "0")
        await update.message.reply_text("▶️ Kopiowanie wznowione.")


SIDES = {"buy": "buy", "kup": "buy", "kupno": "buy", "sell": "sell", "sprzedaj": "sell", "sprzedaz": "sell"}


async def cmd_broadcast(update: Update, ctx):
    if not is_admin(ctx, update):
        return
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        return await update.message.reply_text("Użycie: /ogloszenie treść")
    sent = 0
    for uid in S(ctx).db.all_user_ids():
        try:
            await ctx.bot.send_message(uid, f"📢 {text}")
            sent += 1
        except Exception:  # noqa: BLE001
            pass
    await update.message.reply_text(f"Wysłano do {sent} osób.")


async def cmd_refund(update: Update, ctx):
    if not is_admin(ctx, update):
        return
    s = S(ctx)
    try:
        uid = int(ctx.args[0])
    except (IndexError, ValueError):
        return await update.message.reply_text("Użycie: /zwrot ID_użytkownika (zwraca ostatnią płatność)")
    p = s.db.last_payment(uid)
    if not p:
        return await update.message.reply_text("Brak płatności do zwrotu.")
    try:
        await ctx.bot.refund_star_payment(uid, p["charge_id"])
    except Exception as e:  # noqa: BLE001
        return await update.message.reply_text(f"❌ Zwrot nieudany: {e}")
    s.db.mark_refunded(p["id"], uid)
    s.db.update_user(uid, active=0)
    await update.message.reply_text(f"✅ Zwrócono {p['amount']} ⭐ użytkownikowi {uid}.")


def summary(res) -> str:
    return f"✅ {res.get('filled', 0)} · ⚠️ pominięte {res.get('skipped', 0)} · ❌ błędy {res.get('error', 0)}"


# ---------- rejestracja ----------

async def on_error(update: object, ctx):
    if isinstance(ctx.error, NetworkError):  # chwilowe zerwanie połączenia – bot sam ponowi
        log.warning("Problem z siecią: %s", ctx.error)
        return
    log.error("Błąd w obsłudze wiadomości", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await update.effective_chat.send_message("❌ Coś poszło nie tak. Spróbuj ponownie: /menu")
        except Exception:  # noqa: BLE001
            pass


def register(app: Application):
    private = filters.ChatType.PRIVATE
    text = filters.TEXT & ~filters.COMMAND & private
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(connect_start, pattern="^connect$"),
                      CommandHandler("polacz", connect_start, filters=private)],
        states={CHOOSE: [CallbackQueryHandler(got_exchange, pattern="^cex:")],
                KEY: [MessageHandler(text, got_key)],
                SECRET: [MessageHandler(text, got_secret)],
                PASSWORD: [MessageHandler(text, got_password)]},
        fallbacks=[CommandHandler("anuluj", connect_cancel)],
        allow_reentry=True,
        conversation_timeout=300,
    ))
    app.add_handler(CommandHandler(["start", "menu"], cmd_start, filters=private))
    app.add_handler(CommandHandler(["pomoc", "help"], cmd_help))
    app.add_handler(CommandHandler("mnoznik", cmd_multiplier, filters=private))
    app.add_handler(CommandHandler("limit", cmd_limit, filters=private))
    app.add_handler(CommandHandler("stoploss", cmd_stoploss, filters=private))
    app.add_handler(CommandHandler("wyniki", cmd_leader))
    app.add_handler(CommandHandler(["subskrypcja", "sub"], cmd_sub, filters=private))
    app.add_handler(CommandHandler("paysupport", cmd_paysupport))
    app.add_handler(CommandHandler("zwrot", cmd_refund))
    app.add_handler(PreCheckoutQueryHandler(on_precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_payment))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("pauza", cmd_pause))
    app.add_handler(CommandHandler("wznow", cmd_resume))
    app.add_handler(CommandHandler("ogloszenie", cmd_broadcast))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)


async def make_notify(app: Application):
    async def notify(tg_id: int, text: str):
        try:
            await app.bot.send_message(tg_id, text)
        except Forbidden:  # użytkownik zablokował bota – przestajemy kopiować
            app.bot_data["svc"].db.update_user(tg_id, active=0)
        except Exception:  # noqa: BLE001
            log.exception("Nie wysłano powiadomienia do %s", tg_id)
    return notify
