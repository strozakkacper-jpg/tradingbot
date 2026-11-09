"""Interfejs Telegram (bot prywatny): pulpit, portfel, ustawienia, giełda, dostęp i admin.

Traderzy (lista, profil, dodawanie, sygnały) są w traders.py.
"""
import asyncio
import html
import logging
from dataclasses import dataclass

from telegram import InlineKeyboardButton as B
from telegram import InlineKeyboardMarkup as KB
from telegram import Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler,
                          MessageHandler, filters)

from .config import KEY_HINTS, Config, exchange_name, friendly_error, needs_password, normalize_secret
from .db import DB
from .engine import SIDE_PL, CopyEngine, Market, fmt_amount, fmt_money, fmt_price, user_exchange
from .security import Vault

log = logging.getLogger(__name__)

DISCLAIMER = (
    "⚠️ <b>Zanim zaczniesz</b>\n\n"
    "• Handel kryptowalutami jest ryzykowny – możesz stracić całość środków.\n"
    "• Wyniki tradera z przeszłości <b>nie gwarantują</b> przyszłych zysków.\n"
    "• Startujesz w <b>trybie demo</b> (wirtualne {start} {quote}, prawdziwe ceny). "
    "Na prawdziwe środki przełączasz się sam, po podłączeniu giełdy.\n"
    "• Klucze API twórz <b>tylko z handlem spot</b> – nigdy z prawem do wypłat."
)
LOCKED = "🔒 <b>To prywatny bot.</b>\n\nPoprosiłem właściciela o dostęp dla Ciebie – dostaniesz wiadomość, gdy Cię wpuści."

KEY, SECRET, PASSWORD, CHOOSE = range(4)
MULTIPLIERS = (0.5, 1.0, 1.5, 2.0, 3.0)
LIMITS = (10, 25, 50, 100, 250, 1000)
STOP_LOSSES = (0, 10, 20, 30, 50)
PRICE_MOVES = (0, 1, 2, 5)
TOLERANCES = (1, 2, 5)
UI_TIMEOUT = 20  # s – dłużej nie czekamy na giełdę w odpowiedzi na kliknięcie
SIDES = {"buy": "buy", "kup": "buy", "kupno": "buy", "sell": "sell", "sprzedaj": "sell", "sprzedaz": "sell"}

HOME = [B("« Giełdy", callback_data="menu")]
BACK = HOME  # zgodność wsteczna


@dataclass
class Services:
    cfg: Config
    db: DB
    vault: Vault
    market: Market
    engine: CopyEngine
    watchers: object | None  # watchers.Watchers
    sol: object | None = None  # sol.engine.SolEngine – kopiowanie portfeli Solany
    bot: object | None = None  # telegram.Bot – do wiadomości HTML z poziomu aplikacji


def S(ctx: ContextTypes.DEFAULT_TYPE) -> Services:
    return ctx.application.bot_data["svc"]


def owner_ids(s: "Services") -> set[int]:
    """Właściciel 👑: admini z .env albo pierwsza osoba, która napisała /start. Tylko on nadaje rangi."""
    owner = s.db.get_state("owner")
    return set(s.cfg.admin_ids) | ({int(owner)} if owner else set())


def admin_ids(s: "Services") -> set[int]:
    """Właściciel + osoby z rangą administratora ⭐."""
    return owner_ids(s) | {r["tg_id"] for r in s.db._q(
        "SELECT tg_id FROM users WHERE role = 'admin' AND approved = 1")}


def is_admin(ctx, update: Update) -> bool:
    return update.effective_user.id in admin_ids(S(ctx))


def header(b_text: str) -> list:
    """Nieklikalny nagłówek sekcji między przyciskami."""
    return [B(f"— {b_text} —", callback_data="noop")]


def pm_txt(u) -> str:
    return f"maks. +{u['max_price_move']:g}%" if u["max_price_move"] else "wyłączona"


def sl_txt(u) -> str:
    return f"−{u['stop_loss']:g}%" if u["stop_loss"] else "wyłączony"


def summary(res) -> str:
    return (f"✅ skopiowano: {res.get('filled', 0)} · ⚠️ pominięto: {res.get('skipped', 0)} · "
            f"❌ błędy: {res.get('error', 0)} · 👁 powiadomienia: {res.get('alert', 0)}")


# ---------- widoki ----------

