"""Szczegółowy raport sesji kopiowania (od włączenia ▶️ do wyłączenia ⏸ / stop-lossa)."""
import asyncio
import html
import logging
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram.constants import ParseMode

from ..db import now_ms
from .engine import short

log = logging.getLogger(__name__)


def _dt(s, ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, ZoneInfo(s.cfg.timezone)).strftime("%d.%m %H:%M")


def _dur(ms: int) -> str:
    m = max(int(ms / 60000), 0)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def _pct(x: float | None) -> str:
    if x is None:
        return "—"
    if abs(x) < 0.05:
        return "0.0%"
    return f"{'🟢' if x >= 0 else '🔴'}{x:+.1f}%"


def _sol(x: float) -> str:
    return f"{x:+.4f} SOL"


def closed_trades(fills: list) -> list[dict]:
    """Zamknięte (sprzedane) części pozycji metodą średniej ceny: [{mint, symbol, pnl, pct, leader_id}]."""
    pos: dict[str, list[float]] = {}  # mint -> [tokeny, koszt SOL]
    out = []
    for f in fills:
        qty, cost = pos.setdefault(f["mint"], [0.0, 0.0])
        tokens, sol = f["tokens"] or 0, f["sol"] or 0
        if f["side"] == "buy":
            pos[f["mint"]] = [qty + tokens, cost + sol]
        elif qty > 0 and tokens > 0:
            sold = min(tokens, qty)
            basis = cost * sold / qty
            proceeds = sol * sold / tokens
            out.append({"mint": f["mint"], "symbol": f["symbol"] or short(f["mint"]), "pnl": proceeds - basis,
                        "pct": (proceeds / basis - 1) * 100 if basis > 0 else None, "leader_id": f["leader_id"]})
            pos[f["mint"]] = [qty - sold, cost - basis]
    return out


async def start(s, u, cex0: float | None = None):
    """Zapamiętuje początek sesji i wartość portfeli na starcie (cex0 – jeśli już policzona, bez ponownego zapytania)."""
    uid = u["tg_id"]
    sol0 = None
    if getattr(s, "sol", None):
        s.sol.wallet(uid)  # portfel Solany (demo: 5 SOL) – żeby raport miał punkt startowy
        try:
            from .axiom import portfolio
            sol0 = (await asyncio.wait_for(portfolio(s, u), 15))["total"]
        except Exception:  # noqa: BLE001
            log.debug("Start sesji: wycena Solany", exc_info=True)
    if cex0 is None:
        try:
            cex0 = await asyncio.wait_for(s.engine.equity_of(u), 15)
        except Exception:  # noqa: BLE001
            pass
    s.db.update_user(uid, session_start=now_ms(), session_end=None, session_sol0=sol0, session_cex0=cex0)


