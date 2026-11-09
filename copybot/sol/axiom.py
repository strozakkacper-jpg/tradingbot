"""Pulpit główny w stylu Axiom: kopiowanie portfeli Solany na pierwszym planie.

- pulpit: cena SOL, saldo, pozycje, wynik, kopiowane portfele, ostatnie ruchy
- karta portfela: wyniki, kopiuj / tylko powiadomienia / usuń, osobna kwota zakupu dla tego portfela
- ruchy na żywo, pozycje z PnL, karta tokena po wklejeniu adresu (CA) z szybkim kupnem/sprzedażą
- automatyczne take-profit / stop-loss pozycji
"""
import asyncio
import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton as B
from telegram import InlineKeyboardMarkup as KB
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ConversationHandler, MessageHandler, filters

from ..bot import S, UI_TIMEOUT, admin_ids, header, require_accepted, show
from ..db import now_ms
from .engine import Skip, fmt_tokens, short
from .parser import LAMPORTS, SOL_MINT
from .ui import valid_address

log = logging.getLogger(__name__)
MAIN = [B("« Pulpit", callback_data="home")]
FOLLOW_AMOUNTS = (0.05, 0.1, 0.25, 0.5, 1.0)
TPS = (0, 50, 100, 200, 500)
SLS = (0, 20, 30, 50)
ADD_ADDR, BUY_X = 70, 71


async def _ack(update: Update, text=None, alert=False):
    if update.callback_query:
        try:
            await update.callback_query.answer(text, show_alert=alert)
        except BadRequest:
            pass


def usd(x: float | None) -> str:
    if x is None:
        return "—"
    if x >= 1_000_000_000:
        return f"${x / 1e9:.2f}B"
    if x >= 1_000_000:
        return f"${x / 1e6:.2f}M"
    if x >= 1_000:
        return f"${x / 1e3:.1f}K"
    if x >= 1:
        return f"${x:.2f}"
    from ..engine import fmt_price
    return f"${fmt_price(x)}"


def pct(x: float | None) -> str:
    if x is None:
        return "—"
    if abs(x) < 0.05:
        return "0.0%"
    return f"{'🟢' if x >= 0 else '🔴'}{x:+.1f}%"


def tp_txt(u) -> str:
    return f"+{u['sol_tp']:g}%" if u["sol_tp"] else "wył."


def sl_txt(u) -> str:
    return f"−{u['sol_sl']:g}%" if u["sol_sl"] else "wył."


def hhmm(s, ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, ZoneInfo(s.cfg.timezone)).strftime("%H:%M")


def sol_leaders(s, uid: int) -> list[tuple]:
    """[(trader, kopiuje?), …] – śledzone portfele Solany użytkownika."""
    follows = s.db.follows_of(uid)
    out = []
    for lid, copy in follows.items():
        lead = s.db.get_leader(lid)
        if lead and lead["source"] == "sol":
            out.append((lead, copy))
    return out


async def portfolio(s, u) -> dict:
    """Saldo SOL, pozycje z wartością i PnL – jednym zapytaniem o ceny."""
    se = s.sol
    lam = await se.sol_lamports(u, max_age=5)
    hold = await se.holdings(u)
    values = await se.values_sol(hold)
    try:
        sol_usd = (await se.jup.prices([SOL_MINT])).get(SOL_MINT)
    except Exception:  # noqa: BLE001
        sol_usd = None
    positions = []
    for mint, raw in hold.items():
        sym, dec = se.cached_token(mint) or await se.token_info(mint)
        val = values.get(mint)
        basis = se.cost_basis(u["tg_id"], mint, not se.is_live(u))
        pnl = (val / basis[0] - 1) * 100 if (val is not None and basis and basis[0] > 0) else None
        positions.append({"mint": mint, "symbol": sym, "tokens": raw / 10 ** dec, "value": val, "pnl": pnl,
                          "cost": basis[0] if basis else None})
    positions.sort(key=lambda p: -(p["value"] or 0))
    total = lam / LAMPORTS + sum(p["value"] or 0 for p in positions)
    return {"sol": lam / LAMPORTS, "positions": positions, "total": total, "sol_usd": sol_usd}