def menu_view(s: Services, u) -> tuple[str, KB]:
    q = s.cfg.quote
    ex = exchange_name(user_exchange(s.cfg, u))
    copied = s.db.copied_leaders(u["tg_id"])
    follows = s.db.follows_of(u["tg_id"])
    watching = sum(1 for c in follows.values() if not c)

    lines = ["🏦 <b>Pulpit giełd (CEX)</b>"]
    from .live import cached_prices_line
    prices = cached_prices_line(s, s.engine.m(u))
    if prices:
        lines.append(prices)
    lines.append("")
    state = "▶️ <b>włączone</b>" if u["active"] else "⏸ wyłączone"
    lines.append(f"<b>Kopiowanie:</b> {state}")
    mode = "🧪 demo" if u["paper"] else f"💰 <b>LIVE</b> · {ex}"
    lines.append(f"<b>Tryb:</b> {mode}")
    series = s.db.equity_series(u["tg_id"], bool(u["paper"]))
    if series:
        value = series[-1][1]
        base = s.cfg.paper_start if u["paper"] else (u["start_equity"] or value)
        lines.append(f"<b>Portfel:</b> ≈ {fmt_money(value)} {q} ({(value / base - 1) * 100:+.2f}%)")
    names = ", ".join(html.escape(c["name"]) for c in copied)
    lines.append(f"<b>Kopiujesz:</b> {names if names else '— nikogo'}")
    if u["copy_mode"] == "mirror":
        how = "🪞 lustro 1:1 + każda transakcja" if u["mirror_trades"] else "🪞 lustro 1:1 (tylko wyrównywanie)"
    else:
        how = "🔁 każda transakcja osobno"
    lines.append(f"<b>Sposób:</b> {how}")
    if watching:
        lines.append(f"<b>Obserwujesz:</b> {watching} (tylko powiadomienia)")
    sol_on = bool(getattr(s, "sol", None)) and (any(c["source"] == "sol" for c in copied)
                                                or s.sol.has_wallet(u["tg_id"]))
    if sol_on:
        sol_bal = s.sol.paper(u["tg_id"]).get("So11111111111111111111111111111111111111112", 0) / 1e9 \
            if not u["sol_live"] else None
        lines.append("<b>Solana:</b> " + ("💰 LIVE" if u["sol_live"] else f"🧪 demo · {sol_bal:.4f} SOL"))
    if not copied:
        lines += ["", "👉 Wejdź w <b>👥 Traderzy</b>, dodaj albo wybierz tradera do kopiowania."]
    if s.db.get_state("paused") == "1":
        lines += ["", "⏸ <i>Kopiowanie wstrzymane przez admina.</i>"]

    rows = ([[B("« 🟣 Pulpit Solana (kopiowanie portfeli)", callback_data="home")]] if getattr(s, "sol", None) else [])
    rows += [[B("⏸ Wyłącz kopiowanie" if u["active"] else "▶️ Włącz kopiowanie", callback_data="toggle")],
            [B("💼 Portfel", callback_data="status"), B("👥 Traderzy", callback_data="traders")],
            [B("🎮 Handel demo", callback_data="dt")],
            [B("📜 Historia", callback_data="history"), B("⚙️ Ustawienia", callback_data="settings")],
            [B(f"🏦 Giełda: {ex}" if u["api_key"] else "🏦 Podłącz giełdę", callback_data="exchange"),
             B("Przejdź na 💰 LIVE" if u["paper"] else "Przejdź na 🧪 demo", callback_data="mode")]]
    if s.cfg.webapp_url:
        rows.append([B("📈 Panel z wykresami", web_app=WebAppInfo(f"{s.cfg.webapp_url}/app"))])
    extra = [B("🔄 Odśwież", callback_data="menu")]
    if u["tg_id"] in admin_ids(s):
        extra.insert(0, B("🛠 Admin", callback_data="admin"))
    rows.append(extra)
    return "\n".join(lines), KB(rows)


