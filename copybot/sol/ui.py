"""Ekran Solany w Telegramie: saldo, pozycje, ustawienia, demo/LIVE, wpłata, wypłata, eksport klucza."""
import asyncio
import html
import logging

from solders.pubkey import Pubkey
from telegram import InlineKeyboardButton as B
from telegram import InlineKeyboardMarkup as KB
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, ConversationHandler, MessageHandler, filters, CommandHandler

from ..bot import HOME, S, UI_TIMEOUT, header, require_accepted, show
from .engine import Skip, fmt_tokens, short
from .parser import LAMPORTS

log = logging.getLogger(__name__)
SX = [B("« Pulpit", callback_data="home")]
AMOUNTS = (0.01, 0.05, 0.1, 0.25, 0.5)
MULTS = (0.5, 1.0, 2.0)
PCTS = (1, 5, 10, 25, 50, 100)
MAXES = (0.1, 0.5, 1.0, 2.0, 0)
SLIPS = (300, 500, 1000, 2000, 3000)
WD, IMP = 40, 41


def valid_address(text: str) -> str | None:
    try:
        return str(Pubkey.from_string(text.strip()))
    except Exception:  # noqa: BLE001
        return None


def mode_txt(mode: str, sol_buy: float, pct: float, mult: float) -> str:
    """Ile bot wyda na kopię zakupu – zwykłymi słowami."""
    if mode == "pct":
        return f"{pct:g}% kwoty tradera (on kupi za 10 SOL → Ty za {10 * pct / 100:g} SOL)"
    if mode == "ratio":
        return f"ta sama część portfela co trader{f' × {mult:g}' if mult != 1 else ''}"
    return f"stała kwota {sol_buy:g} SOL na każdy zakup"


def buy_txt(u) -> str:
    cap = f"maks. {u['sol_max']:g} SOL na jeden zakup" if u["sol_max"] > 0 else "bez limitu na zakup"
    return (f"{mode_txt(u['sol_buy_mode'], u['sol_buy'], u['sol_pct'], u['sol_mult'])} · {cap} · "
            f"tolerancja ceny {u['sol_slippage_bps'] / 100:g}%")


async def _ack(update: Update, text=None, alert=False):
    if update.callback_query:
        try:
            await update.callback_query.answer(text, show_alert=alert)
        except BadRequest:
            pass


async def main_view(s, u) -> tuple[str, KB]:
    se = s.sol
    pub, _ = se.wallet(u["tg_id"])
    live = se.is_live(u)
    lam = await se.sol_lamports(u, max_age=0)
    hold = await se.holdings(u)
    lines = [f"🟣 <b>Solana</b> · {'💰 LIVE' if live else '🧪 demo'}",
             f"Saldo: <b>{lam / LAMPORTS:.4f} SOL</b>", f"Zakupy: {buy_txt(u)}", ""]
    rows = []
    items = list(hold.items())[:8]
    infos = await asyncio.gather(*(se.token_info(m) for m, _ in items))
    values = await asyncio.gather(*(se.value_in_sol(m, r) for m, r in items))
    total = 0.0
    for (mint, raw), (sym, dec), val in zip(items, infos, values):
        total += val or 0
        value = f"≈ {val:.4f} SOL" if val is not None else "(brak ceny)"
        lines.append(f"• <b>{html.escape(sym)}</b> {fmt_tokens(raw / 10 ** dec)} {value}")
        rows.append([B(f"Sprzedaj 50% {sym[:10]}", callback_data=f"sx:sell:50:{mint}"),
                     B(f"100% {sym[:10]}", callback_data=f"sx:sell:100:{mint}")])
    if not items:
        lines.append("Brak tokenów. Bot kupi je, gdy kopiowany portfel Solany coś kupi.")
    else:
        all_ = lam / LAMPORTS + total
        lines.append(f"\nRazem ≈ <b>{all_:.4f} SOL</b>"
                     + (f" · wynik demo {(all_ / s.cfg.sol_paper_start - 1) * 100:+.1f}%" if not live else ""))
    copied = [ld["name"] for ld in s.db.copied_leaders(u["tg_id"]) if ld["source"] == "sol"]
    lines.append(f"\nKopiujesz portfele: {html.escape(', '.join(copied)) if copied else '— żadnego (👥 Traderzy)'}")
    if not u["active"]:
        lines.append("⏸ Kopiowanie wyłączone – włączysz je na pulpicie.")
    rows += [[B("⚙️ Ustawienia Solany", callback_data="sx:set"),
              B("Przejdź na 💰 LIVE" if not live else "Przejdź na 🧪 demo", callback_data="sx:mode")],
             [B("👛 Adres / wpłata", callback_data="sx:wallet"), B("📜 Historia", callback_data="sx:hist")],
             [B("🔄 Odśwież", callback_data="sx")], HOME]
    return "\n".join(lines), KB(rows)


