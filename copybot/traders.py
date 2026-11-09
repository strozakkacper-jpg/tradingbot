"""Traderzy: lista, profil, kopiuj / obserwuj, dodawanie, zmiana nazwy, usuwanie, sygnały."""
import asyncio
import html
import logging

from telegram import InlineKeyboardButton as B
from telegram import InlineKeyboardMarkup as KB
from telegram import Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ConversationHandler, MessageHandler,
                          filters)

from .bot import HOME, S, SIDES, admin_ids, exchange_kb, is_admin, require_accepted, show, summary
from .config import KEY_HINTS, exchange_name, friendly_error, needs_password, normalize_secret
from .db import now_ms
from .engine import SIDE_PL, make_exchange, user_exchange
from .reports import fmt_pct, leader_report, leader_text, ranking
from .sizing import LeaderOrder

log = logging.getLogger(__name__)
A_NAME, A_SOURCE, A_EXCH, A_KEY, A_SECRET, A_PASS, R_NAME, A_WALLET = range(10, 18)
TRADERS = [B("« Traderzy", callback_data="traders")]


def src_txt(s, lead) -> str:
    if lead["source"] == "sol":
        return f"🟣 Solana {lead['wallet'][:4]}…{lead['wallet'][-4:]}" if lead["wallet"] else "🟣 Solana"
    return f"📡 {exchange_name(user_exchange(s.cfg, lead))}" if lead["source"] == "api" else "✍️ sygnały"


async def _ack(update: Update, text=None, alert=False):
    if update.callback_query:
        try:
            await update.callback_query.answer(text, show_alert=alert)
        except BadRequest:
            pass


# ---------- lista i profil ----------

async def show_traders(update: Update, ctx):
    s = S(ctx)
    uid = update.effective_user.id
    mine = s.db.follows_of(uid)
    rows = await ranking(s)
    lines = ["👥 <b>Traderzy</b>", ""]
    kb = []
    if not rows:
        lines.append("Nie masz jeszcze żadnych traderów. Dodaj pierwszego przyciskiem poniżej 👇")
    for lead, rep, (_, copying) in rows:
        status = mine.get(lead["id"])
        icon = "✅" if status is True else "👁" if status is False else "▫️"
        lines.append(f"{icon} <b>{html.escape(lead['name'])}</b> · {src_txt(s, lead)}\n"
                     f"      30 dni {fmt_pct(rep['d30'])} · od startu {fmt_pct(rep['total'])} · "
                     f"spadek {fmt_pct(-rep['max_dd'])}")
        kb.append([B(f"{icon} {lead['name'][:28]}", callback_data=f"tr:{lead['id']}")])
    if rows:
        lines += ["", "✅ kopiujesz · 👁 tylko powiadomienia · ▫️ nie śledzisz"]
    kb.append([B("➕ Dodaj tradera", callback_data="add")])
    kb.append(HOME)
    await show(update, "\n".join(lines), KB(kb))


async def show_profile(update: Update, ctx, leader_id: int | None, notice: str | None = None):
    s = S(ctx)
    lead = s.db.get_leader(leader_id)
    if not lead or lead["status"] != "active":
        return await show_traders(update, ctx)
    uid = update.effective_user.id
    status = s.db.follows_of(uid).get(lead["id"])
    rep = await leader_report(s, lead["id"])
    counts = s.db.follower_counts().get(lead["id"], (0, 0))
    text = leader_text(rep, s.cfg.quote, lead, counts)
    text = text.replace("📡 konto giełdowe, kopiowane automatycznie", src_txt(s, lead) + " – automatycznie", 1)
    text = text.replace("✍️ sygnały wysyłane przez tradera", "✍️ sygnały wysyłane w bocie", 1)
    if lead["source"] == "sol":
        text = text.replace("✍️ sygnały wysyłane w bocie", src_txt(s, lead) + " – kopiowany na żywo", 1)
        text += (f'\n\n🔎 <a href="https://solscan.io/account/{lead["wallet"]}">Portfel na Solscan</a> · '
                 f'<a href="https://gmgn.ai/sol/address/{lead["wallet"]}">GMGN</a>')
    me = {True: "✅ <b>Kopiujesz</b> tego tradera.", False: "👁 <b>Obserwujesz</b> – dostajesz tylko powiadomienia.",
          None: "▫️ Nie śledzisz tego tradera."}[status]
    text = (f"ℹ️ {html.escape(notice)}\n\n" if notice else "") + text + f"\n\n{me}"
    if lead["source"] == "manual" and lead["owner_tg_id"] == uid:
        text += ("\n\n✍️ <b>Twoje sygnały:</b> <code>/sygnal buy BTC/USDT 10</code> (kup za 10% salda) · "
                 "<code>/sygnal sell BTC/USDT 100</code> (sprzedaj całość)")
    rows = []
    if status is True:
        rows.append([B("👁 Tylko powiadomienia", callback_data=f"wt:{lead['id']}"),
                     B("🔕 Przestań śledzić", callback_data=f"uf:{lead['id']}")])
    elif status is False:
        rows.append([B("✅ Kopiuj", callback_data=f"cp:{lead['id']}"),
                     B("🔕 Przestań śledzić", callback_data=f"uf:{lead['id']}")])
    else:
        rows.append([B("✅ Kopiuj", callback_data=f"cp:{lead['id']}"),
                     B("👁 Tylko powiadomienia", callback_data=f"wt:{lead['id']}")])
    if s.cfg.webapp_url:
        rows.append([B("📈 Wykres", web_app=WebAppInfo(f"{s.cfg.webapp_url}/app"))])
    if lead["owner_tg_id"] == uid or uid in admin_ids(s):
        rows.append([B("✏️ Zmień nazwę", callback_data=f"ren:{lead['id']}"),
                     B("🗑 Usuń", callback_data=f"del:{lead['id']}")])
    rows.append(TRADERS + HOME)
    await show(update, text, KB(rows))