def settings_view(s: Services, u) -> tuple[str, KB]:
    q = s.cfg.quote
    mirror = u["copy_mode"] == "mirror"
    text = (
        "⚙️ <b>Ustawienia</b>\n\n"
        "🔁 <b>Sposób kopiowania</b>\n"
        + ("<b>🪞 Lustro 1:1</b> – bot trzyma te same proporcje co trader (np. 50% BTC / 30% USDT).\n"
           + ("• <b>Każda transakcja</b> tradera kopiowana od razu, 1:1.\n" if u["mirror_trades"] else
              "• Transakcje nie są kopiowane osobno – tylko wyrównanie portfela.\n")
           + f"• Po każdym ruchu tradera i co {s.cfg.mirror_minutes} min bot porównuje portfele i poprawia różnice.\n"
           f"• Tolerancja <b>{u['mirror_tolerance']:g}%</b> – mniejszych różnic nie poprawia (oszczędza prowizje).\n\n"
           if mirror else
           "<b>Transakcje</b> – każda transakcja tradera kopiowana osobno, procentem salda.\n\n")
        + "💵 <b>Wielkość zleceń</b>"
        + (" <i>(w trybie lustra nie dotyczy – liczą się proporcje)</i>\n" if mirror else "\n") +
        f"Mnożnik <b>{u['multiplier']:g}x</b> – trader wyda 10% salda, Ty wydasz {min(10 * u['multiplier'], 100):g}%.\n"
        f"Limit <b>{u['max_trade']:g} {q}</b> na jeden zakup (sprzedaże bez limitu).\n\n"
        "🛡 <b>Ochrona</b>\n"
        f"Stop-loss <b>{sl_txt(u)}</b> – spadek portfela od włączenia kopiowania wyłącza kopiowanie "
        "i sprzedaje pozycje bota.\n"
        f"Ochrona ceny <b>{pm_txt(u)}</b> – nie kupuj, jeśli cena uciekła od ceny tradera.\n\n"
        "🔔 <b>Powiadomienia</b>\n"
        f"📌 Przypięta wiadomość u góry czatu (ceny, portfel, kopiowanie – co {s.cfg.live_seconds} s): "
        f"<b>{'włączona' if u['live_pin'] else 'wyłączona'}</b>\n"
        f"Raport dzienny o {s.cfg.daily_report_hour}:00: <b>{'włączony' if u['daily_report'] else 'wyłączony'}</b>\n\n"
        "<i>Własne wartości: /mnoznik 1.2 · /limit 75 · /stoploss 25</i>"
    )

    def mark(cur, v, label):
        return f"✓ {label}" if abs((cur or 0) - v) < 1e-9 else label

    mode_rows = [header("Sposób kopiowania"),
                 [B(("✓ " if not mirror else "") + "🔁 Transakcje", callback_data="cm:trades"),
                  B(("✓ " if mirror else "") + "🪞 Lustro 1:1", callback_data="cm:mirror")]]
    if mirror:
        mode_rows += [[B(f"Kopiuj też każdą transakcję: {'✓ wł' if u['mirror_trades'] else 'wył'}",
                         callback_data="mtr")],
                      header("Tolerancja lustra"),
                      [B(mark(u["mirror_tolerance"], t, f"{t:g}%"), callback_data=f"tol:{t}") for t in TOLERANCES]]
    kb = KB(mode_rows + [
        header("Mnożnik"),
        [B(mark(u["multiplier"], m, f"{m:g}x"), callback_data=f"mult:{m}") for m in MULTIPLIERS],
        header(f"Limit na zakup ({q})"),
        [B(mark(u["max_trade"], v, f"{v:g}"), callback_data=f"lim:{v}") for v in LIMITS],
        header("Stop-loss"),
        [B(mark(u["stop_loss"], v, f"−{v:g}%" if v else "wył"), callback_data=f"sl:{v}") for v in STOP_LOSSES],
        header("Ochrona ceny"),
        [B(mark(u["max_price_move"], v, f"+{v:g}%" if v else "wył"), callback_data=f"pm:{v}") for v in PRICE_MOVES],
        [B(f"📌 Na żywo u góry: {'✓ wł' if u['live_pin'] else 'wył'}", callback_data="pin"),
         B(f"🌙 Raport dzienny: {'✓ wł' if u['daily_report'] else 'wył'}", callback_data="daily")],
        [B("🔄 Reset konta demo", callback_data="paper_reset")],
        HOME,
    ])
    return text, kb


async def compare_view(s: Services, u) -> tuple[str, KB]:
    from . import mirror
    p = await mirror.plan(s, u)
    rows = []
    if p.orders:
        rows.append([B("🪞 Wyrównaj teraz", callback_data="mirror_now")])
    rows += [[B("🔄 Odśwież", callback_data="compare"), B("💼 Portfel", callback_data="status")], HOME]
    return mirror.plan_text(s, u, p), KB(rows)


async def status_text(s: Services, u) -> str:
    q = s.cfg.quote
    if u["paper"]:
        bal = s.db.paper_balances(u["tg_id"])
        title = "💼 <b>Portfel demo</b> 🧪"
    else:
        bal = await s.engine.live_balance(u)
        title = f"💼 <b>Portfel {exchange_name(user_exchange(s.cfg, u))}</b> 💰"
    lines = [title, ""]
    if not bal:
        lines.append("Brak środków.")
    for asset, amount in sorted(bal.items(), key=lambda kv: kv[0] != q):
        lines.append(f"• <b>{html.escape(asset)}</b>: {fmt_amount(amount)}")
    try:
        equity = await s.engine.m(u).equity(bal)
        lines += ["", f"Wartość: <b>{fmt_money(equity)} {q}</b>"]
        if u["paper"]:
            pnl = equity - s.cfg.paper_start
            lines.append(f"Wynik demo: {'📈' if pnl >= 0 else '📉'} {pnl:+.2f} {q} "
                         f"({pnl / s.cfg.paper_start * 100:+.2f}%)")
        if u["active"] and u["start_equity"]:
            ch = (equity / u["start_equity"] - 1) * 100
            lines.append(f"Od włączenia kopiowania: {ch:+.2f}% · stop-loss {sl_txt(u)}")
    except Exception:  # noqa: BLE001
        log.exception("Nie udało się wycenić portfela")
        lines += ["", "<i>(nie udało się pobrać cen)</i>"]
    return "\n".join(lines)


