"""Traderzy: ranking, profil, wybór kogo kopiować, zgłoszenia „Zostań traderem”, panel tradera."""
import html
import logging

from telegram import InlineKeyboardButton as B
from telegram import InlineKeyboardMarkup as KB
from telegram import Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ConversationHandler, MessageHandler,
                          filters)

from .bot import BACK, S, SIDES, admin_ids, exchange_kb, is_admin, require_accepted, show, summary
from .config import KEY_HINTS, exchange_name, friendly_error, needs_password, normalize_secret
from .db import now_ms
from .engine import SIDE_PL, make_exchange, user_exchange
from .reports import fmt_pct, leader_report, leader_text, ranking
from .sizing import LeaderOrder

log = logging.getLogger(__name__)
A_NAME, A_BIO, A_SOURCE, A_KEY, A_SECRET, A_PASS, A_EXCH = range(10, 17)
MEDALS = ["🥇", "🥈", "🥉"]


# ---------- ranking i profil ----------

async def show_traders(update: Update, ctx):
    s = S(ctx)
    u = s.db.get_user(update.effective_user.id)
    rows = await ranking(s)
    lines = ["🏆 <b>Ranking traderów</b>", "<i>Wyniki z portfeli wzorcowych: każde zlecenie skopiowane 1:1.</i>", ""]
    kb = []
    if not rows:
        lines.append("Nie ma jeszcze aktywnych traderów.")
    for i, (lead, rep, (_, copying)) in enumerate(rows):
        medal = MEDALS[i] if i < 3 else f"{i + 1}."
        mine = " ✔️" if u and u["leader_id"] == lead["id"] else ""
        wr = f" · trafność {rep['win_rate'] * 100:.0f}%" if rep["win_rate"] is not None else ""
        lines.append(f"{medal} <b>{html.escape(lead['name'])}</b>{mine}\n"
                     f"     30 dni {fmt_pct(rep['d30'])} · od startu {fmt_pct(rep['total'])} · "
                     f"spadek {fmt_pct(-rep['max_dd'])}{wr} · 👥 {copying}")
        kb.append([B(f"{medal} {lead['name'][:24]}{mine}", callback_data=f"tr:{lead['id']}")])
    kb.append([B("🎓 Zostań traderem", callback_data="apply")])
    kb.append(BACK)
    await show(update, "\n".join(lines), KB(kb))


async def show_profile(update: Update, ctx, leader_id: int | None):
    s = S(ctx)
    lead = s.db.get_leader(leader_id)
    if not lead or lead["status"] != "active":
        return await show_traders(update, ctx)
    u = s.db.get_user(update.effective_user.id)
    rep = await leader_report(s, lead["id"])
    counts = s.db.follower_counts().get(lead["id"], (0, 0))
    text = leader_text(rep, s.cfg.quote, lead, counts)
    if lead["source"] == "api":
        text = text.replace("📡 konto giełdowe", f"📡 konto {exchange_name(user_exchange(s.cfg, lead))}", 1)
    rows = []
    if u and u["leader_id"] == lead["id"]:
        rows.append([B("✔️ Kopiujesz tego tradera", callback_data="noop")])
    else:
        rows.append([B("✅ Kopiuj tego tradera", callback_data=f"follow:{lead['id']}")])
    if s.cfg.webapp_url:
        rows.append([B("📈 Wykres w panelu", web_app=WebAppInfo(f"{s.cfg.webapp_url}/app"))])
    rows.append([B("« Ranking", callback_data="traders"), *BACK])
    await show(update, text, KB(rows))


async def on_trader_button(update: Update, ctx):
    q = update.callback_query
    if not (u := await require_accepted(update, ctx)):
        return await q.answer()
    s = S(ctx)
    data = q.data
    if data == "traders":
        await q.answer()
        return await show_traders(update, ctx)
    if data == "noop":
        return await q.answer("Już kopiujesz tego tradera")
    if data.startswith("tr:"):
        await q.answer()
        return await show_profile(update, ctx, int(data[3:]))
    if data.startswith("follow:"):
        lid = int(data[7:])
        lead = s.db.get_leader(lid)
        if not lead or lead["status"] != "active":
            return await q.answer("Ten trader jest niedostępny", show_alert=True)
        had = u["leader_id"] and u["leader_id"] != lid
        s.db.update_user(u["tg_id"], leader_id=lid)
        msg = f"Kopiujesz: {lead['name']}"
        if not u["active"]:
            msg += ". Włącz kopiowanie w menu ▶️"
        if had:
            msg += ". Pozycje od poprzedniego tradera zostają – zamkniesz je w 📊 Portfel → 🧹"
        await q.answer(msg[:200], show_alert=True)
        return await show_profile(update, ctx, lid)


# ---------- zgłoszenie: zostań traderem ----------