async def on_trader_button(update: Update, ctx):
    q = update.callback_query
    if not (u := await require_accepted(update, ctx)):
        return await _ack(update)
    s = S(ctx)
    data = q.data
    if data == "traders":
        await _ack(update)
        return await show_traders(update, ctx)
    action, lid = data.split(":")
    lid = int(lid)
    lead = s.db.get_leader(lid)
    if not lead or lead["status"] != "active":
        await _ack(update, "Ten trader już nie istnieje", True)
        return await show_traders(update, ctx)
    uid = u["tg_id"]
    if action == "tr":
        await _ack(update)
        return await show_profile(update, ctx, lid)
    if action == "cp":
        s.db.follow(uid, lid, True)
        if lead["source"] == "sol" and getattr(s, "sol", None):
            s.sol.wallet(uid)  # portfel Solany (demo: wirtualne SOL) tworzy się przy pierwszym kopiowaniu
        msg = f"Kopiujesz: {lead['name']}" + ("" if u["active"] else " – włącz kopiowanie na pulpicie ▶️")
        await _ack(update, msg[:200], not u["active"])
        return await show_profile(update, ctx, lid, None if not u["active"] else msg)
    if action == "wt":
        s.db.follow(uid, lid, False)
        await _ack(update, "Tylko powiadomienia 👁")
        return await show_profile(update, ctx, lid)
    if action == "uf":
        s.db.unfollow(uid, lid)
        await _ack(update, "Nie śledzisz już tego tradera")
        return await show_profile(update, ctx, lid)
    can_edit = lead["owner_tg_id"] == uid or uid in admin_ids(s)
    if action == "del":
        if not can_edit:
            return await _ack(update, "Może to zrobić tylko osoba, która dodała tradera", True)
        await _ack(update)
        return await show(update, f"🗑 <b>Usunąć tradera „{html.escape(lead['name'])}”?</b>\n\nNikt nie będzie go już "
                                  "kopiował ani obserwował. Otwarte pozycje zostają na kontach.",
                          KB([[B("✅ Tak, usuń", callback_data=f"delok:{lid}")], [B("« Wróć", callback_data=f"tr:{lid}")]]))
    if action == "delok":
        if not can_edit:
            return await _ack(update, "Brak uprawnień", True)
        s.db.delete_leader(lid)
        if s.watchers:
            s.watchers.sync()
        await _ack(update, f"Usunięto {lead['name']}", True)
        return await show_traders(update, ctx)


# ---------- dodawanie tradera ----------

async def add_start(update: Update, ctx):
    await _ack(update)
    if not await require_accepted(update, ctx):
        return ConversationHandler.END
    ctx.user_data["add"] = {}
    await update.effective_chat.send_message(
        "➕ <b>Nowy trader</b> (krok 1/3)\n\nJak go nazwać? Np. <i>Kacper Swing</i> albo <i>Moje sygnały</i>.\n\n"
        "/anuluj – przerwij", parse_mode=ParseMode.HTML)
    return A_NAME