# ---------------- widoki ----------------

async def home_view(s, u, notice: str | None = None) -> tuple[str, KB]:
    se = s.sol
    uid = u["tg_id"]
    se.wallet(uid)
    live = se.is_live(u)
    pf = await portfolio(s, u)
    try:
        px = await se.jup.token_full(SOL_MINT)
        sol_ch = (px or {}).get("stats24h", {}).get("priceChange")
    except Exception:  # noqa: BLE001
        sol_ch = None
    lines = ([f"ℹ️ {html.escape(notice)}", ""] if notice else []) + [
        f"🟣 <b>Copy Trading · Solana</b> · {'💰 LIVE' if live else '🧪 demo'}",
        f"SOL {usd(pf['sol_usd'])} {pct(sol_ch)}" if pf["sol_usd"] else "SOL —",
        f"👛 <b>{pf['sol']:.4f} SOL</b>" + (f" (≈ {usd(pf['sol'] * pf['sol_usd'])})" if pf["sol_usd"] else "")
        + f" · pozycje {len(pf['positions'])} ≈ {pf['total'] - pf['sol']:.3f} SOL",
    ]
    if not live:
        lines.append(f"Wynik demo: {pct((pf['total'] / s.cfg.sol_paper_start - 1) * 100)} "
                     f"(razem {pf['total']:.4f} SOL)")
    last = s.db._q("SELECT latency_ms FROM sol_trades WHERE tg_id = ? AND latency_ms IS NOT NULL ORDER BY id DESC "
                   "LIMIT 1", (uid,))
    speed = f" · ⚡ ostatnio {last[0]['latency_ms']} ms" if last else ""
    lines.append(("▶️ <b>Kopiowanie włączone</b>" if u["active"] else "⏸ Kopiowanie wyłączone") + speed)
    if s.db.get_state("paused") == "1":
        lines.append("⏸ <i>wstrzymane przez admina</i>")

    wallets = sol_leaders(s, uid)
    lines += ["", f"👥 <b>Kopiowane portfele</b> ({len(wallets)})"]
    day_ago = now_ms() - 24 * 3600 * 1000
    rows = []
    for lead, copy in wallets[:8]:
        n = s.db._q("SELECT COUNT(*) n FROM sol_trades WHERE tg_id = ? AND leader_id = ? AND status = 'filled' "
                    "AND ts >= ?", (uid, lead["id"], day_ago))[0]["n"]
        try:
            rep = await asyncio.wait_for(se.report(lead["id"]), 5)
            res = pct(rep["total"] * 100)
        except Exception:  # noqa: BLE001
            res = "—"
        icon = "✅" if copy else "👁"
        lines.append(f"{icon} <b>{html.escape(lead['name'])}</b> · {short(lead['wallet'] or '')} · {res} · "
                     f"kopii 24h: {n}")
        rows.append([B(f"{icon} {lead['name'][:30]}", callback_data=f"aw:{lead['id']}")])
    if not wallets:
        lines.append("Nie kopiujesz jeszcze żadnego portfela.\n👉 Kliknij <b>➕ Dodaj portfel</b> albo po prostu wklej "
                     "tu adres portfela tradera (np. z Axiom / GMGN).")

    feed = se.history(uid, 4)
    if feed:
        lines += ["", "📡 <b>Ostatnie ruchy</b>"]
        for r in feed:
            lines.append(_feed_line(s, r))
    lines += ["", "<i>Wklej adres portfela – zaczniesz go kopiować. Wklej adres tokena (CA) – kupisz go ręcznie.</i>"]

    kb = [[B("⏸ Wyłącz kopiowanie" if u["active"] else "▶️ Włącz kopiowanie", callback_data="ax:tog")],
          [B("➕ Dodaj portfel", callback_data="ax:add")]] + rows + [
          [B("💼 Pozycje", callback_data="ax:pos"), B("📡 Ruchy", callback_data="ax:feed")],
          [B("📊 Raport sesji", callback_data="ax:rep")],
          [B("⚙️ Ustawienia", callback_data="ax:set"), B("🔥 Trendy", callback_data="ax:trend")],
          [B("👛 Portfel / wpłata", callback_data="sx:wallet"),
           B("Przejdź na 💰 LIVE" if not live else "Przejdź na 🧪 demo", callback_data="sx:mode")],
          [B("🏦 Giełdy (CEX) →", callback_data="menu")] + ([B("🛠 Admin", callback_data="admin")]
                                                          if uid in admin_ids(s) else []),
          [B("🔄 Odśwież", callback_data="home")]]
    return "\n".join(lines), KB(kb)