async def apply_start(update: Update, ctx):
    if update.callback_query:
        await update.callback_query.answer()
    if not await require_accepted(update, ctx):
        return ConversationHandler.END
    await update.effective_chat.send_message(
        "🎓 <b>Zostań traderem</b>\n\nInni będą mogli kopiować Twoje transakcje. Twoje wyniki będą liczone "
        "automatycznie i publicznie w rankingu – nie da się ich podkręcić.\n\n"
        "Jak ma się nazywać Twój profil? (np. <i>Kacper Swing</i>)\n/anuluj – przerwij", parse_mode=ParseMode.HTML)
    return A_NAME


async def apply_name(update: Update, ctx):
    name = update.message.text.strip()[:40]
    if len(name) < 3:
        await update.message.reply_text("Nazwa musi mieć min. 3 znaki.")
        return A_NAME
    ctx.user_data["apply"] = {"name": name}
    await update.message.reply_text("Krótki opis strategii (1–2 zdania), np. „Swing trading BTC/ETH, trzymam "
                                    "pozycje kilka dni”. Albo /pomin.")
    return A_BIO


async def apply_bio(update: Update, ctx):
    text = update.message.text.strip()
    ctx.user_data["apply"]["bio"] = None if text.lower() == "/pomin" else text[:200]
    s = S(ctx)
    await update.message.reply_text(
        "Skąd bot ma brać Twoje transakcje?",
        reply_markup=KB([[B("📡 Moje konto na giełdzie (klucz do odczytu)", callback_data="src:api")],
                         [B("✍️ Będę wysyłać sygnały w bocie", callback_data="src:manual")]]))
    return A_SOURCE