def settings_view(u) -> tuple[str, KB]:
    def mark(cur, v, label):
        return f"✓ {label}" if abs((cur or 0) - v) < 1e-9 else label
    mode = u["sol_buy_mode"]
    text = ("⚙️ <b>Ustawienia kopiowania</b>\n\n"
            "<b>Ile wydać, gdy trader coś kupi?</b>\n"
            "• <i>Stała kwota</i> – zawsze tyle samo SOL (najprościej, polecane na start).\n"
            "• <i>% kwoty tradera</i> – np. 10%: on kupi za 5 SOL, Ty za 0,5 SOL. Grasz jak on, tylko mniejszymi "
            "pieniędzmi.\n"
            "• <i>% portfela jak trader</i> – wydajesz taką część swojego salda, jaką on wydał ze swojego.\n"
            "Gdy trader sprzedaje, sprzedajesz tę samą część swoich tokenów.\n\n"
            "<b>Maks. na jeden zakup</b> – bezpiecznik: nigdy nie wydasz więcej naraz.\n"
            "<b>Tolerancja ceny</b> – o ile cena może się zmienić, zanim zakup dojdzie do skutku. Memecoiny skaczą, "
            "więc 10–30% to norma; za mało = zakupy często nie wejdą.\n\n"
            f"Teraz: {buy_txt(u)}\n"
            "<i>Każdemu portfelowi możesz też ustawić osobną kwotę – na jego karcie.</i>")
    rows = [header("Ile wydać na zakup"),
            [B(("✓ " if mode == "fixed" else "") + "Stała kwota", callback_data="sx:bm:fixed"),
             B(("✓ " if mode == "pct" else "") + "% kwoty tradera", callback_data="sx:bm:pct")],
            [B(("✓ " if mode == "ratio" else "") + "% portfela jak trader", callback_data="sx:bm:ratio")]]
    if mode == "fixed":
        rows.append([B(mark(u["sol_buy"], a, f"{a:g} SOL"), callback_data=f"sx:amt:{a}") for a in AMOUNTS])
    elif mode == "pct":
        rows.append([B(mark(u["sol_pct"], v, f"{v:g}%"), callback_data=f"sx:pct:{v}") for v in PCTS])
    else:
        rows.append([B(mark(u["sol_mult"], m, f"{m:g}x"), callback_data=f"sx:mult:{m}") for m in MULTS])
    rows += [header("Maks. na jeden zakup (SOL)"),
             [B(mark(u["sol_max"], m, f"{m:g}" if m else "bez limitu"), callback_data=f"sx:max:{m}") for m in MAXES],
             header("Tolerancja ceny"),
             [B(mark(u["sol_slippage_bps"], b, f"{b / 100:g}%"), callback_data=f"sx:slip:{b}") for b in SLIPS],
             [B("🔄 Zacznij ćwiczenia od nowa", callback_data="sx:reset")], SX]
    return text, KB(rows)


def history_text(s, u) -> str:
    rows = s.sol.history(u["tg_id"])
    if not rows:
        return "📜 <b>Historia Solany</b>\n\nJeszcze nic."
    lines = ["📜 <b>Historia Solany</b>", ""]
    for r in rows:
        sym = html.escape(r["symbol"] or short(r["mint"]))
        if r["status"] == "filled":
            lat = f" · ⚡{r['latency_ms']} ms" if r["latency_ms"] is not None else ""
            src = f" · {html.escape(r['info'])}" if r["info"] else ""
            link = f' · <a href="https://solscan.io/tx/{r["sig"]}">tx</a>' if r["sig"] else ""
            lines.append(f"{'🟢' if r['side'] == 'buy' else '🔴'} {sym}: {r['sol']:.4f} SOL "
                         f"<i>({'demo' if r['paper'] else 'LIVE'}{src}{lat})</i>{link}")
        else:
            lines.append(f"⚠️ {sym}: <i>{html.escape((r['info'] or r['status'])[:70])}</i>")
    return "\n".join(lines)