def _feed_line(s, r) -> str:
    sym = html.escape(r["symbol"] or short(r["mint"]))
    t = hhmm(s, r["ts"])
    src = html.escape(r["info"] or "")
    if r["status"] == "filled":
        lat = f" ⚡{r['latency_ms']}ms" if r["latency_ms"] is not None else ""
        verb = "kupno" if r["side"] == "buy" else "sprzedaż"
        return f"{'🟢' if r['side'] == 'buy' else '🔴'} {t} {verb} <b>{sym}</b> {r['sol']:.4f} SOL · {src}{lat}"
    return f"⚠️ {t} {sym}: <i>{html.escape((r['info'] or r['status'])[:60])}</i>"


async def wallet_card(s, u, lid: int, notice: str | None = None) -> tuple[str, KB]:
    lead = s.db.get_leader(lid)
    if not lead or lead["status"] != "active":
        return "Ten portfel nie jest już śledzony.", KB([MAIN])
    uid = u["tg_id"]
    fr = s.db.follow_row(uid, lid)
    status = None if not fr else bool(fr["copy"])
    lines = ([f"ℹ️ {html.escape(notice)}", ""] if notice else []) + [
        f"👤 <b>{html.escape(lead['name'])}</b>", f"<code>{lead['wallet']}</code>",
        f'<a href="https://solscan.io/account/{lead["wallet"]}">Solscan</a> · '
        f'<a href="https://gmgn.ai/sol/address/{lead["wallet"]}">GMGN</a>', ""]
    try:
        rep = await asyncio.wait_for(s.sol.report(lid), 8)
        wr = f"{rep['win_rate'] * 100:.0f}%" if rep["win_rate"] is not None else "—"
        lines += [f"📊 Portfel wzorcowy (każdy ruch 1:1 od {hhmm(s, rep['since'])}): <b>{pct(rep['total'] * 100)}</b>",
                  f"7 dni {pct(rep['d7'] * 100 if rep['d7'] is not None else None)} · trafione {wr} "
                  f"({rep['wins']}/{rep['closed']}) · spadek {pct(-rep['max_dd'] * 100)}"]
    except Exception:  # noqa: BLE001
        lines.append("📊 Wyniki chwilowo niedostępne")
    mine = s.db._q("""SELECT side, COUNT(*) n, SUM(sol) sol FROM sol_trades WHERE tg_id = ? AND leader_id = ?
                      AND status = 'filled' GROUP BY side""", (uid, lid))
    d = {r["side"]: (r["n"], r["sol"] or 0) for r in mine}
    lines.append(f"🔁 Twoje kopie: kupna {d.get('buy', (0, 0))[0]} ({d.get('buy', (0, 0))[1]:.3f} SOL) · "
                 f"sprzedaże {d.get('sell', (0, 0))[0]} ({d.get('sell', (0, 0))[1]:.3f} SOL)")
    amount = fr["sol_buy"] if fr and fr["sol_buy"] else None
    lines.append(f"💵 Kwota zakupu dla tego portfela: <b>{f'{amount:g} SOL' if amount else 'domyślna z ustawień'}</b>")
    lines.append({True: "\n✅ <b>Kopiujesz</b> ten portfel.", False: "\n👁 Tylko powiadomienia.",
                  None: "\n▫️ Nie śledzisz."}[status])

    def mark(v):
        return f"✓ {v:g}" if amount and abs(amount - v) < 1e-9 else f"{v:g}"
    kb = []
    if status is True:
        kb.append([B("👁 Tylko powiadomienia", callback_data=f"wt:{lid}"), B("🔕 Usuń", callback_data=f"uf:{lid}")])
    elif status is False:
        kb.append([B("✅ Kopiuj", callback_data=f"cp:{lid}"), B("🔕 Usuń", callback_data=f"uf:{lid}")])
    else:
        kb.append([B("✅ Kopiuj", callback_data=f"cp:{lid}"), B("👁 Obserwuj", callback_data=f"wt:{lid}")])
    kb += [header("Kwota zakupu (SOL)"),
           [B("✓ domyślna" if not amount else "domyślna", callback_data=f"aa:{lid}:0")]
           + [B(mark(v), callback_data=f"aa:{lid}:{v}") for v in FOLLOW_AMOUNTS],
           [B("✏️ Zmień nazwę", callback_data=f"ren:{lid}"), B("🔄 Odśwież", callback_data=f"aw:{lid}")], MAIN]
    return "\n".join(lines), KB(kb)