def history_text(s: Services, u) -> str:
    rows = s.db.history(u["tg_id"], 12)
    if not rows:
        return "📜 <b>Historia</b>\n\nJeszcze nic nie skopiowano."
    lines = ["📜 <b>Ostatnie transakcje</b>", ""]
    for r in rows:
        mode = "demo" if r["paper"] else "LIVE"
        if r["status"] == "filled":
            src = {"mirror": "🪞 lustro", "manual": "✋ ręcznie", "stop-loss": "🛑 stop-loss",
                   "zamknięcie": "🧹 zamknięcie"}.get(r["info"] or "", html.escape(r["info"] or ""))
            lines.append(f"{'🟢' if r['side'] == 'buy' else '🔴'} <b>{r['symbol']}</b> {fmt_amount(r['amount'])} "
                         f"@ {fmt_price(r['price'])} = {fmt_money(r['cost'])} {s.cfg.quote} "
                         f"<i>({mode}{' · ' + src if src else ''})</i>")
        else:
            icon = "⚠️" if r["status"] == "skipped" else "❌"
            lines.append(f"{icon} {r['symbol']}: <i>{html.escape((r['info'] or '')[:80])}</i>")
    return "\n".join(lines)


def exchange_view(s: Services, u) -> tuple[str, KB]:
    ex = exchange_name(user_exchange(s.cfg, u))
    if u["api_key"]:
        text = (f"🏦 <b>Giełda: {ex}</b> ✅\n\nKlucze API są podłączone i zaszyfrowane.\n"
                f"Tryb: {'🧪 demo' if u['paper'] else '💰 LIVE – bot handluje Twoimi środkami'}.\n\n"
                "Możesz podłączyć inną giełdę albo odłączyć klucze.")
        rows = [[B("🔁 Zmień giełdę / klucze", callback_data="connect")],
                [B("🗑 Odłącz klucze", callback_data="disconnect")], HOME]
    else:
        names = ", ".join(exchange_name(e) for e in s.cfg.exchanges)
        text = ("🏦 <b>Giełda</b>\n\nNie masz podłączonej giełdy – działasz w trybie demo.\n\n"
                f"Obsługiwane: {names}.\nTrader może być na innej giełdzie niż Ty – bot kopiuje te same pary.")
        rows = [[B("🔑 Podłącz giełdę", callback_data="connect")], HOME]
    return text, KB(rows)


def admin_view(s: Services) -> tuple[str, KB]:
    st = s.db.stats()
    paused = s.db.get_state("paused") == "1"
    owners = owner_ids(s)
    admins = admin_ids(s)
    lines = ["🛠 <b>Admin</b>", "",
             f"Kopiowanie: {'⏸ wstrzymane dla wszystkich' if paused else '🟢 działa'}",
             f"Traderzy: {len(s.db.leaders('active'))} · kopiuje teraz: {st['active']} os. (LIVE: {st['live']})",
             "", "<b>Osoby z dostępem</b>"]
    rows = []
    for u in s.db.approved_users():
        name = f"@{u['username']}" if u["username"] else str(u["tg_id"])
        role = " 👑 właściciel" if u["tg_id"] in owners else " ⭐ admin" if u["tg_id"] in admins else ""
        lines.append(f"• {html.escape(name)}{role} – {'▶️ kopiuje' if u['active'] else '⏸'} "
                     f"{'🧪' if u['paper'] else '💰'}")
        if u["tg_id"] not in owners:
            rows.append([B(f"🚫 Zabierz dostęp: {name[:24]}", callback_data=f"usr_del:{u['tg_id']}")])
    lines += ["", "<i>Nowa osoba pisze /start do bota – dostaniesz przycisk „Wpuść”.</i>",
              "<i>Ogłoszenie do wszystkich: /ogloszenie treść</i>"]
    rows.insert(0, [B("▶️ Wznów kopiowanie" if paused else "⏸ Wstrzymaj kopiowanie wszystkim",
                      callback_data="resume" if paused else "pause")])
    if getattr(s, "sol", None):
        rows.insert(0, [B("👥 Kto handluje – podgląd wszystkich", callback_data="adm:users")])
    rows.append(HOME)
    return "\n".join(lines), KB(rows)


# ---------- dostęp ----------

def get_user(ctx, update: Update):
    s = S(ctx)
    tu = update.effective_user
    u = s.db.ensure_user(tu.id, tu.username, s.cfg.paper_start, s.cfg.quote)
    if not u["approved"] and tu.id in admin_ids(s):
        s.db.update_user(tu.id, approved=1)
        u = s.db.get_user(tu.id)
    if tu.username and u["username"] != tu.username:
        s.db.update_user(tu.id, username=tu.username)
    return u


async def show(update: Update, text: str, kb: KB | None = None):
    """Edytuje wiadomość z przyciskami albo wysyła nową."""
    kw = {"parse_mode": ParseMode.HTML, "reply_markup": kb, "disable_web_page_preview": True}
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, **kw)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
    else:
        await update.effective_chat.send_message(text, **kw)


