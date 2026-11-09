"""Panel administratora: podgląd każdego, kto handluje (saldo, wynik, pozycje, ruchy) i rangi."""
import asyncio
import html
import logging

from telegram import InlineKeyboardButton as B
from telegram import InlineKeyboardMarkup as KB
from telegram import Update
from telegram.ext import Application, CallbackQueryHandler

from ..bot import S, UI_TIMEOUT, admin_ids, owner_ids, show
from ..db import now_ms
from .engine import fmt_tokens, short

log = logging.getLogger(__name__)
DAY_MS = 24 * 3600 * 1000


def display_name(u) -> str:
    return f"@{u['username']}" if u["username"] else str(u["tg_id"])


def role_of(s, uid: int) -> str:
    return "owner" if uid in owner_ids(s) else "admin" if uid in admin_ids(s) else "user"


ROLE_TXT = {"owner": "👑 właściciel", "admin": "⭐ admin", "user": "👤 użytkownik"}


async def user_summary(s, u) -> dict:
    """Najważniejsze liczby jednej osoby (Solana)."""
    from .axiom import portfolio, sol_leaders
    se = s.sol
    uid = u["tg_id"]
    live = se.is_live(u)
    out = {"id": uid, "name": display_name(u), "role": role_of(s, uid), "active": bool(u["active"]), "live": live,
           "has_wallet": se.has_wallet(uid), "total": None, "sol": None, "positions": 0, "result": None,
           "sol_usd": None}
    out["wallets"] = [{"id": ld["id"], "name": ld["name"], "copy": c} for ld, c in sol_leaders(s, uid)]
    if out["has_wallet"]:
        try:
            pf = await asyncio.wait_for(portfolio(s, u), 10)
            out.update(total=pf["total"], sol=pf["sol"], positions=len(pf["positions"]), sol_usd=pf["sol_usd"])
            start = s.cfg.sol_paper_start if not live else u["session_sol0"]
            if start:
                out["result"] = (pf["total"] / start - 1) * 100
        except Exception:  # noqa: BLE001
            log.debug("Podgląd %s", uid, exc_info=True)
        out["today_pnl"] = se.day_pnl(uid, not live)
        day = s.db._q("SELECT COUNT(*) n, MAX(ts) last FROM sol_trades WHERE tg_id = ? AND status = 'filled' "
                      "AND ts >= ?", (uid, now_ms() - DAY_MS))[0]
        out["trades24"], out["last_ts"] = day["n"], day["last"]
    return out


async def overview(s) -> list[dict]:
    users = s.db.approved_users()
    res = await asyncio.gather(*(user_summary(s, u) for u in users), return_exceptions=True)
    return [r for r in res if isinstance(r, dict)]


async def user_detail(s, uid: int) -> dict | None:
    from .axiom import portfolio
    from .ui import buy_txt
    u = s.db.get_user(uid)
    if not u:
        return None
    d = await user_summary(s, u)
    d["settings"] = buy_txt(u)
    d["positions_list"] = []
    if d["has_wallet"]:
        try:
            pf = await asyncio.wait_for(portfolio(s, u), 10)
            d["positions_list"] = [{"symbol": p["symbol"], "mint": p["mint"], "tokens": p["tokens"],
                                    "value": p["value"], "pnl": p["pnl"]} for p in pf["positions"][:15]]
        except Exception:  # noqa: BLE001
            pass
    d["history"] = [{"ts": r["ts"], "side": r["side"], "symbol": r["symbol"] or short(r["mint"]), "sol": r["sol"],
                     "status": r["status"], "info": r["info"]} for r in s.sol.history(uid, 15)]
    return d


# ---------------- widoki w bocie ----------------

def _pct(x):
    from .axiom import pct
    return pct(x)


async def users_view(s) -> tuple[str, KB]:
    rows = await overview(s)
    lines = ["👥 <b>Kto handluje</b> – podgląd wszystkich", ""]
    kb = []
    for d in rows:
        state = "▶️ kopiuje" if d["active"] else "⏸ stoi"
        mode = "💰" if d["live"] else "🧪"
        money = f"{d['total']:.3f} SOL" if d["total"] is not None else "—"
        lines.append(f"{mode} <b>{html.escape(d['name'])}</b> {ROLE_TXT[d['role']].split()[0]} · {state}")
        if d["has_wallet"]:
            lines.append(f"   {money} · wynik {_pct(d['result'])} · dziś {d['today_pnl']:+.3f} SOL · "
                         f"ruchy 24h: {d['trades24']} · traderzy: {sum(1 for w in d['wallets'] if w['copy'])}")
        else:
            lines.append("   jeszcze nie używa Solany")
        kb.append([B(f"🔍 {d['name'][:28]}", callback_data=f"adm:u:{d['id']}")])
    if not rows:
        lines.append("Nikt jeszcze nie ma dostępu.")
    lines += ["", "<i>🧪 ćwiczenia · 💰 prawdziwe SOL · 👑 właściciel · ⭐ admin</i>"]
    kb += [[B("🔄 Odśwież", callback_data="adm:users")], [B("« Admin", callback_data="admin")]]
    return "\n".join(lines), KB(kb)