async def positions_view(s, u) -> tuple[str, KB]:
    pf = await portfolio(s, u)
    lines = [f"💼 <b>Pozycje</b> · {'💰 LIVE' if s.sol.is_live(u) else '🧪 demo'}", ""]
    kb = []
    for p in pf["positions"][:12]:
        val = f"{p['value']:.4f} SOL" if p["value"] is not None else "brak ceny"
        lines.append(f"• <b>{html.escape(p['symbol'])}</b> {fmt_tokens(p['tokens'])} ≈ {val} · PnL {pct(p['pnl'])}")
        kb.append([B(f"{p['symbol'][:12]} {pct(p['pnl'])}", callback_data=f"tk:{p['mint']}"),
                   B("🔴 100%", callback_data=f"ts:100:{p['mint']}")])
    if not pf["positions"]:
        lines.append("Brak pozycji – pojawią się, gdy bot skopiuje zakup.")
    lines += ["", f"SOL: {pf['sol']:.4f} · razem ≈ <b>{pf['total']:.4f} SOL</b>"]
    if u["sol_tp"] or u["sol_sl"]:
        lines.append(f"Auto: take-profit {tp_txt(u)} · stop-loss {sl_txt(u)}")
    kb += [[B("🔄 Odśwież", callback_data="ax:pos")], MAIN]
    return "\n".join(lines), KB(kb)


def feed_view(s, u) -> tuple[str, KB]:
    rows = s.sol.history(u["tg_id"], 15)
    lines = ["📡 <b>Ruchy</b> (Twoje kopie i transakcje)", ""]
    lines += [_feed_line(s, r) for r in rows] or ["Jeszcze nic."]
    return "\n".join(lines), KB([[B("🔄 Odśwież", callback_data="ax:feed")], MAIN])


def settings_view(s, u) -> tuple[str, KB]:
    from .ui import settings_view as base
    text, kb = base(u)
    text += (f"\n\n🎯 <b>Auto take-profit</b>: {tp_txt(u)} · <b>Auto stop-loss</b>: {sl_txt(u)}\n"
             "Bot co minutę sprawdza Twoje pozycje i sprzedaje całość, gdy zysk lub strata przekroczy próg.")
    rows = list(kb.inline_keyboard[:-1])
    rows += [header("Auto take-profit"),
             [B(("✓ " if abs(u["sol_tp"] - v) < 1e-9 else "") + (f"+{v}%" if v else "wył"), callback_data=f"ax:tp:{v}")
              for v in TPS],
             header("Auto stop-loss"),
             [B(("✓ " if abs(u["sol_sl"] - v) < 1e-9 else "") + (f"−{v}%" if v else "wył"), callback_data=f"ax:sl:{v}")
              for v in SLS], MAIN]
    return text, KB(rows)