async def on_sol_button(update: Update, ctx):
    if not (u := await require_accepted(update, ctx)):
        return await _ack(update)
    s = S(ctx)
    if not getattr(s, "sol", None):
        await _ack(update, "Solana jest wyłączona", True)
        return
    data = update.callback_query.data
    uid = u["tg_id"]
    parts = data.split(":")
    try:
        if data == "sx":  # dawny ekran Solany – teraz pulpit główny
            from .axiom import home_view
            await _ack(update, "⏳")
            return await show(update, *await asyncio.wait_for(home_view(s, u), UI_TIMEOUT))
        act = parts[1]
        if act == "set":
            from .axiom import settings_view as ax_settings
            await _ack(update)
            return await show(update, *ax_settings(s, u))
        if act in ("bm", "amt", "mult", "pct", "max", "slip"):
            field = {"bm": "sol_buy_mode", "amt": "sol_buy", "mult": "sol_mult", "pct": "sol_pct", "max": "sol_max",
                     "slip": "sol_slippage_bps"}[act]
            val = parts[2] if act == "bm" else int(parts[2]) if act == "slip" else float(parts[2])
            if act == "bm" and val not in ("fixed", "pct", "ratio"):
                return await _ack(update)
            s.db.update_user(uid, **{field: val})
            await _ack(update, "Zapisano")
            from .axiom import settings_view as ax_settings
            return await show(update, *ax_settings(s, s.db.get_user(uid)))
        if act == "reset":
            s.sol.paper_reset(uid)
            await _ack(update, f"Ćwiczenia od nowa: masz {s.cfg.sol_paper_start:g} wirtualnych SOL", True)
            from .axiom import settings_view as ax_settings
            return await show(update, *ax_settings(s, s.db.get_user(uid)))
        if act == "mode":
            await _ack(update)
            if s.sol.is_live(u):
                from .axiom import home_view
                s.db.update_user(uid, sol_live=0)
                return await show(update, *await home_view(s, s.db.get_user(uid)))
            return await show(update, "💰 <b>Grać prawdziwymi pieniędzmi?</b>\n\nDo tej pory bot ćwiczył na wirtualnych "
                                      "SOL. Teraz będzie naprawdę kupował i sprzedawał tokeny z Twojego portfela "
                                      f"Solany.\nZakupy: {buy_txt(u)}.\n\n⚠️ Memecoiny potrafią stracić 90% wartości "
                                      "w kilka minut. Wpłać tylko tyle, ile możesz stracić.\n\nNajpierw wpłać SOL na "
                                      "adres z „👛 Portfel / wpłata”.",
                              KB([[B("✅ Tak, przejdź na LIVE", callback_data="sx:modeok")], SX]))
        if act == "modeok":
            s.db.update_user(uid, sol_live=1)
            await _ack(update, "Solana LIVE 💰")
            from .axiom import home_view
            return await show(update, *await home_view(s, s.db.get_user(uid)))
        if act == "hist":
            await _ack(update)
            return await show(update, history_text(s, u), KB([SX]))
        if act == "restore":
            pub = s.sol.restore_wallet(uid)
            await _ack(update, f"Wrócono do {short(pub)}" if pub else "Brak poprzedniego portfela", True)
            act = "wallet"
        if act == "wallet":
            if parts[1] != "restore":
                await _ack(update)
            pub, _ = s.sol.wallet(uid)
            try:
                bal = f"{await s.sol.rpc.balance(pub) / LAMPORTS:.4f} SOL"
            except Exception:  # noqa: BLE001
                bal = "?"
            axiom = s.sol.wallet_source(uid) in ("axiom", "import")
            prev = s.db._q("SELECT pubkey FROM sol_wallet_backup WHERE tg_id = ? ORDER BY id DESC LIMIT 1", (uid,))
            return await show(update, f"👛 <b>{'Twój portfel z Axiom 🔗' if axiom else 'Twój portfel Solana'}</b>\n\n"
                                      f"<code>{pub}</code>\n(dotknij, żeby skopiować)\n\nSaldo prawdziwe: <b>{bal}</b>\n\n"
                                      "Wpłać SOL na ten adres (sieć <b>Solana</b>), żeby kopiować na LIVE. Klucz możesz "
                                      "wyeksportować do Phantoma lub Axiom.",
                              KB([[B("🔗 Podłącz portfel z Axiom", callback_data="sx:imp")]]
                                 + ([[B(f"↩️ Wróć do {short(prev[0]['pubkey'])}", callback_data="sx:restore")]] if prev else [])
                                 + [[B("💸 Wypłać SOL", callback_data="sx:wd"), B("🔑 Eksportuj klucz", callback_data="sx:exp")],
                                  [B("Zobacz na Solscan", url=f"https://solscan.io/account/{pub}")], SX]))
        if act == "exp":
            await _ack(update)
            return await show(update, "🔑 <b>Klucz prywatny</b> daje pełną kontrolę nad portfelem. Nie pokazuj go nikomu.",
                              KB([[B("Pokaż (zniknie po 60 s)", callback_data="sx:expok")], SX]))
        if act == "expok":
            await _ack(update)
            _, kp = s.sol.wallet(uid)
            m = await update.effective_chat.send_message(
                f"<code>{kp}</code>\n\nImport: Phantom/Axiom → importuj klucz prywatny. Wiadomość zniknie za 60 s.",
                parse_mode=ParseMode.HTML)
            if getattr(ctx, "job_queue", None):
                ctx.job_queue.run_once(_delete, 60, data=(m.chat_id, m.message_id))
            return
        if act in ("impok", "impno"):
            secret = ctx.user_data.pop("sol_imp", None)
            if act == "impno" or not secret:
                await _ack(update, "Anulowano")
                return await show(update, "Anulowano – nic nie zmieniłem.", KB([SX]))
            from solders.keypair import Keypair
            pub = s.sol.import_wallet(uid, Keypair.from_base58_string(secret), "axiom")
            await _ack(update, "Podłączono 🔗")
            return await show(update, f"🔗 <b>Podłączono portfel</b> <code>{pub}</code>\n\nBot handluje nim, gdy grasz "
                                      "💰 prawdziwymi SOL (przełącznik na pulpicie). Na ćwiczeniach nic się nie zmienia.",
                              KB([[B("Przejdź na 💰 prawdziwe", callback_data="sx:mode")], SX]))
        if act == "wdok":
            pending = ctx.user_data.pop("sol_wd", None)
            if not pending:
                return await _ack(update, "Brak oczekującej wypłaty", True)
            await _ack(update, "Wysyłam…")
            _, kp = s.sol.wallet(uid)
            try:
                sig = await s.sol.rpc.transfer_sol(kp, pending[0], pending[1])
                return await show(update, f"✅ Wypłacono {pending[1] / LAMPORTS:.6f} SOL\nhttps://solscan.io/tx/{sig}",
                                  KB([SX]))
            except Exception as e:  # noqa: BLE001
                return await show(update, f"❌ Wypłata nieudana: {html.escape(str(e)[:200])}", KB([SX]))
        if act == "sell":
            pct, mint = int(parts[2]), parts[3]
            await _ack(update, "Sprzedaję…")
            try:
                fill = await s.sol.sell_manual(u, mint, pct)
                sym, _ = await s.sol.token_info(mint)
                msg = f"✅ Sprzedano {fmt_tokens(fill.tokens)} {sym} za {fill.sol:.4f} SOL"
            except Skip as e:
                msg = f"⚠️ {e}"
            text, kb = await main_view(s, s.db.get_user(uid))
            return await show(update, f"{html.escape(msg)}\n\n{text}", kb)
    except Exception as e:  # noqa: BLE001
        log.exception("Ekran Solany")
        return await show(update, f"❌ Błąd: {html.escape(str(e)[:200]) or 'przekroczony czas'}", KB([SX, HOME]))