async def build(s, u) -> str:
    uid = u["tg_id"]
    start_ms = u["session_start"]
    if not start_ms:
        return "📊 <b>Raport sesji</b>\n\nNie było jeszcze żadnej sesji – włącz kopiowanie ▶️ na pulpicie."
    end_ms = u["session_end"] or now_ms()
    running = not u["session_end"]
    lines = [f"📊 <b>Raport sesji</b>{' (trwa)' if running else ''}",
             f"{_dt(s, start_ms)} → {'teraz' if running else _dt(s, end_ms)} · {_dur(end_ms - start_ms)}"]

    # ---------- Solana ----------
    if getattr(s, "sol", None) and s.sol.has_wallet(uid):
        rows = s.db._q("""SELECT t.*, k.symbol FROM sol_trades t LEFT JOIN sol_tokens k ON k.mint = t.mint
                          WHERE t.tg_id = ? AND t.ts BETWEEN ? AND ? ORDER BY t.id""", (uid, start_ms, end_ms))
        fills = [r for r in rows if r["status"] == "filled"]
        buys = [r for r in fills if r["side"] == "buy"]
        sells = [r for r in fills if r["side"] == "sell"]
        lines += ["", f"🟣 <b>Solana</b> · {'💰 LIVE' if s.sol.is_live(u) else '🧪 demo'}"]
        from .axiom import portfolio
        try:
            pf = await asyncio.wait_for(portfolio(s, u), 20)
        except Exception:  # noqa: BLE001
            pf = None
        if pf and u["session_sol0"]:
            ch = (pf["total"] / u["session_sol0"] - 1) * 100
            lines.append(f"Portfel: {u['session_sol0']:.4f} → <b>{pf['total']:.4f} SOL</b> ({_pct(ch)}, "
                         f"{_sol(pf['total'] - u['session_sol0'])})")
        elif pf:
            lines.append(f"Portfel teraz: <b>{pf['total']:.4f} SOL</b>")
        skipped = [r for r in rows if r["status"] == "skipped"]
        errors = [r for r in rows if r["status"] == "error"]
        lines.append(f"Transakcje: <b>{len(fills)}</b> (🟢 kupna {len(buys)} · 🔴 sprzedaże {len(sells)}) · "
                     f"pominięte {len(skipped)} · błędy {len(errors)}")
        lines.append(f"Obrót: kupiono za {sum(r['sol'] or 0 for r in buys):.4f} SOL · "
                     f"sprzedano za {sum(r['sol'] or 0 for r in sells):.4f} SOL")
        closed = closed_trades(fills)
        if closed:
            wins = [c for c in closed if c["pnl"] > 0]
            realized = sum(c["pnl"] for c in closed)
            best = max(closed, key=lambda c: c["pnl"])
            worst = min(closed, key=lambda c: c["pnl"])
            lines.append(f"Zamknięte: {len(closed)} · trafione {len(wins)} ({len(wins) / len(closed) * 100:.0f}%) · "
                         f"zrealizowany PnL <b>{_sol(realized)}</b>")
            lines.append(f"Najlepsza: {html.escape(best['symbol'])} {_sol(best['pnl'])} ({_pct(best['pct'])}) · "
                         f"najgorsza: {html.escape(worst['symbol'])} {_sol(worst['pnl'])} ({_pct(worst['pct'])})")
        if pf and pf["positions"]:
            unreal = sum((p["value"] or 0) - p["cost"] for p in pf["positions"] if p["cost"] is not None)
            lines.append(f"Otwarte pozycje: {len(pf['positions'])} · wartość "
                         f"{sum(p['value'] or 0 for p in pf['positions']):.4f} SOL · niezrealizowany PnL {_sol(unreal)}")
        lat = [r["latency_ms"] for r in fills if r["latency_ms"] is not None and r["leader_id"]]
        if lat:
            lines.append(f"⚡ Szybkość kopii: śr. {statistics.mean(lat):.0f} ms · mediana {statistics.median(lat):.0f} ms"
                         f" · najszybciej {min(lat)} ms · najwolniej {max(lat)} ms")
        per = defaultdict(lambda: {"n": 0, "pnl": 0.0})
        for r in fills:
            if r["leader_id"]:
                per[r["leader_id"]]["n"] += 1
        for c in closed:
            if c["leader_id"]:
                per[c["leader_id"]]["pnl"] += c["pnl"]
        if per:
            lines.append("<b>Według portfeli:</b>")
            for lid, d in sorted(per.items(), key=lambda kv: -kv[1]["pnl"]):
                lead = s.db.get_leader(lid)
                name = html.escape(lead["name"]) if lead else f"#{lid}"
                lines.append(f"• {name}: {d['n']} kopii · zrealizowany PnL {_sol(d['pnl'])}")
        manual = [r for r in fills if not r["leader_id"]]
        if manual:
            lines.append(f"Ręczne transakcje: {len(manual)}")
        if skipped or errors:
            reasons = Counter(re.split(r" \(| –|:", (r["info"] or r["status"]))[0][:40] for r in skipped + errors)
            lines.append("Pominięcia: " + ", ".join(f"{html.escape(k)} ×{v}" for k, v in reasons.most_common(4)))

    # ---------- giełdy (CEX) ----------
    cex = s.db._q("SELECT status, side, cost FROM copies WHERE tg_id = ? AND ts BETWEEN ? AND ?", (uid, start_ms, end_ms))
    if cex or u["session_cex0"]:
        filled = [r for r in cex if r["status"] == "filled"]
        lines += ["", "🏦 <b>Giełdy (CEX)</b>"]
        lines.append(f"Transakcje: {len(filled)} · pominięte {sum(1 for r in cex if r['status'] == 'skipped')} · "
                     f"błędy {sum(1 for r in cex if r['status'] == 'error')}")
        if u["session_cex0"]:
            try:
                eq = await asyncio.wait_for(s.engine.equity_of(u), 15)
                lines.append(f"Portfel: {u['session_cex0']:,.2f} → <b>{eq:,.2f} {s.cfg.quote}</b> "
                             f"({_pct((eq / u['session_cex0'] - 1) * 100)})".replace(",", " "))
            except Exception:  # noqa: BLE001
                pass
    if len(lines) == 2:
        lines.append("\nW tej sesji nie było jeszcze transakcji.")
    return "\n".join(lines)


async def end(s, u, bot=None, reason: str = "") -> str:
    """Kończy sesję i wysyła raport (HTML przez bota albo zwykłym tekstem przez notify)."""
    if not u["session_start"] or u["session_end"]:
        return ""
    s.db.update_user(u["tg_id"], session_end=now_ms())
    text = await build(s, s.db.get_user(u["tg_id"]))
    if reason:
        text = text.replace("📊 <b>Raport sesji</b>", f"📊 <b>Raport sesji</b> · {html.escape(reason)}", 1)
    try:
        if bot is not None:
            await bot.send_message(u["tg_id"], text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        else:
            await s.engine.notify(u["tg_id"], re.sub(r"<[^>]+>", "", text))
    except Exception:  # noqa: BLE001
        log.exception("Nie wysłano raportu sesji %s", u["tg_id"])
    return text