async def token_card(s, u, mint: str, notice: str | None = None) -> tuple[str, KB]:
    se = s.sol
    info = await se.jup.token_full(mint)
    if not info:
        return f"❌ Nie znalazłem tokena <code>{html.escape(mint)}</code>.", KB([MAIN])
    held = await se.token_raw(u, mint)
    dec = int(info.get("decimals") or 0)
    se.db._q("INSERT OR REPLACE INTO sol_tokens VALUES (?, ?, ?)", (mint, info.get("symbol") or short(mint), dec))
    a = info.get("audit") or {}
    st = {k: (info.get(f"stats{k}") or {}).get("priceChange") for k in ("5m", "1h", "24h")}
    flags = []
    if a.get("mintAuthorityDisabled") is not None:
        flags.append("✅ mint wył." if a["mintAuthorityDisabled"] else "⚠️ można dodrukować")
    if a.get("freezeAuthorityDisabled") is not None:
        flags.append("✅ freeze wył." if a["freezeAuthorityDisabled"] else "⚠️ można zamrozić")
    if a.get("topHoldersPercentage") is not None:
        flags.append(f"top10 {a['topHoldersPercentage']:.0f}%")
    holders = f"{info['holderCount']:,}".replace(",", " ") if info.get("holderCount") else "—"
    lines = ([f"ℹ️ {html.escape(notice)}", ""] if notice else []) + [
        f"🪙 <b>{html.escape(info.get('symbol') or '?')}</b> · {html.escape(info.get('name') or '')}",
        f"<code>{mint}</code>",
        f"💲 {usd(info.get('usdPrice'))} · MC {usd(info.get('mcap'))} · płynność {usd(info.get('liquidity'))}",
        f"📈 5m {pct(st['5m'])} · 1h {pct(st['1h'])} · 24h {pct(st['24h'])}",
        f"👥 holderzy {holders}" + (f" · {' · '.join(flags)}" if flags else ""),
    ]
    if held > 0:
        vals = await se.values_sol({mint: held})
        basis = se.cost_basis(u["tg_id"], mint, not se.is_live(u))
        v = vals.get(mint)
        pnl = (v / basis[0] - 1) * 100 if (v and basis and basis[0] > 0) else None
        lines.append(f"\n💼 Masz {fmt_tokens(held / 10 ** dec)} ≈ {v:.4f} SOL · PnL {pct(pnl)}" if v is not None else
                     f"\n💼 Masz {fmt_tokens(held / 10 ** dec)}")
    lines.append(f"\n{'💰 LIVE' if se.is_live(u) else '🧪 demo'} · poślizg {u['sol_slippage_bps'] / 100:g}%")
    presets = [float(x) for x in (u["sol_presets"] or "0.05,0.1,0.5,1").split(",") if x.strip()][:4]
    kb = [[B(f"🟢 {p:g} SOL", callback_data=f"tb:{p:g}:{mint}") for p in presets] + [B("🟢 X", callback_data=f"tbx:{mint}")]]
    if held > 0:
        kb.append([B(f"🔴 {p}%", callback_data=f"ts:{p}:{mint}") for p in (25, 50, 100)])
    kb += [[B("🔄 Odśwież", callback_data=f"tk:{mint}"),
            B("📈 Wykres", url=f"https://dexscreener.com/solana/{mint}")], MAIN]
    return "\n".join(lines), KB(kb)


async def trend_view(s, u) -> tuple[str, KB]:
    items = await s.sol.jup.trending("1h", 10)
    lines = ["🔥 <b>Trendy (1 h)</b>", ""]
    kb, row = [], []
    for i, t in enumerate(items[:10], 1):
        ch = (t.get("stats1h") or {}).get("priceChange")
        lines.append(f"{i}. <b>{html.escape(t.get('symbol') or '?')}</b> · MC {usd(t.get('mcap'))} · 1h {pct(ch)}")
        row.append(B(t.get("symbol", "?")[:10], callback_data=f"tk:{t['id']}"))
        if len(row) == 3:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    if not items:
        lines.append("Brak danych.")
    kb.append(MAIN)
    return "\n".join(lines), KB(kb)


# ---------------- akcje ----------------