async def _delete(ctx):
    chat_id, msg_id = ctx.job.data
    try:
        await ctx.bot.delete_message(chat_id, msg_id)
    except Exception:  # noqa: BLE001
        pass


# ---------- podłączenie portfela z Axiom (klucz prywatny) ----------

async def imp_start(update: Update, ctx):
    await _ack(update)
    if not await require_accepted(update, ctx):
        return ConversationHandler.END
    await update.effective_chat.send_message(
        "🔗 <b>Podłącz portfel z Axiom</b>\n\n1. W Axiom: <b>Settings → Wallets</b> → przy portfelu <b>Export private key</b>\n"
        "2. Skopiuj klucz i wyślij mi go tutaj – <b>od razu usunę Twoją wiadomość</b> z czatu.\n\n"
        "🔐 Klucz daje pełną kontrolę nad portfelem. Trzymam go zaszyfrowanego i nigdy nie pokazuję. "
        "Najlepiej utwórz w Axiom <b>osobny portfel do bota</b> i trzymaj na nim tylko tyle SOL, ile chcesz grać.\n"
        "/anuluj – przerwij", parse_mode=ParseMode.HTML)
    return IMP


async def imp_text(update: Update, ctx):
    s = S(ctx)
    text = update.message.text or ""
    try:
        await update.message.delete()  # klucz nie zostaje w historii czatu
    except Exception:  # noqa: BLE001
        pass
    try:
        kp = s.sol.parse_secret(text)
    except Skip as e:
        await update.effective_chat.send_message(f"⚠️ {e}\nSpróbuj jeszcze raz albo /anuluj.")
        return IMP
    pub = str(kp.pubkey())
    try:
        bal = f"{await s.sol.rpc.balance(pub) / LAMPORTS:.4f} SOL"
    except Exception:  # noqa: BLE001
        bal = "?"
    ctx.user_data["sol_imp"] = str(kp)
    await update.effective_chat.send_message(
        f"🔗 Podłączyć ten portfel?\n<code>{pub}</code>\nSaldo: <b>{bal}</b>\n\nSprawdź, że to ten sam adres co w Axiom. "
        "Obecny portfel bota zapiszę – możesz do niego wrócić.", parse_mode=ParseMode.HTML,
        reply_markup=KB([[B("✅ Tak, podłącz", callback_data="sx:impok")], [B("Anuluj", callback_data="sx:impno")]]))
    return ConversationHandler.END