async def require_accepted(update: Update, ctx) -> object | None:
    """Wpuszcza tylko zatwierdzone osoby, które zaakceptowały ostrzeżenie o ryzyku."""
    s = S(ctx)
    u = get_user(ctx, update)
    if not u["approved"]:
        await show(update, LOCKED)
        if not u["requested"]:
            s.db.update_user(u["tg_id"], requested=1)
            tu = update.effective_user
            who = f"@{tu.username}" if tu.username else (tu.full_name if hasattr(tu, "full_name") else str(tu.id))
            for admin in admin_ids(s):
                try:
                    await ctx.bot.send_message(
                        admin, f"🔔 <b>{html.escape(str(who))}</b> (ID {tu.id}) prosi o dostęp do bota.",
                        parse_mode=ParseMode.HTML,
                        reply_markup=KB([[B("✅ Wpuść", callback_data=f"usr_ok:{tu.id}"),
                                          B("❌ Odrzuć", callback_data=f"usr_no:{tu.id}")]]))
                except Exception:  # noqa: BLE001
                    log.exception("Nie wysłano prośby o dostęp")
        return None
    if u["accepted"]:
        return u
    await show(update, DISCLAIMER.format(start=f"{s.cfg.paper_start:g}", quote=s.cfg.quote),
               KB([[B("✅ Rozumiem, zaczynam", callback_data="accept")]]))
    return None


# ---------- komendy ----------

async def cmd_start(update: Update, ctx):
    s = S(ctx)
    if not s.cfg.admin_ids and s.db.get_state("owner") is None:
        s.db.set_state("owner", str(update.effective_user.id))
        get_user(ctx, update)
        await update.effective_chat.send_message(
            "👑 Jesteś właścicielem tego bota. Tylko osoby, które wpuścisz, będą mogły z niego korzystać.",
            parse_mode=ParseMode.HTML)
    if u := await require_accepted(update, ctx):
        if getattr(s, "sol", None):  # pulpit główny: kopiowanie portfeli Solany (styl Axiom)
            from .sol.axiom import home_view
            try:
                await show(update, *await asyncio.wait_for(home_view(s, u), UI_TIMEOUT))
            except Exception:  # noqa: BLE001
                log.exception("Pulpit Solany")
                await show(update, *menu_view(s, u))
        else:
            await show(update, *menu_view(s, u))
        if u["live_pin"] and not u["pin_msg_id"]:
            from . import live
            asyncio.create_task(live.refresh(ctx.bot, s, u))


async def cmd_help(update: Update, ctx):
    await update.effective_chat.send_message(
        "ℹ️ <b>Jak to działa?</b>\n\n"
        "1. W <b>👥 Traderzy</b> dodajesz tradera: jego konto na giełdzie (klucz tylko do odczytu) albo własne "
        "sygnały.\n"
        "2. Przy każdym traderze wybierasz: <b>kopiuj</b> albo <b>tylko powiadomienia</b>.\n"
        "3. Włączasz kopiowanie. Trader kupi za X% swojego salda → bot w około sekundę kupi u Ciebie za X% "
        "Twojego (× mnożnik, maks. limit). Sprzeda część pozycji → sprzedasz ten sam procent.\n\n"
        "<b>Komendy</b>\n/menu – pulpit\n/traderzy – lista traderów\n/dodaj – dodaj tradera\n"
        "/sygnal buy BTC/USDT 10 – sygnał (dla traderów „sygnały”)\n"
        "/kup BTC 50 · /sprzedaj BTC 50% – handel demo\n/polacz – podłącz giełdę\n"
        "/mnoznik 1.5 · /limit 100 · /stoploss 25\n/anuluj – przerwij bieżącą czynność",
        parse_mode=ParseMode.HTML)


def _num(ctx, lo: float, hi: float) -> float | None:
    try:
        v = float(ctx.args[0].replace(",", ".").rstrip("%"))
        return v if lo <= v <= hi else None
    except (IndexError, ValueError):
        return None


async def cmd_multiplier(update: Update, ctx):
    if u := await require_accepted(update, ctx):
        if (v := _num(ctx, 0.01, 10)) is None:
            return await update.message.reply_text("Użycie: /mnoznik 1.5 (od 0.01 do 10)")
        S(ctx).db.update_user(u["tg_id"], multiplier=v)
        await update.message.reply_text(f"✅ Mnożnik: {v:g}x")


async def cmd_limit(update: Update, ctx):
    if u := await require_accepted(update, ctx):
        if (v := _num(ctx, 1, 10_000_000)) is None:
            return await update.message.reply_text(f"Użycie: /limit 100 (w {S(ctx).cfg.quote})")
        S(ctx).db.update_user(u["tg_id"], max_trade=v)
        await update.message.reply_text(f"✅ Limit na zakup: {v:g} {S(ctx).cfg.quote}")


async def cmd_stoploss(update: Update, ctx):
    if u := await require_accepted(update, ctx):
        if (v := _num(ctx, 0, 99)) is None:
            return await update.message.reply_text("Użycie: /stoploss 25 (procent, 0 = wyłączony)")
        S(ctx).db.update_user(u["tg_id"], stop_loss=v)
        await update.message.reply_text(f"✅ Stop-loss: {'−' + format(v, 'g') + '%' if v else 'wyłączony'}")