async def buy(s, u, mint: str, sol: float) -> str:
    se = s.sol
    fill = await se.execute(u, "buy", mint, int(sol * LAMPORTS))
    se.record(u["tg_id"], None, None, "buy", mint, fill.sol, fill.tokens, "filled", "ręcznie", not se.is_live(u),
              fill.sig, fill.latency_ms)
    sym, _ = se.cached_token(mint) or await se.token_info(mint)
    return f"✅ Kupiono {fmt_tokens(fill.tokens)} {sym} za {fill.sol:.4f} SOL"


async def on_button(update: Update, ctx):
    if not (u := await require_accepted(update, ctx)):
        return await _ack(update)
    s = S(ctx)
    if not getattr(s, "sol", None):
        return await _ack(update, "Solana jest wyłączona", True)
    data = update.callback_query.data
    uid = u["tg_id"]
    p = data.split(":")
    try:
        if data == "home":
            await _ack(update)
            return await show(update, *await asyncio.wait_for(home_view(s, u), UI_TIMEOUT))
        if data == "ax:tog":
            from . import session
            if u["active"]:
                s.db.update_user(uid, active=0)
                await _ack(update, "Kopiowanie wyłączone – wysyłam raport sesji 📊")
                await session.end(s, s.db.get_user(uid), ctx.bot, "kopiowanie wyłączone")
            elif not any(c for _, c in sol_leaders(s, uid)) and not s.db.copied_leaders(uid):
                return await _ack(update, "Najpierw dodaj portfel i kliknij ✅ Kopiuj", True)
            else:
                await _ack(update, "Kopiowanie włączone ▶️")
                s.db.update_user(uid, active=1)
                await session.start(s, s.db.get_user(uid))
            return await show(update, *await home_view(s, s.db.get_user(uid)))
        if data == "ax:rep":
            from . import session
            await _ack(update, "⏳")
            text = await asyncio.wait_for(session.build(s, u), UI_TIMEOUT)
            return await show(update, text, KB([[B("🔄 Odśwież", callback_data="ax:rep")], MAIN]))
        if data == "ax:pos":
            await _ack(update, "⏳")
            return await show(update, *await asyncio.wait_for(positions_view(s, u), UI_TIMEOUT))
        if data == "ax:feed":
            await _ack(update)
            return await show(update, *feed_view(s, u))
        if data == "ax:set":
            await _ack(update)
            return await show(update, *settings_view(s, u))
        if p[0] == "ax" and p[1] in ("tp", "sl"):
            s.db.update_user(uid, **{f"sol_{p[1]}": float(p[2])})
            await _ack(update, "Zapisano")
            return await show(update, *settings_view(s, s.db.get_user(uid)))
        if data == "ax:trend":
            await _ack(update, "⏳")
            return await show(update, *await asyncio.wait_for(trend_view(s, u), UI_TIMEOUT))
        if p[0] == "aw":
            await _ack(update)
            return await show(update, *await asyncio.wait_for(wallet_card(s, u, int(p[1])), UI_TIMEOUT))
        if p[0] == "aa":
            lid, amt = int(p[1]), float(p[2])
            if not s.db.follow_row(uid, lid):
                s.db.follow(uid, lid, False)
            s.db.set_follow_amount(uid, lid, amt or None)
            await _ack(update, f"Kwota: {amt:g} SOL" if amt else "Kwota domyślna")
            return await show(update, *await wallet_card(s, s.db.get_user(uid), lid))
        if p[0] == "aq":  # szybkie śledzenie wklejonego portfela
            mode, addr = p[1], p[2]
            lid = _ensure_leader(s, uid, addr)
            s.db.follow(uid, lid, mode == "copy")
            s.sol.wallet(uid)
            msg = "✅ Kopiujesz ten portfel" + ("" if u["active"] else " – włącz kopiowanie na pulpicie ▶️")
            await _ack(update, msg[:190], True)
            return await show(update, *await wallet_card(s, s.db.get_user(uid), lid, msg if mode == "copy" else None))
        if p[0] == "tk":
            await _ack(update, "⏳")
            return await show(update, *await asyncio.wait_for(token_card(s, u, p[1]), UI_TIMEOUT))
        if p[0] == "tb":
            await _ack(update, "Kupuję…")
            try:
                msg = await buy(s, u, p[2], float(p[1]))
            except Skip as e:
                msg = f"⚠️ {e}"
            return await show(update, *await token_card(s, s.db.get_user(uid), p[2], msg))
        if p[0] == "ts":
            await _ack(update, "Sprzedaję…")
            try:
                fill = await s.sol.sell_manual(u, p[2], int(p[1]))
                msg = f"✅ Sprzedano za {fill.sol:.4f} SOL"
            except Skip as e:
                msg = f"⚠️ {e}"
            return await show(update, *await token_card(s, s.db.get_user(uid), p[2], msg))
    except Exception as e:  # noqa: BLE001
        log.exception("Pulpit Solany")
        return await show(update, f"❌ {html.escape(str(e)[:200]) or 'przekroczony czas'}", KB([MAIN]))