async def add_name(update: Update, ctx):
    name = update.message.text.strip()[:40]
    if len(name) < 2:
        await update.message.reply_text("Nazwa musi mieć min. 2 znaki.")
        return A_NAME
    ctx.user_data.setdefault("add", {})["name"] = name
    await update.message.reply_text(
        "➕ <b>Nowy trader</b> (krok 2/3)\n\nSkąd bot ma brać jego transakcje?\n\n"
        "📡 <b>Konto na giełdzie</b> – trader daje klucz API <b>tylko do odczytu</b>. Bot widzi każdą jego transakcję "
        "w około sekundę i kopiuje automatycznie.\n\n"
        "🟣 <b>Portfel Solana</b> – adres portfela tradera z Axiom, Pump.fun, GMGN… Bot widzi jego wymiany na "
        "blockchainie w ułamku sekundy i kopiuje przez Jupitera. Klucz nie jest potrzebny.\n\n"
        "✍️ <b>Sygnały w bocie</b> – Ty (albo ktoś z dostępem) wysyłasz komendy typu <code>/sygnal buy BTC/USDT 10</code>.",
        parse_mode=ParseMode.HTML,
        reply_markup=KB([[B("📡 Konto na giełdzie", callback_data="src:api")],
                         [B("🟣 Portfel Solana (Axiom, Pump.fun…)", callback_data="src:sol")],
                         [B("✍️ Sygnały w bocie", callback_data="src:manual")]]))
    return A_SOURCE


async def add_source(update: Update, ctx):
    q = update.callback_query
    await _ack(update)
    if q.data == "src:manual":
        return await _add_finish(update, ctx, "manual")
    if q.data == "src:sol":
        if not getattr(S(ctx), "sol", None):
            await q.edit_message_text("Solana jest wyłączona w tej instalacji.")
            return ConversationHandler.END
        await q.edit_message_text(
            "🟣 <b>Portfel Solana</b> (krok 3/3)\n\nWklej <b>adres portfela</b> tradera – znajdziesz go w profilu na "
            "Axiom, GMGN, Solscan albo w rankingu kolscan.io.\n/anuluj – przerwij", parse_mode=ParseMode.HTML)
        return A_WALLET
    await q.edit_message_text("➕ <b>Nowy trader</b> (krok 3/3)\n\nNa której giełdzie handluje?",
                              parse_mode=ParseMode.HTML, reply_markup=exchange_kb(S(ctx).cfg, "aex:"))
    return A_EXCH


async def add_exchange(update: Update, ctx):
    q = update.callback_query
    await _ack(update)
    ex = q.data[4:]
    ctx.user_data.setdefault("add", {})["exchange"] = ex
    hint = f"\n\n{KEY_HINTS[ex]}" if ex in KEY_HINTS else ""
    await q.edit_message_text(
        f"🔑 Na koncie tradera na {exchange_name(ex)} utwórz klucz API <b>tylko do odczytu</b> "
        f"(bez handlu i bez wypłat).{hint}\n\nWyślij <b>API Key</b> – wiadomość od razu usunę.\n/anuluj – przerwij",
        parse_mode=ParseMode.HTML)
    return A_KEY


async def add_wallet(update: Update, ctx):
    from .sol.ui import valid_address
    s = S(ctx)
    addr = valid_address(update.message.text)
    if not addr:
        await update.message.reply_text("To nie jest poprawny adres Solany – wklej sam adres (32–44 znaki).")
        return A_WALLET
    preview = ""
    try:
        lam = await asyncio.wait_for(s.sol.rpc.balance(addr), 10)
        tokens = await asyncio.wait_for(s.sol.rpc.token_accounts(addr), 15)
        preview = f"\nPodgląd: {lam / 1e9:.3f} SOL · {len(tokens)} tokenów w portfelu."
    except Exception:  # noqa: BLE001
        preview = "\n(Podgląd portfela chwilowo niedostępny – śledzenie i tak zadziała.)"
    ctx.user_data.setdefault("add", {})["wallet"] = addr
    ctx.user_data["add"]["preview"] = preview
    return await _add_finish(update, ctx, "sol")


async def _wipe(update: Update):
    try:
        await update.message.delete()
    except Exception:  # noqa: BLE001
        pass


async def add_key(update: Update, ctx):
    ctx.user_data.setdefault("add", {})["api_key"] = update.message.text.strip()
    await _wipe(update)
    await update.effective_chat.send_message("Teraz <b>API Secret</b>.", parse_mode=ParseMode.HTML)
    return A_SECRET


async def add_secret(update: Update, ctx):
    a = ctx.user_data.setdefault("add", {})
    a["api_secret"] = normalize_secret(update.message.text)
    await _wipe(update)
    if needs_password(a.get("exchange", "")):
        await update.effective_chat.send_message("Teraz <b>passphrase</b>.", parse_mode=ParseMode.HTML)
        return A_PASS
    return await _add_finish(update, ctx, "api")