# ---------- przyciski ----------

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

    if data == "noop":
        return await ack()
    if data.startswith("usr_") or data in ("admin", "pause", "resume"):
        return await on_admin_button(update, ctx, ack)
    if data == "accept":
        u0 = get_user(ctx, update)
        if u0["approved"]:
            s.db.update_user(u0["tg_id"], accepted=1)
    if not (u := await require_accepted(update, ctx)):
        return await ack()
    uid = u["tg_id"]
    alert = None

    if data == "toggle":
        if u["active"]:
            from .sol import session
            s.db.update_user(uid, active=0)
            alert = "Kopiowanie wyłączone – raport sesji w czacie 📊"
            await session.end(s, s.db.get_user(uid), ctx.bot, "kopiowanie wyłączone")
        elif not s.db.copied_leaders(uid):
            await ack("Najpierw wybierz tradera do kopiowania.", show_alert=True)
            from .traders import show_traders
            return await show_traders(update, ctx)
        elif not u["paper"] and not u["api_key"]:
            alert = "Najpierw podłącz giełdę albo przejdź na demo."
        else:
            await ack("⏳ Włączam…")
            try:
                start = await asyncio.wait_for(s.engine.equity_of(u), UI_TIMEOUT)
            except Exception as e:  # noqa: BLE001
                return await show(update, "❌ Nie udało się pobrać salda z giełdy"
                                          f"{': ' + html.escape(str(e)[:150]) if str(e) else ' (przekroczony czas)'}."
                                          "\nSpróbuj ponownie za chwilę.", KB([HOME]))
            s.db.update_user(uid, active=1, start_equity=start)
            s.db.add_equity(uid, start, bool(u["paper"]))
            from .sol import session
            await session.start(s, s.db.get_user(uid), cex0=start)
            alert = "Kopiowanie włączone ▶️"
            if u["copy_mode"] == "mirror":
                from . import mirror
                asyncio.create_task(mirror.sync(s, s.db.get_user(uid), "start lustra"))
                alert += " – wyrównuję portfel do tradera 🪞"
    elif data == "mode":
        if u["paper"]:
            if not u["api_key"]:
                await ack("Najpierw podłącz giełdę 🏦", show_alert=True)
                return await show(update, *exchange_view(s, u))
            await ack()
            return await show(update,
                              "💰 <b>Przejść na prawdziwe środki?</b>\n\nBot będzie składał <b>prawdziwe zlecenia</b> na "
                              f"Twoim koncie {exchange_name(user_exchange(s.cfg, u))}.\n"
                              f"Limit: {u['max_trade']:g} {s.cfg.quote} na zakup · stop-loss: {sl_txt(u)}.\n\n"
                              "Kopiowanie wyłączy się – włączysz je ponownie, gdy będziesz gotowy.",
                              KB([[B("✅ Tak, przejdź na LIVE", callback_data="mode_live")], HOME]))
        s.db.update_user(uid, paper=1, active=0)
        alert = "Tryb demo 🧪 – włącz kopiowanie ponownie"
    elif data == "mode_live":
        if u["api_key"]:
            s.db.update_user(uid, paper=0, active=0)
            alert = "Tryb LIVE 💰 – włącz kopiowanie, gdy będziesz gotowy"
    elif data.split(":")[0] in ("mult", "lim", "sl", "pm"):
        key, val = data.split(":")
        field = {"mult": "multiplier", "lim": "max_trade", "sl": "stop_loss", "pm": "max_price_move"}[key]
        s.db.update_user(uid, **{field: float(val)})
        await ack("Zapisano")
        return await show(update, *settings_view(s, s.db.get_user(uid)))
    elif data.startswith("cm:"):
        mode = data[3:]
        s.db.update_user(uid, copy_mode=mode)
        await ack("Lustro 1:1 🪞" if mode == "mirror" else "Kopiowanie transakcji 🔁")
        if mode == "mirror" and u["active"]:
            from . import mirror
            asyncio.create_task(mirror.sync(s, s.db.get_user(uid), "włączono lustro"))
        return await show(update, *settings_view(s, s.db.get_user(uid)))
    elif data == "mtr":
        s.db.update_user(uid, mirror_trades=0 if u["mirror_trades"] else 1)
        await ack("Lustro + transakcje ✅" if not u["mirror_trades"] else "Lustro: tylko wyrównywanie")
        return await show(update, *settings_view(s, s.db.get_user(uid)))
    elif data.startswith("tol:"):
        s.db.update_user(uid, mirror_tolerance=float(data[4:]))
        await ack("Zapisano")
        return await show(update, *settings_view(s, s.db.get_user(uid)))
    elif data == "compare":
        await ack("Porównuję…")
        try:
            return await show(update, *await asyncio.wait_for(compare_view(s, u), UI_TIMEOUT))
        except Exception as e:  # noqa: BLE001
            return await show(update, f"❌ Nie udało się porównać: {html.escape(friendly_error(e) if str(e) else 'przekroczony czas')}",
                              KB([HOME]))
    elif data == "mirror_now":
        await ack("Wyrównuję…")
        from . import mirror
        res = await mirror.sync(s, u, "ręcznie", notify=False)
        text, kb = await compare_view(s, s.db.get_user(uid))
        return await show(update, f"🪞 Wyrównano: zleceń {res.get('filled', 0)}, pominięte {res.get('skipped', 0)}, "
                                  f"błędy {res.get('error', 0)}.\n\n" + text, kb)
    elif data == "pin":
        from . import live
        if u["live_pin"]:
            await live.disable(ctx.bot, s, u)
            await ack("Przypięta wiadomość wyłączona")
        else:
            s.db.update_user(uid, live_pin=1)
            await ack("📌 Przypinam informacje na żywo u góry czatu")
            asyncio.create_task(live.refresh(ctx.bot, s, s.db.get_user(uid)))
        return await show(update, *settings_view(s, s.db.get_user(uid)))
    elif data == "daily":
        s.db.update_user(uid, daily_report=0 if u["daily_report"] else 1)
        await ack("Zapisano")
        return await show(update, *settings_view(s, s.db.get_user(uid)))
    elif data == "paper_reset":
        s.db.paper_reset(uid, s.cfg.paper_start, s.cfg.quote)
        await ack(f"Konto demo: {s.cfg.paper_start:g} {s.cfg.quote}", show_alert=True)
        return await show(update, *settings_view(s, u))
    elif data == "settings":
        await ack()
        return await show(update, *settings_view(s, u))
    elif data == "exchange":
        await ack()
        return await show(update, *exchange_view(s, u))
    elif data == "disconnect":
        s.db.update_user(uid, api_key=None, api_secret=None, api_password=None, paper=1, active=0)
        await ack("Klucze odłączone – jesteś w trybie demo", show_alert=True)
        return await show(update, *exchange_view(s, s.db.get_user(uid)))
    elif data == "status":
        await ack("Pobieram saldo…")
        try:
            text = await asyncio.wait_for(status_text(s, u), UI_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            text = f"❌ Nie udało się pobrać salda: {html.escape(friendly_error(e) if str(e) else 'przekroczony czas')}"
        return await show(update, text, KB([[B("🔍 Porównaj z traderem", callback_data="compare")],
                                            [B("🔄 Odśwież", callback_data="status"),
                                             B("🧹 Sprzedaj wszystko", callback_data="closeall")], HOME]))
    elif data == "closeall":
        await ack()
        return await show(update, "🧹 <b>Sprzedać wszystkie pozycje otwarte przez bota?</b>\n\nMonet, których bot "
                                  "nie kupował, nie ruszam. Kopiowanie zostanie wyłączone.",
                          KB([[B("✅ Tak, sprzedaj", callback_data="closeall_ok")], HOME]))
    elif data == "closeall_ok":
        await ack("Sprzedaję…")
        s.db.update_user(uid, active=0)
        res = await s.engine.close_positions(u, title="🧹 Zamknięcie pozycji: sprzedano")
        return await show(update, f"🧹 Gotowe. Sprzedane pozycje: {res.get('filled', 0)}, błędy: "
                                  f"{res.get('error', 0)}. Kopiowanie wyłączone.", KB([HOME]))
    elif data == "history":
        await ack()
        return await show(update, history_text(s, u), KB([HOME]))

    notice = alert if acked else None  # gdy już odpowiedzieliśmy, komunikat trafia do treści pulpitu
    await ack(alert)
    text, kb = menu_view(s, s.db.get_user(uid))
    await show(update, (f"ℹ️ {html.escape(notice)}\n\n" if notice else "") + text, kb)


async def on_admin_button(update: Update, ctx, ack):
    s = S(ctx)
    if not is_admin(ctx, update):
        return await ack("Tylko dla właściciela", show_alert=True)
    data = q_data = update.callback_query.data
    if q_data.startswith("usr_"):
        action, tid = q_data.split(":")
        tid = int(tid)
        target = s.db.get_user(tid)
        if not target:
            return await ack("Nie ma takiej osoby")
        name = f"@{target['username']}" if target["username"] else str(tid)
        if action == "usr_ok":
            s.db.update_user(tid, approved=1)
            await ack("Wpuszczono ✅")
            try:
                await ctx.bot.send_message(tid, "✅ Masz dostęp do bota! Napisz /start, żeby zacząć.")
            except Exception:  # noqa: BLE001
                pass
            return await update.callback_query.edit_message_text(f"✅ {name} ma dostęp.")
        if action == "usr_no":
            await ack("Odrzucono")
            return await update.callback_query.edit_message_text(f"❌ Odrzucono prośbę {name}.")
        if action == "usr_del":
            s.db.update_user(tid, approved=0, active=0, requested=0)
            await ack("Dostęp zabrany")
            return await show(update, *admin_view(s))
    if data in ("pause", "resume"):
        s.db.set_state("paused", "1" if data == "pause" else "0")
        await ack("Wstrzymano" if data == "pause" else "Wznowiono")
    else:
        await ack()
    return await show(update, *admin_view(s))


# ---------- podłączanie giełdy ----------

def exchange_kb(cfg: Config, prefix: str) -> KB:
    names = [B(exchange_name(e), callback_data=f"{prefix}{e}") for e in cfg.exchanges]
    return KB([names[i:i + 3] for i in range(0, len(names), 3)])


async def connect_start(update: Update, ctx):
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except BadRequest:
            pass
    if not await require_accepted(update, ctx):
        return ConversationHandler.END
    await update.effective_chat.send_message(
        "🔑 <b>Podłączenie giełdy</b>\n\nWybierz giełdę, na której masz konto:\n\n/anuluj – przerwij",
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
        "2. Zaznacz <b>tylko</b> odczyt i handel spot. <b>Bez wypłat!</b>\n"
        f"3. Nie ustawiaj ograniczenia IP (albo dodaj IP serwera bota).{extra}\n\n"
        "Wyślij teraz <b>API Key</b> (wiadomość od razu usunę).\n/anuluj – przerwij",
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
        bal = await asyncio.wait_for(s.engine.verify_keys(key, secret, password, ex), 30)
    except Exception as e:  # noqa: BLE001
        log.warning("Klucze %s odrzucone: %s", ex, e)
        await msg.edit_text(f"❌ Klucze nie działają: {html.escape(friendly_error(e))}.\nSpróbuj ponownie: /polacz")
        return ConversationHandler.END
    u = s.db.get_user(update.effective_user.id)
    changed = user_exchange(s.cfg, u) != ex
    s.db.update_user(update.effective_user.id, api_key=s.vault.enc(key), api_secret=s.vault.enc(secret),
                     api_password=s.vault.enc(password), exchange=ex,
                     **({"active": 0} if changed and not u["paper"] else {}))
    stables = {k: v for k, v in bal.items() if k in ("USDT", "USDC", "USD")}
    bal_txt = ", ".join(f"{fmt_money(v)} {k}" for k, v in stables.items()) or f"0 {s.cfg.quote}"
    await msg.edit_text(
        f"✅ <b>{exchange_name(ex)} podłączona.</b> Saldo: {bal_txt}\n\n"
        + ("Zmieniłeś giełdę, więc kopiowanie LIVE zostało wstrzymane – włącz je ponownie na pulpicie."
           if changed and not u["paper"] else "Na prawdziwe środki przejdziesz przyciskiem „Przejdź na 💰 LIVE”."),
        parse_mode=ParseMode.HTML)
    text, kb = menu_view(s, s.db.get_user(update.effective_user.id))
    await chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    return ConversationHandler.END


async def connect_cancel(update: Update, ctx):
    for k in ("api_key", "api_secret", "exchange"):
        ctx.user_data.pop(k, None)
    await update.message.reply_text("Anulowano. /menu – pulpit")
    return ConversationHandler.END


# ---------- admin: komendy ----------

async def cmd_admin(update: Update, ctx):
    if is_admin(ctx, update):
        await show(update, *admin_view(S(ctx)))


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


# ---------- rejestracja ----------

async def on_error(update: object, ctx):
    if isinstance(ctx.error, Conflict):
        log.warning("Ten sam BOT_TOKEN działa jeszcze w innym miejscu (np. drugi serwer albo komputer) – "
                    "wyłącz drugą kopię bota, inaczej wiadomości będą ginąć")
        return
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
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("ogloszenie", cmd_broadcast))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)