async def apply_source(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    s = S(ctx)
    if q.data == "src:manual":
        return await _apply_finish(update, ctx, "manual")
    await q.edit_message_text("Na której giełdzie handlujesz?", reply_markup=exchange_kb(s.cfg, "aex:"))
    return A_EXCH


async def apply_exchange(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    ex = q.data[4:]
    ctx.user_data["apply"]["exchange"] = ex
    await q.edit_message_text(
        f"🔑 Utwórz na {exchange_name(ex)} klucz API <b>tylko do odczytu</b> (bez handlu i bez wypłat) "
        "i wyślij <b>API Key</b>. Wiadomość zostanie od razu usunięta."
        + (f"\n\n{KEY_HINTS[ex]}" if ex in KEY_HINTS else ""), parse_mode=ParseMode.HTML)
    return A_KEY


async def _wipe(update: Update):
    try:
        await update.message.delete()
    except Exception:  # noqa: BLE001
        pass


async def apply_key(update: Update, ctx):
    ctx.user_data["apply"]["api_key"] = update.message.text.strip()
    await _wipe(update)
    await update.effective_chat.send_message("Teraz <b>API Secret</b>.", parse_mode=ParseMode.HTML)
    return A_SECRET


async def apply_secret(update: Update, ctx):
    ctx.user_data["apply"]["api_secret"] = normalize_secret(update.message.text)
    await _wipe(update)
    if needs_password(ctx.user_data["apply"].get("exchange", "")):
        await update.effective_chat.send_message("Teraz <b>passphrase</b>.", parse_mode=ParseMode.HTML)
        return A_PASS
    return await _apply_finish(update, ctx, "api")


async def apply_pass(update: Update, ctx):
    ctx.user_data["apply"]["api_password"] = update.message.text.strip()
    await _wipe(update)
    return await _apply_finish(update, ctx, "api")


async def _apply_finish(update: Update, ctx, source: str):
    s = S(ctx)
    a = ctx.user_data.pop("apply", {})
    chat = update.effective_chat
    keys = {}
    if source == "api":
        msg = await chat.send_message("⏳ Sprawdzam klucz…")
        ex = make_exchange(s.cfg, a.get("api_key"), a.get("api_secret"), a.get("api_password"),
                           exchange_id=a.get("exchange"))
        try:
            await ex.fetch_balance()
        except Exception as e:  # noqa: BLE001
            await msg.edit_text(f"❌ Klucz nie działa: {html.escape(friendly_error(e))}.\n"
                                "Spróbuj ponownie: /zostan_traderem")
            return ConversationHandler.END
        finally:
            await ex.close()
        keys = {k: s.vault.enc(a.get(k)) for k in ("api_key", "api_secret", "api_password")}
        keys["exchange"] = a.get("exchange")
    lid = s.db.create_leader(a["name"], source, update.effective_user.id, "pending", a.get("bio"), **keys)
    await chat.send_message("✅ Zgłoszenie wysłane. Dostaniesz wiadomość, gdy admin je zatwierdzi.\n"
                            "Twój panel: /trader")
    src = f"konto {exchange_name(a.get('exchange') or '')} (klucz do odczytu)" if source == "api" else "sygnały ręczne"
    for admin in admin_ids(s):
        try:
            await ctx.bot.send_message(
                admin, f"🎓 Nowe zgłoszenie tradera #{lid}\n<b>{html.escape(a['name'])}</b>\n"
                       f"{html.escape(a.get('bio') or '—')}\nŹródło: {src}\nOd: {update.effective_user.id}",
                parse_mode=ParseMode.HTML,
                reply_markup=KB([[B("✅ Zatwierdź", callback_data=f"adm_ok:{lid}"),
                                  B("❌ Odrzuć", callback_data=f"adm_hide:{lid}")]]))
        except Exception:  # noqa: BLE001
            log.exception("Nie powiadomiono admina")
    return ConversationHandler.END


async def apply_cancel(update: Update, ctx):
    ctx.user_data.pop("apply", None)
    await update.message.reply_text("Anulowano.")
    return ConversationHandler.END


# ---------- admin ----------

async def on_admin_button(update: Update, ctx):
    q = update.callback_query
    if not is_admin(ctx, update):
        return await q.answer("Tylko dla admina", show_alert=True)
    s = S(ctx)
    action, lid = q.data.split(":")
    lead = s.db.get_leader(int(lid))
    if not lead:
        return await q.answer("Nie ma takiego tradera")
    status = "active" if action == "adm_ok" else "hidden"
    s.db.update_leader(lead["id"], status=status)
    if status == "active":
        s.db.ensure_reference(lead["id"], s.cfg.paper_start, s.cfg.quote)
    if s.watchers:
        s.watchers.sync()
    await q.answer("Zapisano")
    await q.edit_message_text(f"Trader #{lead['id']} {lead['name']}: "
                              f"{'✅ aktywny' if status == 'active' else '🚫 ukryty'}")
    if lead["owner_tg_id"]:
        try:
            await ctx.bot.send_message(lead["owner_tg_id"], (
                f"🎉 Twój profil tradera „{lead['name']}” jest aktywny i widoczny w rankingu! Panel: /trader"
                if status == "active" else f"Profil tradera „{lead['name']}” został ukryty."))
        except Exception:  # noqa: BLE001
            pass
    if status == "hidden":  # obserwujący ukrytego tradera przestają kopiować
        for u in s.db.active_followers(lead["id"]):
            s.db.update_user(u["tg_id"], active=0)
            await s.engine.notify(u["tg_id"], f"⏸ Trader „{lead['name']}” został wycofany. Kopiowanie wstrzymane – "
                                              "wybierz innego w 🏆 Traderzy.")


async def cmd_traders_admin(update: Update, ctx):
    if not is_admin(ctx, update):  # zwykły użytkownik dostaje ranking
        if await require_accepted(update, ctx):
            await show_traders(update, ctx)
        return
    s = S(ctx)
    counts = s.db.follower_counts()
    rows, lines = [], ["👥 <b>Traderzy</b>", ""]
    icon = {"active": "🟢", "pending": "⏳", "hidden": "🚫"}
    for lead in s.db.leaders(None):
        w = s.watchers.get(lead["id"]) if s.watchers else None
        conn = f" · {w.mode}" if w else ""
        lines.append(f"{icon.get(lead['status'], '•')} #{lead['id']} <b>{html.escape(lead['name'])}</b> "
                     f"({exchange_name(user_exchange(s.cfg, lead)) if lead['source'] == 'api' else 'sygnały'}{conn}) "
                     f"· kopiuje {counts.get(lead['id'], (0, 0))[1]}")
        rows.append([B(f"✅ #{lead['id']}", callback_data=f"adm_ok:{lead['id']}"),
                     B(f"🚫 #{lead['id']}", callback_data=f"adm_hide:{lead['id']}")])
    if len(lines) == 2:
        lines.append("Brak traderów.")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=KB(rows) if rows else None)


# ---------- panel tradera i sygnały ----------

async def cmd_trader_panel(update: Update, ctx):
    s = S(ctx)
    own = s.db.leaders_owned(update.effective_user.id)
    if not own:
        return await update.message.reply_text("Nie masz profilu tradera. Załóż: /zostan_traderem")
    counts = s.db.follower_counts()
    for lead in own:
        st = {"active": "🟢 aktywny", "pending": "⏳ czeka na zatwierdzenie", "hidden": "🚫 ukryty"}[lead["status"]]
        lines = [f"🎓 <b>{html.escape(lead['name'])}</b> (#{lead['id']}) – {st}"]
        if lead["status"] == "active":
            rep = await leader_report(s, lead["id"])
            n, a = counts.get(lead["id"], (0, 0))
            lines += [f"Kopiuje Cię: <b>{a}</b> os. (wybrało {n})",
                      f"Wynik od startu: {fmt_pct(rep['total'])} · 30 dni: {fmt_pct(rep['d30'])}"]
            if lead["source"] == "manual":
                lines += ["", "Wysyłanie sygnałów:", "<code>/sygnal buy BTC/USDT 10</code> – kup za 10% salda",
                          "<code>/sygnal sell BTC/USDT 50</code> – sprzedaj połowę pozycji"]
            else:
                w = s.watchers.get(lead["id"]) if s.watchers else None
                lines.append(f"Śledzenie konta: {'WebSocket ⚡' if w and w.mode == 'ws' else 'odpytywanie'}"
                             if w else "Śledzenie konta: uruchamia się…")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


def _signal_leader(s, tg_id: int, explicit: int | None, admin: bool):
    if explicit is not None:
        lead = s.db.get_leader(explicit)
        if lead and lead["status"] == "active" and (admin or lead["owner_tg_id"] == tg_id):
            return lead
        return None
    own = [lead for lead in s.db.leaders_owned(tg_id) if lead["status"] == "active" and lead["source"] == "manual"]
    if len(own) == 1:
        return own[0]
    if admin:
        manual = [lead for lead in s.db.leaders("active") if lead["source"] == "manual"]
        if len(manual) == 1:
            return manual[0]
    return None


async def cmd_signal(update: Update, ctx):
    """/sygnal buy BTC/USDT 10 [#id] – trader z sygnałami ręcznymi (albo admin) wysyła zlecenie."""
    s = S(ctx)
    args = list(ctx.args)
    explicit = None
    if args and args[-1].startswith("#"):
        try:
            explicit = int(args.pop()[1:])
        except ValueError:
            pass
    admin = is_admin(ctx, update)
    lead = _signal_leader(s, update.effective_user.id, explicit, admin)
    if not lead:
        if not admin and not s.db.leaders_owned(update.effective_user.id):
            return
        return await update.message.reply_text("Nie wiem, dla którego tradera. Dopisz numer, np. "
                                               "/sygnal buy BTC/USDT 10 #3 (lista: /traderzy)")
    try:
        side = SIDES[args[0].lower()]
        symbol = args[1].upper()
        pct = float(args[2].replace(",", "."))
        assert 0 < pct <= 100
    except (IndexError, KeyError, ValueError, AssertionError):
        return await update.message.reply_text(
            "Użycie: /sygnal buy|sell PARA PROCENT\nnp. /sygnal buy BTC/USDT 10 – kupno za 10% salda\n"
            "/sygnal sell BTC/USDT 100 – sprzedaż całej pozycji")
    market = s.engine.m(lead)
    resolved = await market.resolve(symbol) if hasattr(market, "resolve") else (symbol if await market.has(symbol) else None)
    if not resolved:
        return await update.message.reply_text(f"Para {symbol} nie istnieje na {exchange_name(market.exchange_id)}.")
    symbol = resolved
    price = await market.price(symbol)
    order = LeaderOrder(id=f"manual-{now_ms()}", symbol=symbol, side=side, amount=0, cost=0, price=price, ts=now_ms())
    res = await s.engine.copy(order, pct / 100, lead["id"])
    await update.message.reply_text(f"📡 {lead['name']}: {SIDE_PL[side]} {symbol} {pct:g}% wysłany.\n" + summary(res))


def register(app: Application):
    """Rejestrować PRZED bot.register – handlery bota łapią wszystkie pozostałe przyciski."""
    private = filters.ChatType.PRIVATE
    text = filters.TEXT & ~filters.COMMAND & private
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(apply_start, pattern="^apply$"),
                      CommandHandler("zostan_traderem", apply_start, filters=private)],
        states={A_NAME: [MessageHandler(text, apply_name)],
                A_BIO: [MessageHandler(text, apply_bio), CommandHandler("pomin", apply_bio)],
                A_SOURCE: [CallbackQueryHandler(apply_source, pattern="^src:")],
                A_EXCH: [CallbackQueryHandler(apply_exchange, pattern="^aex:")],
                A_KEY: [MessageHandler(text, apply_key)],
                A_SECRET: [MessageHandler(text, apply_secret)],
                A_PASS: [MessageHandler(text, apply_pass)]},
        fallbacks=[CommandHandler("anuluj", apply_cancel)],
        allow_reentry=True, conversation_timeout=600,
    ))
    app.add_handler(CallbackQueryHandler(on_trader_button, pattern=r"^(traders|noop|tr:\d+|follow:\d+)$"))
    app.add_handler(CallbackQueryHandler(on_admin_button, pattern=r"^adm_(ok|hide):\d+$"))
    app.add_handler(CommandHandler(["traderzy"], cmd_traders_admin))
    app.add_handler(CommandHandler(["trader"], cmd_trader_panel))
    app.add_handler(CommandHandler("sygnal", cmd_signal))