def _ensure_leader(s, uid: int, addr: str) -> int:
    rows = s.db._q("SELECT id FROM leaders WHERE source = 'sol' AND status = 'active' AND wallet = ?", (addr,))
    if rows:
        return rows[0]["id"]
    lid = s.db.create_leader(f"Portfel {short(addr)}", "sol", uid, "active", wallet=addr)
    return lid


# ---------------- wklejanie adresu (portfel albo token) ----------------

async def on_paste(update: Update, ctx):
    s = S(ctx)
    if not getattr(s, "sol", None):
        return
    addr = valid_address(update.message.text or "")
    if not addr:
        if await require_accepted(update, ctx):
            await update.message.reply_text("Wklej adres portfela (żeby go kopiować) albo tokena (żeby go kupić). "
                                            "Pulpit: /menu")
        return
    if not (u := await require_accepted(update, ctx)):
        return
    info = None
    try:
        info = await asyncio.wait_for(s.sol.jup.token_full(addr), 6)
    except Exception:  # noqa: BLE001
        pass
    if info:  # to token
        text, kb = await token_card(s, u, addr)
        return await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                                        disable_web_page_preview=True)
    preview = ""
    try:
        lam = await asyncio.wait_for(s.sol.rpc.balance(addr), 6)
        preview = f"\nSaldo: {lam / LAMPORTS:.3f} SOL"
    except Exception:  # noqa: BLE001
        pass
    await update.effective_chat.send_message(
        f"👤 Portfel <code>{addr}</code>{preview}\n"
        f'<a href="https://gmgn.ai/sol/address/{addr}">Zobacz wyniki na GMGN</a>\n\nCo robimy?',
        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        reply_markup=KB([[B("✅ Kopiuj jego transakcje", callback_data=f"aq:copy:{addr}")],
                         [B("👁 Tylko powiadomienia", callback_data=f"aq:watch:{addr}")], MAIN]))


# ---------------- rozmowy: dodaj portfel, kup za X SOL ----------------

async def add_start(update: Update, ctx):
    await _ack(update)
    if not await require_accepted(update, ctx):
        return ConversationHandler.END
    await update.effective_chat.send_message(
        "➕ <b>Dodaj portfel do kopiowania</b>\n\nWklej adres portfela tradera (z Axiom, GMGN, Solscan, kolscan.io…).\n"
        "Możesz dopisać nazwę po spacji, np. <code>7xKX…AsU Kowal</code>\n/anuluj – przerwij", parse_mode=ParseMode.HTML)
    return ADD_ADDR


async def add_addr(update: Update, ctx):
    s = S(ctx)
    parts = (update.message.text or "").split(maxsplit=1)
    addr = valid_address(parts[0]) if parts else None
    if not addr:
        await update.message.reply_text("To nie jest adres Solany. Wklej sam adres portfela (32–44 znaki).")
        return ADD_ADDR
    uid = update.effective_user.id
    lid = _ensure_leader(s, uid, addr)
    if len(parts) > 1:
        s.db.update_leader(lid, name=parts[1].strip()[:40])
    s.db.follow(uid, lid, True)
    s.sol.wallet(uid)
    u = s.db.get_user(uid)
    text, kb = await wallet_card(s, u, lid, "Dodano – kopiujesz ten portfel" +
                                 ("" if u["active"] else ". Włącz kopiowanie na pulpicie ▶️"))
    await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                             disable_web_page_preview=True)
    return ConversationHandler.END