async def imp_cancel(update: Update, ctx):
    ctx.user_data.pop("sol_imp", None)
    await update.message.reply_text("Anulowano.")
    return ConversationHandler.END


# ---------- wypłata (rozmowa: adres + kwota) ----------

async def wd_start(update: Update, ctx):
    await _ack(update)
    if not await require_accepted(update, ctx):
        return ConversationHandler.END
    await update.effective_chat.send_message("💸 Wyślij: <code>ADRES ILOŚĆ</code> (np. <code>7xKX… 0.5</code>) "
                                             "albo <code>ADRES all</code>.\n/anuluj – przerwij", parse_mode=ParseMode.HTML)
    return WD


async def wd_text(update: Update, ctx):
    s = S(ctx)
    u = s.db.get_user(update.effective_user.id)
    parts = update.message.text.split()
    dest = valid_address(parts[0]) if parts else None
    if not dest or len(parts) < 2:
        await update.message.reply_text("Format: ADRES ILOŚĆ, np. 7xKX… 0.5")
        return WD
    pub, _ = s.sol.wallet(u["tg_id"])
    bal = await s.sol.rpc.balance(pub)
    try:
        lamports = bal - 5000 if parts[1].lower() in ("all", "max", "wszystko") else int(float(parts[1].replace(",", ".")) * LAMPORTS)
    except ValueError:
        await update.message.reply_text("Nieprawidłowa kwota.")
        return WD
    if lamports <= 0 or lamports + 5000 > bal:
        await update.message.reply_text(f"Za mało środków. Saldo: {bal / LAMPORTS:.4f} SOL.")
        return ConversationHandler.END
    ctx.user_data["sol_wd"] = (dest, lamports)
    await update.message.reply_text(f"💸 Wypłacić <b>{lamports / LAMPORTS:.6f} SOL</b> na\n<code>{dest}</code>?\n"
                                    "Przelewu nie da się cofnąć.", parse_mode=ParseMode.HTML,
                                    reply_markup=KB([[B("✅ Tak, wypłać", callback_data="sx:wdok")], SX]))
    return ConversationHandler.END


async def wd_cancel(update: Update, ctx):
    ctx.user_data.pop("sol_wd", None)
    await update.message.reply_text("Anulowano.")
    return ConversationHandler.END


def register(app: Application):
    """Rejestrować PRZED bot.register."""
    text = filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE
    app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(imp_start, pattern="^sx:imp$"),
                                                      CommandHandler("axiom", imp_start)],
                                        states={IMP: [MessageHandler(text, imp_text)]},
                                        fallbacks=[CommandHandler("anuluj", imp_cancel)], allow_reentry=True,
                                        conversation_timeout=300))
    app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(wd_start, pattern="^sx:wd$")],
                                        states={WD: [MessageHandler(text, wd_text)]},
                                        fallbacks=[CommandHandler("anuluj", wd_cancel)], allow_reentry=True,
                                        conversation_timeout=300))
    app.add_handler(CallbackQueryHandler(on_sol_button, pattern=r"^sx(:.*)?$"))