async def add_pass(update: Update, ctx):
    ctx.user_data.setdefault("add", {})["api_password"] = update.message.text.strip()
    await _wipe(update)
    return await _add_finish(update, ctx, "api")


async def _add_finish(update: Update, ctx, source: str):
    s = S(ctx)
    a = ctx.user_data.pop("add", {})
    chat = update.effective_chat
    uid = update.effective_user.id
    if not a.get("name"):
        await chat.send_message("Coś poszło nie tak – zacznij od nowa: /dodaj")
        return ConversationHandler.END
    keys = {}
    if source == "api":
        msg = await chat.send_message(f"⏳ Sprawdzam klucz na {exchange_name(a.get('exchange', ''))}…")
        ex = make_exchange(s.cfg, a.get("api_key"), a.get("api_secret"), a.get("api_password"),
                           exchange_id=a.get("exchange"))
        try:
            await ex.fetch_balance()
        except Exception as e:  # noqa: BLE001
            await msg.edit_text(f"❌ Klucz nie działa: {html.escape(friendly_error(e))}.\nSpróbuj ponownie: /dodaj")
            return ConversationHandler.END
        finally:
            await ex.close()
        keys = {k: s.vault.enc(a.get(k)) for k in ("api_key", "api_secret", "api_password")}
        keys["exchange"] = a.get("exchange")
    if source == "sol":
        keys = {"wallet": a.get("wallet")}
    lid = s.db.create_leader(a["name"], source, uid, "active", **keys)
    s.db.ensure_reference(lid, s.cfg.paper_start, s.cfg.quote)
    s.db.follow(uid, lid, False)  # dodający od razu obserwuje
    if s.watchers:
        s.watchers.sync()
    how = {"api": "Bot śledzi jego konto na żywo.",
           "sol": "Bot śledzi ten portfel na blockchainie na żywo." + a.get("preview", ""),
           "manual": "Wysyłaj sygnały: <code>/sygnal buy BTC/USDT 10</code> albo <code>/sygnal sell BTC/USDT 100</code>."
           }[source]
    await chat.send_message(
        f"✅ <b>Dodano tradera „{html.escape(a['name'])}”.</b>\n{how}\n\nNa razie dostajesz tylko powiadomienia. "
        "Kopiować od razu?", parse_mode=ParseMode.HTML,
        reply_markup=KB([[B("✅ Kopiuj", callback_data=f"cp:{lid}"), B("👁 Tylko powiadomienia", callback_data=f"tr:{lid}")],
                         TRADERS]))
    # pozostałe osoby z dostępem dowiadują się o nowym traderze
    for other in s.db.approved_users():
        if other["tg_id"] != uid:
            try:
                await ctx.bot.send_message(
                    other["tg_id"], f"➕ Nowy trader w bocie: <b>{html.escape(a['name'])}</b>",
                    parse_mode=ParseMode.HTML, reply_markup=KB([[B("Zobacz", callback_data=f"tr:{lid}")]]))
            except Exception:  # noqa: BLE001
                pass
    return ConversationHandler.END


# ---------- zmiana nazwy ----------

async def rename_start(update: Update, ctx):
    s = S(ctx)
    lid = int(update.callback_query.data.split(":")[1])
    lead = s.db.get_leader(lid)
    uid = update.effective_user.id
    if not lead or not (lead["owner_tg_id"] == uid or uid in admin_ids(s)):
        await _ack(update, "Brak uprawnień", True)
        return ConversationHandler.END
    await _ack(update)
    ctx.user_data["rename"] = lid
    await update.effective_chat.send_message(f"✏️ Nowa nazwa dla „{html.escape(lead['name'])}”:\n/anuluj – przerwij",
                                             parse_mode=ParseMode.HTML)
    return R_NAME


async def rename_done(update: Update, ctx):
    s = S(ctx)
    lid = ctx.user_data.pop("rename", None)
    name = update.message.text.strip()[:40]
    if lid and len(name) >= 2:
        s.db.update_leader(lid, name=name)
        await update.message.reply_text(f"✅ Nowa nazwa: {name}", reply_markup=KB([[B("Zobacz", callback_data=f"tr:{lid}")]]))
    return ConversationHandler.END


async def conv_cancel(update: Update, ctx):
    ctx.user_data.pop("add", None)
    ctx.user_data.pop("rename", None)
    await update.message.reply_text("Anulowano. /menu – pulpit")
    return ConversationHandler.END


# ---------- komendy ----------