async def user_view(s, viewer: int, uid: int) -> tuple[str, KB]:
    from .axiom import hhmm, in_usd, mode_label
    d = await user_detail(s, uid)
    if not d:
        return "Nie ma takiej osoby.", KB([[B("« Lista", callback_data="adm:users")]])
    lines = [f"🔍 <b>{html.escape(d['name'])}</b> · {ROLE_TXT[d['role']]}",
             f"{mode_label(d['live'])} · {'▶️ kopiuje' if d['active'] else '⏸ kopiowanie wyłączone'}", ""]
    if d["has_wallet"] and d["total"] is not None:
        lines += [f"💰 Razem: <b>{d['total']:.4f} SOL</b>{in_usd(d['total'], d['sol_usd'])} · wolne {d['sol']:.4f} SOL",
                  f"📈 Wynik: {_pct(d['result'])} · dziś {d['today_pnl']:+.4f} SOL · ruchy 24h: {d['trades24']}",
                  f"⚙️ {html.escape(d['settings'])}"]
    copied = [w["name"] for w in d["wallets"] if w["copy"]]
    watched = [w["name"] for w in d["wallets"] if not w["copy"]]
    lines.append(f"\n👥 Kopiuje: {html.escape(', '.join(copied)) if copied else '—'}"
                 + (f"\n👁 Obserwuje: {html.escape(', '.join(watched))}" if watched else ""))
    if d["positions_list"]:
        lines.append("\n🪙 <b>Tokeny</b>")
        for p in d["positions_list"][:10]:
            val = f"{p['value']:.4f} SOL" if p["value"] is not None else "brak ceny"
            lines.append(f"• {html.escape(p['symbol'])} {fmt_tokens(p['tokens'])} ≈ {val} · {_pct(p['pnl'])}")
    if d["history"]:
        lines.append("\n📜 <b>Ostatnie ruchy</b>")
        for h in d["history"][:10]:
            t = hhmm(s, h["ts"])
            if h["status"] == "filled":
                lines.append(f"{'🟢' if h['side'] == 'buy' else '🔴'} {t} {html.escape(h['symbol'])} "
                             f"{h['sol']:.4f} SOL · {html.escape(h['info'] or '')}")
            else:
                lines.append(f"⚠️ {t} {html.escape(h['symbol'])}: <i>{html.escape((h['info'] or '')[:50])}</i>")
    kb = []
    if d["active"]:
        kb.append([B("⏸ Zatrzymaj mu kopiowanie", callback_data=f"adm:stop:{uid}")])
    if viewer in owner_ids(s) and d["role"] != "owner":
        kb.append([B("⭐ Daj rangę admina" if d["role"] == "user" else "Zabierz rangę admina",
                     callback_data=f"adm:role:{uid}")])
    kb += [[B("🔄 Odśwież", callback_data=f"adm:u:{uid}"), B("« Lista", callback_data="adm:users")]]
    return "\n".join(lines), KB(kb)


# ---------------- akcje ----------------

async def stop_user(s, uid: int, bot, by: str) -> bool:
    from . import session
    u = s.db.get_user(uid)
    if not u or not u["active"]:
        return False
    s.db.update_user(uid, active=0)
    try:
        await bot.send_message(uid, f"⏸ Administrator ({by}) zatrzymał Twoje kopiowanie. Włączysz je ponownie na "
                                    "pulpicie ▶️.")
    except Exception:  # noqa: BLE001
        pass
    await session.end(s, s.db.get_user(uid), bot, "zatrzymane przez admina")
    return True


def toggle_role(s, viewer: int, uid: int) -> str | None:
    """Tylko właściciel nadaje i zabiera rangę admina. Zwraca nową rangę albo None."""
    if viewer not in owner_ids(s) or uid in owner_ids(s):
        return None
    u = s.db.get_user(uid)
    if not u or not u["approved"]:
        return None
    new = "user" if u["role"] == "admin" else "admin"
    s.db.update_user(uid, role=new)
    return new


async def on_button(update: Update, ctx):
    s = S(ctx)
    q = update.callback_query
    viewer = update.effective_user.id
    if viewer not in admin_ids(s) or not getattr(s, "sol", None):
        return await q.answer("Tylko dla administratora", show_alert=True)
    p = q.data.split(":")
    try:
        if p[1] == "users":
            await q.answer("⏳")
            return await show(update, *await asyncio.wait_for(users_view(s), UI_TIMEOUT))
        uid = int(p[2])
        if p[1] == "stop":
            ok = await stop_user(s, uid, ctx.bot, display_name(s.db.get_user(viewer)))
            await q.answer("Zatrzymano" if ok else "Już stoi")
        elif p[1] == "role":
            new = toggle_role(s, viewer, uid)
            await q.answer({"admin": "Dano rangę admina ⭐", "user": "Zabrano rangę admina"}.get(new, "Tylko właściciel"),
                           show_alert=new is None)
            if new == "admin":
                try:
                    await ctx.bot.send_message(uid, "⭐ Dostałeś rangę administratora – w /admin widzisz podgląd "
                                                    "wszystkich, którzy handlują.")
                except Exception:  # noqa: BLE001
                    pass
        else:
            await q.answer()
        return await show(update, *await asyncio.wait_for(user_view(s, viewer, uid), UI_TIMEOUT))
    except Exception as e:  # noqa: BLE001
        log.exception("Panel admina")
        return await show(update, f"❌ {html.escape(str(e)[:200]) or 'przekroczony czas'}",
                          KB([[B("« Admin", callback_data="admin")]]))


def register(app: Application):
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^adm:(users|u:\d+|stop:\d+|role:\d+)$"))
