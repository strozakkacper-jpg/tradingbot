"""Dodatki: kopia zapasowa bazy, alerty o problemach, raport dzienny/tygodniowy, eksport CSV,
inteligentne kwoty na traderów i PIN do wypłat."""
import csv
import gzip
import hashlib
import html
import io
import logging
import os
import re
import secrets
import sqlite3
import tempfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from ..bot import S, admin_ids, owner_ids, require_accepted
from ..db import now_ms

log = logging.getLogger(__name__)
DAY_MS = 24 * 3600 * 1000


# ---------------- kopia zapasowa ----------------

def make_backup(db) -> bytes:
    """Spójna kopia bazy (sqlite backup API) spakowana gzipem."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        dst = sqlite3.connect(path)
        with db.lock:
            db.conn.backup(dst)
        dst.close()
        with open(path, "rb") as f:
            return gzip.compress(f.read(), 6)
    finally:
        os.unlink(path)


async def send_backup(s, bot, to: int):
    data = make_backup(s.db)
    name = f"copybot-kopia-{datetime.now(ZoneInfo(s.cfg.timezone)):%Y-%m-%d}.db.gz"
    await bot.send_document(to, InputFile(io.BytesIO(data), filename=name), caption=(
        "🗄 Kopia zapasowa bazy bota (portfele, traderzy, historia).\n"
        "Klucze portfeli są w niej ZASZYFROWANE – do odczytu potrzebny jest ENCRYPTION_KEY z Railway "
        "(Variables). Zapisz go w bezpiecznym miejscu, osobno od tego pliku. Nie wysyłaj tego pliku nikomu."))


async def backup_job(ctx):
    s = ctx.application.bot_data["svc"]
    for uid in owner_ids(s):
        try:
            await send_backup(s, ctx.bot, uid)
        except Exception:  # noqa: BLE001
            log.exception("Kopia zapasowa dla %s", uid)


async def cmd_backup(update: Update, ctx):
    s = S(ctx)
    if update.effective_user.id not in owner_ids(s):
        return await update.message.reply_text("Kopię zapasową dostaje tylko właściciel 👑.")
    await send_backup(s, ctx.bot, update.effective_user.id)


# ---------------- alerty o problemach ----------------

_ALERTS: dict[str, float] = {}
_DOWN_SINCE: list[float] = []


def health_problems(s) -> list[tuple[str, str]]:
    """[(klucz, opis)] – co teraz nie działa."""
    out = []
    se = s.sol
    w = getattr(se, "watcher", None)
    if w is not None:
        if not w.connected:
            if not _DOWN_SINCE:
                _DOWN_SINCE.append(time.time())
            elif time.time() - _DOWN_SINCE[0] > 180:
                out.append(("ws", f"🔴 Brak połączenia z siecią Solana od {int((time.time() - _DOWN_SINCE[0]) / 60)} min"
                                  f" – bot nie widzi ruchów traderów. Ostatni błąd: {w.last_error or '—'}"))
        else:
            _DOWN_SINCE.clear()
        err = str(w.last_error or "")
        if any(x in err for x in ("401", "Unauthorized", "invalid api key")):
            out.append(("helius", "🔑 Helius odrzuca klucz (HELIUS_API_KEY) – sprawdź plan / klucz na Railway"))
    if getattr(se.rpc, "auth_error", None):
        out.append(("rpc", f"🔑 RPC (Helius) odrzuca zapytania: {se.rpc.auth_error} – sprawdź HELIUS_API_KEY"))
    if getattr(se.jup, "auth_error", None):
        out.append(("jup", f"🔑 Jupiter odrzuca klucz: {se.jup.auth_error} – sprawdź JUPITER_API_KEY"))
    hits = [t for t in getattr(se.jup, "limit_hits", []) if time.time() - t < 3600]
    if len(hits) > 150:
        out.append(("jup429", f"⏳ Limit Jupitera: {len(hits)} razy w ostatniej godzinie – kopie się spóźniają. "
                              "Pomaga płatny klucz Jupitera albo mniej włączonych traderów."))
    copying = s.db._q("SELECT COUNT(*) n FROM users WHERE active = 1 AND approved = 1")[0]["n"]
    if w is not None and copying and w.connected and time.time() - (w.last_swap or w.since) > 3 * 3600:
        out.append(("idle", "😴 Od 3 h bot nie wykrył żadnej wymiany Twoich traderów – może są nieaktywni "
                            "(👥 Traderzy → 🔍 kto jest aktywny)"))
    return out


async def health_job(ctx):
    s = ctx.application.bot_data["svc"]
    if not getattr(s, "sol", None):
        return
    for key, text in health_problems(s):
        if time.time() - _ALERTS.get(key, 0) < 2 * 3600:  # ten sam alert maks. co 2 h
            continue
        _ALERTS[key] = time.time()
        for uid in admin_ids(s):
            try:
                await ctx.bot.send_message(uid, f"⚠️ <b>ALERT</b>\n{html.escape(text)}", parse_mode=ParseMode.HTML)
            except Exception:  # noqa: BLE001
                log.debug("Alert dla %s", uid, exc_info=True)


# ---------------- raport dzienny / tygodniowy ----------------

def period_report(s, u, days: int, until: int | None = None, label: str | None = None) -> str | None:
    """Raport z ostatnich `days` dni (do teraz albo do `until` – wtedy odtworzony z historii)."""
    se = s.sol
    uid, paper = u["tg_id"], not se.is_live(u)
    until = until or now_ms()
    since = until - days * DAY_MS
    rows = s.db._q("""SELECT t.side, t.pnl, t.status, l.name FROM sol_trades t LEFT JOIN leaders l ON l.id = t.leader_id
                      WHERE t.tg_id = ? AND t.paper = ? AND t.ts >= ? AND t.ts < ?""", (uid, int(paper), since, until))
    if not rows:
        return None
    sells = [r for r in rows if r["side"] == "sell" and r["status"] == "filled" and r["pnl"] is not None]
    pnl = sum(r["pnl"] for r in sells)
    wins = sum(1 for r in sells if r["pnl"] > 0)
    per: dict[str, float] = {}
    for r in sells:
        per[r["name"] or "ręcznie"] = per.get(r["name"] or "ręcznie", 0) + r["pnl"]
    eq = [x for x in s.db.sol_equity_series(uid, paper, since) if x[0] < until]
    title = "📅 <b>RAPORT DNIA</b>" if days == 1 else "🗓 <b>RAPORT TYGODNIA</b>"
    if label:
        title += f" · {html.escape(label)}"
    lines = [f"{title} · {'💰 prawdziwe SOL' if not paper else '🧪 ćwiczenia'}",
             f"{'🟢 Zarobek' if pnl >= 0 else '🔴 Strata'} na sprzedanych: <b>{pnl:+.4f} SOL</b>"]
    if len(eq) >= 2 and eq[0][1]:
        lines.append(f"Portfel: {eq[0][1]:.4f} → <b>{eq[-1][1]:.4f} SOL</b> ({(eq[-1][1] / eq[0][1] - 1) * 100:+.1f}%)")
    buys = sum(1 for r in rows if r["side"] == "buy" and r["status"] == "filled")
    skipped = sum(1 for r in rows if r["status"] != "filled")
    lines.append(f"Kupna {buys} · sprzedaże {len(sells)} (z zyskiem {wins}) · pominięte {skipped}")
    if per:
        top = sorted(per.items(), key=lambda kv: -kv[1])
        lines.append("🏆 Najlepsi: " + ", ".join(f"{html.escape(k)} {v:+.3f}" for k, v in top[:3] if v > 0)
                     if any(v > 0 for _, v in top) else "🏆 Najlepsi: —")
        worst = [(k, v) for k, v in reversed(top) if v < 0][:2]
        if worst:
            lines.append("📉 Najsłabsi: " + ", ".join(f"{html.escape(k)} {v:+.3f}" for k, v in worst))
    return "\n".join(lines)


async def report_job(ctx):
    s = ctx.application.bot_data["svc"]
    if not getattr(s, "sol", None):
        return
    weekly = datetime.now(ZoneInfo(s.cfg.timezone)).weekday() == 6  # niedziela
    for u in s.db._q("SELECT u.* FROM users u JOIN sol_wallets w ON w.tg_id = u.tg_id "
                     "WHERE u.approved = 1 AND u.sol_report = 1"):
        for days in ((1, 7) if weekly else (1,)):
            text = period_report(s, u, days)
            if text:
                save_report(s, u["tg_id"], "dzień" if days == 1 else "tydzień", text)
                try:
                    await ctx.bot.send_message(u["tg_id"], text, parse_mode=ParseMode.HTML)
                except Exception:  # noqa: BLE001
                    log.debug("Raport dla %s", u["tg_id"], exc_info=True)


# ---------------- /raporty – 10 ostatnich raportów ----------------

KIND_ICON = {"dzień": "📅", "tydzień": "🗓", "sesja": "📊"}


def report_summary(text: str) -> str | None:
    """Krótka linijka do listy: pierwszy wynik w SOL z raportu (np. „+0.1234 SOL”) i ewentualny % portfela."""
    plain = re.sub(r"<[^>]+>", "", text)
    m = re.search(r"([+−-]\d+[.,]\d+) SOL", plain)
    pct = re.search(r"\(([+−-]\d+[.,]\d+%)\)", plain)
    return " · ".join(x for x in ((m.group(1) + " SOL") if m else None, pct.group(1) if pct else None) if x) or None


def save_report(s, uid: int, kind: str, text: str):
    title = re.sub(r"<[^>]+>", "", text.split("\n", 1)[0]).strip()[:80]
    try:
        s.db.add_report(uid, kind, title, text, report_summary(text))
    except Exception:  # noqa: BLE001
        log.exception("Zapis raportu %s", uid)


def report_list(s, u, n: int = 10) -> list[dict]:
    """Zapisane raporty, a gdy jest ich mniej niż n – uzupełnione raportami poprzednich dni odtworzonymi z historii."""
    uid = u["tg_id"]
    out = [{"id": r["id"], "kind": r["kind"], "ts": r["ts"], "title": r["title"], "summary": r["summary"]}
           for r in s.db.reports(uid, n)]
    if len(out) >= n:
        return out
    tz = ZoneInfo(s.cfg.timezone)
    have_days = {datetime.fromtimestamp(r["ts"] / 1000, tz).date() for r in out if r["kind"] == "dzień"}
    midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    from datetime import timedelta
    for back in range(0, 30):
        if len(out) >= n:
            break
        day_end = midnight - timedelta(days=back)
        day = (day_end - timedelta(days=1)).date()
        if day in have_days:
            continue
        text = period_report(s, u, 1, int(day_end.timestamp() * 1000), day.strftime("%d.%m"))
        if text:
            out.append({"id": None, "day": day.isoformat(), "kind": "dzień", "ts": int(day_end.timestamp() * 1000) - 1,
                        "title": f"Raport dnia {day.strftime('%d.%m')} (z historii)", "summary": report_summary(text)})
    out.sort(key=lambda r: -r["ts"])
    return out[:n]


def report_text(s, u, rid: int | None = None, day: str | None = None) -> str | None:
    if rid:
        r = s.db.report(u["tg_id"], rid)
        return r["text"] if r else None
    if day:
        from datetime import timedelta
        tz = ZoneInfo(s.cfg.timezone)
        d = datetime.fromisoformat(day).replace(tzinfo=tz)
        end = d + timedelta(days=1)
        return period_report(s, u, 1, int(end.timestamp() * 1000), d.strftime("%d.%m") + " · odtworzony z historii")
    return None


def reports_view(s, u) -> tuple[str, InlineKeyboardMarkup]:
    items = report_list(s, u)
    tz = ZoneInfo(s.cfg.timezone)
    if not items:
        return ("📑 <b>Raporty</b>\n\nJeszcze nie ma żadnego raportu. Raport dnia przychodzi codziennie wieczorem, "
                "a raport sesji – gdy wyłączysz kopiowanie.", InlineKeyboardMarkup([]))
    lines = [f"📑 <b>Ostatnie raporty ({len(items)})</b> – kliknij numer, żeby otworzyć\n"]
    btns = []
    for i, r in enumerate(items, 1):
        when = datetime.fromtimestamp(r["ts"] / 1000, tz).strftime("%d.%m %H:%M")
        lines.append(f"{i}. {KIND_ICON.get(r['kind'], '📄')} {when} · {html.escape(r['title'])}"
                     + (f" · <b>{html.escape(r['summary'])}</b>" if r.get("summary") else ""))
        btns.append(InlineKeyboardButton(str(i), callback_data=f"rp:{r['id']}" if r["id"] else f"rpd:{r['day']}"))
    rows = [btns[i:i + 5] for i in range(0, len(btns), 5)]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def cmd_reports(update: Update, ctx):
    if u := await require_accepted(update, ctx):
        text, kb = reports_view(S(ctx), u)
        await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def on_report_button(update: Update, ctx):
    q = update.callback_query
    s = S(ctx)
    u = s.db.get_user(q.from_user.id)
    await q.answer()
    if not u:
        return
    data = q.data
    if data == "rp:list":
        text, kb = reports_view(s, u)
        await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return
    text = (report_text(s, u, rid=int(data[3:])) if data.startswith("rp:") else report_text(s, u, day=data[4:]))
    await q.message.reply_text(text or "Nie znalazłem tego raportu.", parse_mode=ParseMode.HTML,
                               disable_web_page_preview=True,
                               reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📑 Wszystkie raporty",
                                                                                       callback_data="rp:list")]]))


# ---------------- eksport CSV ----------------

def history_csv(s, uid: int) -> bytes:
    rows = s.db._q("""SELECT t.*, k.symbol, l.name leader FROM sol_trades t LEFT JOIN sol_tokens k ON k.mint = t.mint
                      LEFT JOIN leaders l ON l.id = t.leader_id WHERE t.tg_id = ? ORDER BY t.id""", (uid,))
    buf = io.StringIO()
    wr = csv.writer(buf, delimiter=";")
    wr.writerow(["data", "tryb", "strona", "token", "adres tokena", "SOL", "ilość tokenów", "zysk/strata SOL",
                 "trader", "status", "opis", "transakcja"])
    tz = ZoneInfo(s.cfg.timezone)
    for r in rows:
        wr.writerow([datetime.fromtimestamp(r["ts"] / 1000, tz).strftime("%Y-%m-%d %H:%M:%S"),
                     "ćwiczenia" if r["paper"] else "prawdziwe", "kupno" if r["side"] == "buy" else "sprzedaż",
                     r["symbol"] or "", r["mint"], f"{r['sol'] or 0:.9f}".replace(".", ","),
                     f"{r['tokens'] or 0:.6f}".replace(".", ","),
                     "" if r["pnl"] is None else f"{r['pnl']:.9f}".replace(".", ","), r["leader"] or "ręcznie",
                     r["status"], r["info"] or "", f"https://solscan.io/tx/{r['sig']}" if r["sig"] else ""])
    return ("﻿" + buf.getvalue()).encode("utf-8")  # BOM – Excel otworzy polskie znaki


async def send_csv(s, bot, uid: int):
    data = history_csv(s, uid)
    await bot.send_document(uid, InputFile(io.BytesIO(data), filename=f"historia-transakcji-{uid}.csv"),
                            caption="📄 Historia transakcji (CSV – otworzysz w Excelu / Arkuszach Google). "
                                    "Przyda się do rozliczenia PIT-38.")


async def cmd_export(update: Update, ctx):
    if await require_accepted(update, ctx):
        await send_csv(S(ctx), ctx.bot, update.effective_user.id)


# ---------------- inteligentne kwoty ----------------

def smart_allocation(s, u) -> list[str]:
    """Więcej na traderów, którzy dają Ci zysk (7 dni), mniej na tracących – osobny % na karcie tradera."""
    from .api import ranking
    if u["sol_buy_mode"] != "pct":
        return []
    rows = [r for r in ranking(s, u["tg_id"], not s.sol.is_live(u)) if r["status"] == "copy" and r["lid"]
            and r["sells"] >= 3]
    if len(rows) < 2:
        return []
    rows.sort(key=lambda r: -r["pnl_7d"])
    third = max(len(rows) // 3, 1)
    base = u["sol_pct"]
    changes = []
    for i, r in enumerate(rows):
        if i < third and r["pnl_7d"] > 0:
            pct, tag = base * 1.5, "⬆️"
        elif i >= len(rows) - third and r["pnl_7d"] < 0:
            pct, tag = base * 0.5, "⬇️"
        else:
            pct, tag = None, None
        fr = s.db.follow_row(u["tg_id"], r["lid"])
        if (fr["sol_pct"] if fr else None) != (float(f"{pct:.2g}") if pct else None):
            s.db.set_follow_amount(u["tg_id"], r["lid"], pct=float(f"{pct:.2g}") if pct else None)
            if tag:
                changes.append(f"{tag} {r['name']} {pct:.2g}% ({r['pnl_7d']:+.3f} SOL w 7 dni)")
    return changes


async def smart_job(ctx):
    s = ctx.application.bot_data["svc"]
    if not getattr(s, "sol", None):
        return
    for u in s.db._q("SELECT * FROM users WHERE approved = 1 AND sol_smart = 1 AND sol_autocfg = 0"):  # 🤖 auto liczy to sam
        try:
            ch = smart_allocation(s, u)
            if ch:
                await s.sol.say(u, "auto", "🧠 <b>INTELIGENTNE KWOTY</b> – więcej na tych, którzy dają Ci zysk:\n"
                                + "\n".join(html.escape(c) for c in ch))
        except Exception:  # noqa: BLE001
            log.exception("Inteligentne kwoty %s", u["tg_id"])


# ---------------- PIN ----------------

_PIN_FAILS: dict[int, list[float]] = {}
_UNLOCKED: dict[int, float] = {}  # bot: po /pin odblokowane na 5 min


def hash_pin(pin: str) -> str:
    salt = secrets.token_hex(8)
    return salt + "$" + hashlib.sha256((salt + pin).encode()).hexdigest()


def pin_ok(u, pin: str | None) -> bool:
    """True, gdy PIN nie jest ustawiony albo się zgadza. Po 5 błędach – blokada na 15 min."""
    if not u["pin_hash"]:
        return True
    uid = u["tg_id"]
    fails = [t for t in _PIN_FAILS.get(uid, []) if time.time() - t < 900]
    if len(fails) >= 5:
        return False
    salt, h = u["pin_hash"].split("$", 1)
    if pin and hashlib.sha256((salt + str(pin)).encode()).hexdigest() == h:
        _PIN_FAILS.pop(uid, None)
        return True
    _PIN_FAILS[uid] = fails + [time.time()]
    return False


def pin_locked(uid: int) -> bool:
    return len([t for t in _PIN_FAILS.get(uid, []) if time.time() - t < 900]) >= 5


def unlocked(u) -> bool:
    return not u["pin_hash"] or time.time() < _UNLOCKED.get(u["tg_id"], 0)


async def cmd_pin(update: Update, ctx):
    """/pin 1234 – odblokowuje wypłatę i eksport klucza na 5 min. /ustawpin NOWY [STARY] – ustawia PIN."""
    if not (u := await require_accepted(update, ctx)):
        return
    try:
        await update.message.delete()  # PIN nie zostaje w czacie
    except Exception:  # noqa: BLE001
        pass
    pin = (ctx.args or [""])[0]
    if not u["pin_hash"]:
        return await update.effective_chat.send_message("Nie masz PIN-u. Ustaw: /ustawpin 1234 (4–8 cyfr).")
    if pin_ok(u, pin):
        _UNLOCKED[u["tg_id"]] = time.time() + 300
        return await update.effective_chat.send_message("🔓 Odblokowane na 5 min (wypłata, eksport klucza).")
    await update.effective_chat.send_message("❌ Zły PIN." + (" Za dużo prób – spróbuj za 15 min."
                                                              if pin_locked(u["tg_id"]) else ""))


async def cmd_setpin(update: Update, ctx):
    s = S(ctx)
    if not (u := await require_accepted(update, ctx)):
        return
    try:
        await update.message.delete()
    except Exception:  # noqa: BLE001
        pass
    args = ctx.args or []
    new = args[0] if args else ""
    old = args[1] if len(args) > 1 else None
    if u["pin_hash"] and not pin_ok(u, old):
        return await update.effective_chat.send_message("❌ Podaj obecny PIN: /ustawpin NOWY STARY")
    if new in ("-", "usun", "usuń"):
        s.db.update_user(u["tg_id"], pin_hash=None)
        return await update.effective_chat.send_message("PIN usunięty.")
    if not (new.isdigit() and 4 <= len(new) <= 8):
        return await update.effective_chat.send_message("PIN to 4–8 cyfr, np. /ustawpin 4821")
    s.db.update_user(u["tg_id"], pin_hash=hash_pin(new))
    await update.effective_chat.send_message("🔐 PIN ustawiony. Wypłata i eksport klucza będą wymagały PIN-u "
                                             "(w czacie: /pin 1234 odblokowuje na 5 min).")


def register(app: Application):
    app.add_handler(CommandHandler("raporty", cmd_reports))
    app.add_handler(CallbackQueryHandler(on_report_button, pattern=r"^(rp:(\d+|list)|rpd:\d{4}-\d{2}-\d{2})$"))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("eksport", cmd_export))
    app.add_handler(CommandHandler("pin", cmd_pin))
    app.add_handler(CommandHandler("ustawpin", cmd_setpin))