async def make_notify(app: Application):
    async def notify(tg_id: int, text: str, html: bool = False, buttons: list | None = None):
        """buttons: wiersze [(etykieta, callback_data albo https://…)] pod wiadomością."""
        kb = KB([[B(lbl, url=d) if d.startswith("http") else B(lbl, callback_data=d) for lbl, d in row]
                 for row in buttons]) if buttons else None
        # ponawiamy przy chwilowych problemach z siecią i przy limicie Telegrama (RetryAfter)
        for attempt in range(4):
            try:
                await app.bot.send_message(tg_id, text, parse_mode=ParseMode.HTML if html else None,
                                           reply_markup=kb, disable_web_page_preview=True)
                return
            except Forbidden:  # użytkownik zablokował bota – przestajemy kopiować
                app.bot_data["svc"].db.update_user(tg_id, active=0)
                return
            except RetryAfter as e:
                await asyncio.sleep(min(float(e.retry_after), 30) + 0.5)
            except (TimedOut, NetworkError):
                await asyncio.sleep(1.5 * (attempt + 1))
            except Exception:  # noqa: BLE001
                log.exception("Nie wysłano powiadomienia do %s", tg_id)
                return
        log.warning("Nie wysłano powiadomienia do %s po 4 próbach (sieć/limit Telegrama)", tg_id)
    return notify


__all__ = ["Services", "S", "SIDES", "SIDE_PL", "admin_ids", "is_admin", "require_accepted", "show", "summary",
           "exchange_kb", "register", "make_notify", "HOME", "BACK", "header"]