async def cmd_traders(update: Update, ctx):
    if await require_accepted(update, ctx):
        await show_traders(update, ctx)


def _signal_leader(s, tg_id: int, explicit: int | None, admin: bool):
    if explicit is not None:
        lead = s.db.get_leader(explicit)
        if lead and lead["status"] == "active" and lead["source"] == "manual" and (admin or lead["owner_tg_id"] == tg_id):
            return lead
        return None
    own = [lead for lead in s.db.leaders_owned(tg_id) if lead["status"] == "active" and lead["source"] == "manual"]
    if len(own) == 1:
        return own[0]
    if admin and not own:
        manual = [lead for lead in s.db.leaders("active") if lead["source"] == "manual"]
        if len(manual) == 1:
            return manual[0]
    return None


async def cmd_signal(update: Update, ctx):
    """/sygnal buy BTC/USDT 10 [#id] – sygnał tradera typu „sygnały w bocie”."""
    if not await require_accepted(update, ctx):
        return
    s = S(ctx)
    args = list(ctx.args)
    explicit = None
    if args and args[-1].startswith("#"):
        try:
            explicit = int(args.pop()[1:])
        except ValueError:
            pass
    lead = _signal_leader(s, update.effective_user.id, explicit, is_admin(ctx, update))
    if not lead:
        manual = [f"#{ld['id']} {ld['name']}" for ld in s.db.leaders("active") if ld["source"] == "manual"]
        hint = ("\nTwoi traderzy z sygnałami: " + ", ".join(manual) + "\nDopisz numer, np. /sygnal buy BTC/USDT 10 #3"
                if manual else "\nNajpierw dodaj tradera typu „✍️ Sygnały w bocie”: /dodaj")
        return await update.message.reply_text("Nie wiem, dla którego tradera wysłać sygnał." + hint)
    try:
        side = SIDES[args[0].lower()]
        symbol = args[1].upper()
        pct = float(args[2].replace(",", ".").rstrip("%"))
        assert 0 < pct <= 100
    except (IndexError, KeyError, ValueError, AssertionError):
        return await update.message.reply_text(
            "Użycie: /sygnal buy|sell PARA PROCENT\n• /sygnal buy BTC/USDT 10 – kup za 10% salda\n"
            "• /sygnal sell BTC/USDT 100 – sprzedaj całą pozycję")
    market = s.engine.m(lead)
    resolved = await market.resolve(symbol) if hasattr(market, "resolve") else (symbol if await market.has(symbol) else None)
    if not resolved:
        return await update.message.reply_text(f"Para {symbol} nie istnieje na {exchange_name(market.exchange_id)}.")
    price = await market.price(resolved)
    order = LeaderOrder(id=f"manual-{now_ms()}", symbol=resolved, side=side, amount=0, cost=0, price=price,
                        ts=now_ms())
    res = await s.engine.copy(order, pct / 100, lead["id"])
    await update.message.reply_text(f"📡 {lead['name']}: {SIDE_PL[side]} {resolved} {pct:g}%\n{summary(res)}")


def register(app: Application):
    """Rejestrować PRZED bot.register – handlery bota łapią wszystkie pozostałe przyciski."""
    private = filters.ChatType.PRIVATE
    text = filters.TEXT & ~filters.COMMAND & private
    cancel = [CommandHandler("anuluj", conv_cancel)]
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(add_start, pattern="^add$"),
                      CommandHandler(["dodaj", "zostan_traderem"], add_start, filters=private)],
        states={A_NAME: [MessageHandler(text, add_name)],
                A_SOURCE: [CallbackQueryHandler(add_source, pattern="^src:")],
                A_EXCH: [CallbackQueryHandler(add_exchange, pattern="^aex:")],
                A_KEY: [MessageHandler(text, add_key)],
                A_SECRET: [MessageHandler(text, add_secret)],
                A_PASS: [MessageHandler(text, add_pass)],
                A_WALLET: [MessageHandler(text, add_wallet)]},
        fallbacks=cancel, allow_reentry=True, conversation_timeout=600,
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(rename_start, pattern=r"^ren:\d+$")],
        states={R_NAME: [MessageHandler(text, rename_done)]},
        fallbacks=cancel, allow_reentry=True, conversation_timeout=300,
    ))
    app.add_handler(CallbackQueryHandler(on_trader_button, pattern=r"^(traders|(tr|cp|wt|uf|del|delok):\d+)$"))
    app.add_handler(CommandHandler(["traderzy", "wyniki"], cmd_traders))
    app.add_handler(CommandHandler("sygnal", cmd_signal))
