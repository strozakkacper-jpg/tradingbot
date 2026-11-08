"""Raporty dla czatu i panelu: wyniki traderów, ranking i portfel użytkownika."""
import html as _html
import time
from datetime import datetime

from .db import now_ms, ref_id
from .stats import downsample, max_drawdown, period_return, trade_stats

DAY_MS = 24 * 3600 * 1000
_cache: dict = {}


def fmt_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%d.%m.%Y")


def fmt_pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:+.2f}%"


async def leader_report(s, leader_id: int, max_age: float = 60) -> dict:
    """Wyniki portfela wzorcowego tradera (każde zlecenie 1:1). Cache, bo panel i ranking pytają często."""
    hit = _cache.get((id(s.db), leader_id))
    if hit and time.monotonic() - hit[0] < max_age:
        return hit[1]
    now = now_ms()
    rid = ref_id(leader_id)
    s.db.ensure_reference(leader_id, s.cfg.paper_start, s.cfg.quote)
    equity = await s.engine.m(s.db.get_leader(leader_id)).equity(s.db.paper_balances(rid))
    since = int(s.db.get_state(f"reference_start:{leader_id}") or now)
    # punkt startowy portfela wzorcowego – bez niego nowy trader miałby wynik 30 dni = 0%
    series = [(since, s.cfg.paper_start)] + s.db.equity_series(rid, True) + [(now, equity)]
    fills = s.db.fills(rid)
    rep = {
        "id": leader_id,
        "since": since,
        "equity": equity,
        "start": s.cfg.paper_start,
        "total": equity / s.cfg.paper_start - 1,
        "d7": period_return(series, now - 7 * DAY_MS, equity),
        "d30": period_return(series, now - 30 * DAY_MS, equity),
        "max_dd": max_drawdown([v for _, v in series]),
        **trade_stats(fills),
        "series": downsample(series),
        "recent": list(reversed(fills[-8:])),
    }
    _cache[(id(s.db), leader_id)] = (time.monotonic(), rep)
    return rep


def score(rep: dict) -> float:
    """Kolejność w rankingu: wynik z 30 dni (albo od startu), karany za duży spadek."""
    ret = rep["d30"] if rep["d30"] is not None else rep["total"]
    return ret - 0.5 * rep["max_dd"]


async def ranking(s) -> list[tuple]:
    """[(trader, raport, (wybierających, kopiujących)), …] od najlepszego."""
    counts = s.db.follower_counts()
    out = []
    for lead in s.db.leaders("active"):
        try:
            out.append((lead, await leader_report(s, lead["id"]), counts.get(lead["id"], (0, 0))))
        except Exception:  # noqa: BLE001
            continue
    return sorted(out, key=lambda x: score(x[1]), reverse=True)


async def user_report(s, u) -> dict:
    paper = bool(u["paper"])
    equity = await s.engine.equity_of(u)
    base = s.cfg.paper_start if paper else (u["start_equity"] or equity)
    series = s.db.equity_series(u["tg_id"], paper) + [(now_ms(), equity)]
    return {
        "paper": paper,
        "active": bool(u["active"]),
        "equity": equity,
        "base": base,
        "pnl": equity - base,
        "pnl_pct": equity / base - 1 if base else 0,
        "since_start": (equity / u["start_equity"] - 1) if u["active"] and u["start_equity"] else None,
        "max_dd": max_drawdown([v for _, v in series]),
        "series": downsample(series),
        "multiplier": u["multiplier"],
        "max_trade": u["max_trade"],
        "stop_loss": u["stop_loss"],
        "history": [dict(r) for r in s.db.history(u["tg_id"], 20)],
    }


def leader_text(rep: dict, quote: str, leader=None, followers: tuple[int, int] = (0, 0)) -> str:
    wr = rep["win_rate"]
    name = _html.escape(leader["name"]) if leader else "lidera"
    src = ("📡 konto giełdowe, kopiowane automatycznie" if leader and leader["source"] == "api"
           else "✍️ sygnały wysyłane przez tradera") if leader else ""
    lines = [
        f"🏆 <b>{name}</b>" if leader else "🏆 <b>Wyniki lidera</b>",
    ]
    if leader and leader["bio"]:
        lines.append(f"<i>{_html.escape(leader['bio'])}</i>")
    if leader:
        lines.append(f"{src} · kopiuje: {followers[1]} os.")
    lines += [
        f"<i>Portfel wzorcowy: każde zlecenie skopiowane 1:1 od {fmt_date(rep['since'])}, "
        f"start {rep['start']:g} {quote}, z prowizją 0,1%.</i>", "",
        f"Od startu: <b>{fmt_pct(rep['total'])}</b>",
        f"7 dni: {fmt_pct(rep['d7'])} · 30 dni: {fmt_pct(rep['d30'])}",
        f"Największy spadek: {fmt_pct(-rep['max_dd'])}",
        f"Zamknięte transakcje: {rep['closed']}"
        + (f" · trafione: {rep['wins']} ({wr * 100:.0f}%)" if wr is not None else ""),
    ]
    if rep["closed"]:
        lines.append(f"Najlepsza: {rep['best']:+.2f} {quote} · najgorsza: {rep['worst']:+.2f} {quote}")
    if rep["recent"]:
        lines += ["", "<b>Ostatnie zlecenia</b>"]
        for f in rep["recent"][:5]:
            icon = "🟢" if f["side"] == "buy" else "🔴"
            lines.append(f"{icon} {fmt_date(f['ts'])} {f['symbol']} @ {f['price']:,.2f}".replace(",", " "))
    lines += ["", "⚠️ Wyniki z przeszłości nie gwarantują przyszłych zysków."]
    return "\n".join(lines)