async def buyx_start(update: Update, ctx):
    await _ack(update)
    ctx.user_data["buyx"] = update.callback_query.data.split(":")[1]
    await update.effective_chat.send_message("Za ile SOL kupić? (np. 0.3)\n/anuluj – przerwij")
    return BUY_X


async def buyx_amount(update: Update, ctx):
    s = S(ctx)
    mint = ctx.user_data.pop("buyx", None)
    try:
        sol = float((update.message.text or "").replace(",", ".").split()[0])
        assert 0 < sol <= 1000
    except (ValueError, IndexError, AssertionError):
        await update.message.reply_text("Nieprawidłowa kwota.")
        return ConversationHandler.END
    u = s.db.get_user(update.effective_user.id)
    try:
        msg = await buy(s, u, mint, sol)
    except Skip as e:
        msg = f"⚠️ {e}"
    text, kb = await token_card(s, s.db.get_user(u["tg_id"]), mint, msg)
    await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                             disable_web_page_preview=True)
    return ConversationHandler.END


async def conv_cancel(update: Update, ctx):
    ctx.user_data.pop("buyx", None)
    await update.message.reply_text("Anulowano. /menu – pulpit")
    return ConversationHandler.END


# ---------------- auto take-profit / stop-loss ----------------

async def tpsl_job(ctx):
    s = ctx.application.bot_data["svc"]
    if not getattr(s, "sol", None):
        return
    users = s.db._q("SELECT u.* FROM users u JOIN sol_wallets w ON w.tg_id = u.tg_id "
                    "WHERE u.approved = 1 AND (u.sol_tp > 0 OR u.sol_sl > 0)")
    for u in users:
        try:
            pf = await portfolio(s, u)
        except Exception:  # noqa: BLE001
            continue
        for pos in pf["positions"]:
            pnl = pos["pnl"]
            if pnl is None:
                continue
            hit = ("🎯 Take-profit" if u["sol_tp"] and pnl >= u["sol_tp"] else
                   "🛑 Stop-loss" if u["sol_sl"] and pnl <= -u["sol_sl"] else None)
            if not hit:
                continue
            try:
                fill = await s.sol.sell_manual(u, pos["mint"], 100)
                await s.sol.notify(u["tg_id"], f"{hit} {pos['symbol']}: PnL {pnl:+.1f}% → sprzedano całość za "
                                               f"{fill.sol:.4f} SOL")
            except Exception as e:  # noqa: BLE001
                log.warning("TP/SL %s %s: %s", u["tg_id"], pos["symbol"], e)


async def cmd_report(update: Update, ctx):
    if u := await require_accepted(update, ctx):
        from . import session
        text = await session.build(S(ctx), u)
        await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                                 reply_markup=KB([[B("🔄 Odśwież", callback_data="ax:rep")], MAIN]))


def register(app: Application):
    """Przyciski i rozmowy pulpitu – rejestrować PRZED bot.register."""
    app.add_handler(CommandHandler("raport", cmd_report))
    text = filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE
    cancel = [CommandHandler("anuluj", conv_cancel)]
    app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(add_start, pattern="^ax:add$"),
                                                      CommandHandler("portfel", add_start)],
                                        states={ADD_ADDR: [MessageHandler(text, add_addr)]},
                                        fallbacks=cancel, allow_reentry=True, conversation_timeout=300))
    app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(buyx_start, pattern=r"^tbx:")],
                                        states={BUY_X: [MessageHandler(text, buyx_amount)]},
                                        fallbacks=cancel, allow_reentry=True, conversation_timeout=120))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(home|ax:.*|aw:\d+|aa:\d+:.*|aq:.*|tk:.*|tb:.*|ts:.*)$"))


def register_paste(app: Application):
    """Wklejony adres – rejestrować NA KOŃCU (po wszystkich rozmowach), żeby nie przechwytywać ich odpowiedzi."""
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_paste))
