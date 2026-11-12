"""Automatyczne ustawienia pod bieżące saldo – liczone z prawdziwych danych, nie „na oko”.

Co bierze pod uwagę:
  • saldo (SOL wolne + tokeny) i to, ile zablokują depozyty kont tokenów (0,002 SOL na każdą otwartą pozycję),
  • koszty jednej transakcji tam i z powrotem (opłata sieci + priorytet/napiwek) – kwota zakupu musi być na tyle
    duża, żeby opłaty zjadały najwyżej ~2,5% (inaczej nawet dobry trader da Ci stratę),
  • za ile NAPRAWDĘ kupuje każdy z Twoich traderów (mediana z 7 dni z jego portfela wzorcowego, a gdy brak –
    z rankingu / badania) → osobny % dla każdego, tak żeby jego zwykłe wejście dało u Ciebie zwykłą pozycję,
  • ilu masz traderów i jak często kupują → ile pozycji naraz, żeby nie zabrakło SOL na nowe okazje,
  • (opcjonalnie) wyniki: traderzy, którzy dają Ci zysk, dostają ×1,5, tracący ×0,5.

Tryb „🤖 Auto” przelicza to samo co kilkadziesiąt minut i zapisuje, gdy saldo zmieni się o ≥12%, zmieni się
lista traderów albo minie doba. Strategii (stop-loss, ×2, filtry) nie rusza – tylko kwoty i limity.
"""
import json
import logging
import statistics

from ..db import now_ms, ref_id
from .parser import LAMPORTS

log = logging.getLogger(__name__)
DAY_MS = 24 * 3600 * 1000
RENT = 0.00203928
BASE_FEE = 5_000
MAX_FEE_SHARE = 0.025  # opłaty (tam i z powrotem) najwyżej 2,5% pozycji
MAX_ONE_BUY = 3.0  # SOL – większe kupno w małym memecoinie samo rusza cenę
HINT_CAP = 5.0  # SOL – górna granica „średniego zakupu” z rankingu, dopóki bot nie zobaczy prawdziwych
SIZING_KEYS = ("sol_buy_mode", "sol_pct", "sol_buy", "sol_max", "sol_min_buy", "sol_max_pos", "sol_day_limit",
               "sol_priority")
# 🛡 Bezpieczniki – ustawia je tylko człowiek. Automaty (✨ Ustaw za mnie, 🤖 Auto, 🚀 Autopilot) ich nie zmieniają,
# najwyżej podpowiadają wartości pasujące do salda.
SAFETY_KEYS = ("sol_safe", "sol_min_buy", "sol_max_pos", "sol_streak", "sol_max", "sol_day_limit")


def r2(x: float) -> float:
    return float(f"{x:.2g}")


def tip_for(balance: float, live: bool) -> int:
    """Priorytet/napiwek: przy małym saldzie taniej (opłata nie może zjeść zysku), przy większym szybciej."""
    if not live:
        return 200_000
    return 100_000 if balance < 0.5 else 150_000 if balance < 2 else 250_000 if balance < 10 else 400_000


def position_share(balance: float) -> float:
    """Jaka część salda na zwykłą pozycję. Małe saldo musi grać większą częścią, inaczej opłaty zjedzą zysk."""
    return 0.10 if balance < 0.3 else 0.08 if balance < 1 else 0.06 if balance < 5 else 0.05 if balance < 20 else 0.04


def trader_profiles(s, uid: int) -> list[dict]:
    """Każdy włączony trader: za ile zwykle kupuje (mediana 7 dni) i ile razy dziennie."""
    from .api import saved_list
    since = now_ms() - 7 * DAY_MS
    lb = {}
    for r in s.db._q("SELECT wallet, stats FROM leaderboard WHERE period = 7"):
        try:
            a = json.loads(r["stats"])
        except ValueError:
            continue
        if a.get("avg_buy"):
            lb[r["wallet"]] = a["avg_buy"]
    out = []
    for it in saved_list(s, uid):
        if it["status"] != "copy" or not it["lid"]:
            continue
        rows = s.db._q("""SELECT info, ts FROM sol_trades WHERE tg_id = ? AND side = 'buy' AND status = 'filled'
                          AND ts >= ?""", (ref_id(it["lid"]), since))
        buys = []
        for r in rows:
            try:
                v = float(r["info"]) if r["info"] else None
            except ValueError:
                v = None
            if v and v > 0:
                buys.append(v)
        src = "7 dni jego zakupów"
        # 1) prawdziwe zakupy, które bot sam widział (nawet 1–2 są lepsze niż cudza średnia)
        median = statistics.median(buys) if buys else None
        if buys and len(buys) < 3:
            src = f"{len(buys)} jego zakupy"
        if median is None:
            # 2) ranking / badanie – „średni zakup” z serwisów bywa liczony na cały token, nie na jedno
            #    kupno (wychodziło ~100 SOL przy realnych 0,3 SOL) → ograniczamy do rozsądnego zakresu
            hint = lb.get(it["wallet"]) or ((it.get("stats") or {}).get("avg_buy"))
            median = min(max(hint, 0.05), HINT_CAP) if hint else None
            src = "ranking / badanie (szacunek)" if median else "brak danych"
        span_days = max((now_ms() - min(r["ts"] for r in rows)) / DAY_MS, 0.25) if rows else None
        out.append({"lid": it["lid"], "wallet": it["wallet"], "name": it["name"], "median": median, "src": src,
                    "buys": len(buys), "per_day": len(rows) / span_days if span_days else None})
    return out


def plan(balance: float, traders: list[dict], live: bool, perf: dict | None = None) -> dict:
    """Wylicza ustawienia kwot i limitów + osobny % dla każdego tradera. Zwraca też wyjaśnienie."""
    tip = tip_for(balance, live)
    fees = 2 * (BASE_FEE + tip) / LAMPORTS
    min_buy = max(fees / MAX_FEE_SHARE, 0.005)
    reserve = 0.01 + (0.01 if live else 0)  # opłaty / wypłaty
    share = position_share(balance)
    # pozycje naraz: ilu traderów × jak często kupują – ale razem maks. ok. 85% salda w tokenach
    n_tr = max(len(traders), 1)
    busy = sum(t["per_day"] or 3 for t in traders) if traders else 5
    want_pos = min(max(round(n_tr * 2 + busy / 25), 4), 20)
    usable = max(balance - reserve, 0.0)
    target = usable * share
    max_pos = int(min(want_pos, max(usable * 0.85 / max(target, 1e-9), 1)))
    usable_pos = usable - max_pos * RENT  # depozyty kont tokenów też blokują SOL
    target = max(min(usable_pos * share, MAX_ONE_BUY), 0.0)
    warn = None
    if target < min_buy:
        # małe saldo: lepiej mniej pozycji po sensownej kwocie niż dużo pozycji, na których zarabia tylko sieć
        target = min_buy
        max_pos = int(max(min(usable_pos * 0.85 // max(min_buy, 1e-9), 20), 0))
        warn = (f"Saldo jest małe – żeby opłaty nie zjadały zysku, zakup musi mieć min. {min_buy:.3f} SOL, "
                f"więc naraz zmieści się ok. {max_pos} pozycji. Doładuj portfel, żeby kopiować więcej.")
        if max_pos < 1:
            warn = (f"Za mało SOL na kopiowanie z zyskiem (potrzeba min. ~{min_buy + RENT + reserve:.3f} SOL). "
                    "Doładuj portfel.")
            max_pos = 0
    per = {}
    medians = [t["median"] for t in traders if t.get("median")]
    typical = statistics.median(medians) if medians else 1.0
    for t in traders:
        m = t.get("median") or typical
        mult = (perf or {}).get(t["lid"], 1.0)
        per[t["lid"]] = r2(min(max(target * mult / max(m, 0.01) * 100, 0.1), 100))
    vals = {"sol_buy_mode": "pct", "sol_pct": r2(min(max(target / max(typical, 0.01) * 100, 0.1), 100)),
            "sol_buy": r2(target), "sol_max": r2(min(target * 2, MAX_ONE_BUY)), "sol_min_buy": r2(min_buy),
            "sol_max_pos": max_pos, "sol_day_limit": r2(max(balance * 0.2, 0.005)), "sol_priority": tip}
    return {"values": vals, "per_trader": per, "target": target, "fees": fees, "typical": typical, "warn": warn,
            "balance": balance}


def performance(s, u) -> dict[int, float]:
    """×1,5 dla górnej 1/3 traderów z zyskiem (7 dni), ×0,5 dla dolnej 1/3 ze stratą – gdy włączone 🧠."""
    from .api import ranking
    if not u["sol_smart"]:
        return {}
    rows = [r for r in ranking(s, u["tg_id"], not s.sol.is_live(u)) if r["status"] == "copy" and r["lid"]
            and r["sells"] >= 3]
    if len(rows) < 2:
        return {}
    rows.sort(key=lambda r: -r["pnl_7d"])
    third = max(len(rows) // 3, 1)
    out = {}
    for i, r in enumerate(rows):
        if i < third and r["pnl_7d"] > 0:
            out[r["lid"]] = 1.5
        elif i >= len(rows) - third and r["pnl_7d"] < 0:
            out[r["lid"]] = 0.5
    return out


def describe(p: dict, traders: list[dict], u=None) -> str:
    v = p["values"]
    lines = [f"Saldo {p['balance']:.3f} SOL → zwykła pozycja ~{p['target']:.3g} SOL "
             f"(opłaty ~{p['fees']:.4f} SOL za kupno+sprzedaż)."]
    if u is not None:
        cur = (f"maks. {u['sol_max']:g} SOL na zakup" if u["sol_max"] else "bez limitu na zakup",
               f"min. zakup {u['sol_min_buy']:g} SOL" if u["sol_min_buy"] else "bez min. zakupu",
               f"do {u['sol_max_pos']} pozycji" if u["sol_max_pos"] else "bez limitu pozycji",
               f"limit straty dnia {u['sol_day_limit']:g} SOL" if u["sol_day_limit"] else "bez limitu dnia")
        lines.append("🛡 Bezpieczniki bez zmian (Twoje): " + ", ".join(cur) + ".")
        hint = []
        if not u["sol_max_pos"] or u["sol_max_pos"] > v["sol_max_pos"] * 1.5:
            hint.append(f"do {v['sol_max_pos']} pozycji")
        if not u["sol_min_buy"] or u["sol_min_buy"] < v["sol_min_buy"] * 0.5:
            hint.append(f"min. zakup {v['sol_min_buy']:g} SOL")
        if not u["sol_day_limit"]:
            hint.append(f"limit dnia {v['sol_day_limit']:g} SOL")
        if u["sol_max"] and u["sol_max"] < p["target"]:
            hint.append(f"maks. na zakup co najmniej {v['sol_max']:g} SOL (teraz tnie zwykłe pozycje)")
        if hint:
            lines.append("💡 Sugestia do 🛡 Bezpieczników przy tym saldzie: " + ", ".join(hint) + ".")
    for t in traders[:12]:
        pct = p["per_trader"].get(t["lid"])
        if pct is None:
            continue
        m = t.get("median")
        lines.append(f"• {t['name']}: {pct:g}% jego kwoty" + (f" (kupuje zwykle ~{m:.3g} SOL → Ty ~{m * pct / 100:.3g} SOL)"
                                                             if m else " (jeszcze nie znam jego kwot – jak średnia)"))
    if len(traders) > 12:
        lines.append(f"• …i {len(traders) - 12} kolejnych")
    if p["warn"]:
        lines.append("⚠️ " + p["warn"])
    return "\n".join(lines)


def apply(s, u, balance: float, full: bool = False) -> dict:
    """Zapisuje ustawienia z planu. full=True – także strategia (jak „✨ Ustaw za mnie”)."""
    from .api import recommended
    uid = u["tg_id"]
    traders = trader_profiles(s, uid)
    live = s.sol.is_live(u)
    p = plan(balance, traders, live, performance(s, u))
    vals = dict(p["values"])
    if full:
        base = recommended(balance, p["typical"], live)
        vals = {**base, **vals}
    vals = {k: v for k, v in vals.items() if k not in SAFETY_KEYS}  # 🛡 bezpieczników nie ruszamy
    s.db.update_user(uid, sol_autocfg_bal=balance, sol_autocfg_at=now_ms(),
                     sol_autocfg_sig=signature(traders), **vals)
    for t in traders:
        fr = s.db.follow_row(uid, t["lid"])
        if fr and fr["sol_buy"]:
            continue  # stała kwota ustawiona ręcznie dla tego tradera – zostaje
        s.db.set_follow_amount(uid, t["lid"], pct=p["per_trader"][t["lid"]])
    p["text"] = describe(p, traders, s.db.get_user(uid))
    p["traders"] = traders
    return p


def signature(traders: list[dict]) -> str:
    """Lista traderów + rząd wielkości ich zakupów – gdy realna kwota tradera zmieni się ~2×, przeliczamy."""
    import math
    return ",".join(f"{t['lid']}:{round(math.log2(t['median'])) if t.get('median') else '-'}"
                    for t in sorted(traders, key=lambda t: t["lid"]))


def needs_update(u, balance: float, traders: list[dict]) -> str | None:
    last_bal, last_at = u["sol_autocfg_bal"], u["sol_autocfg_at"] or 0
    if not last_bal:
        return "pierwsze ustawienie"
    if abs(balance / last_bal - 1) >= 0.12:
        return f"saldo zmieniło się z {last_bal:.3f} na {balance:.3f} SOL"
    if signature(traders) != (u["sol_autocfg_sig"] or ""):
        return "zmieniła się lista traderów"
    if now_ms() - last_at > DAY_MS:
        return "codzienne przeliczenie"
    return None


async def autocfg_job(ctx):
    s = ctx.application.bot_data["svc"]
    if not getattr(s, "sol", None):
        return
    from .axiom import portfolio
    for u in s.db._q("SELECT * FROM users WHERE approved = 1 AND sol_autocfg = 1"):
        try:
            pf = await portfolio(s, u)
            bal = pf["total"]
            if not bal or bal <= 0:
                continue
            why = needs_update(u, bal, trader_profiles(s, u["tg_id"]))
            if not why:
                continue
            old = {k: u[k] for k in SIZING_KEYS}
            p = apply(s, u, bal)
            changed = not old["sol_buy"] or abs(p["values"]["sol_buy"] / old["sol_buy"] - 1) > 0.1 or \
                old["sol_pct"] != p["values"]["sol_pct"]
            if changed:
                await s.sol.say(u, "auto", f"🤖 <b>AUTO-USTAWIENIA</b> ({why})\n" + _esc(p["text"]))
        except Exception:  # noqa: BLE001
            log.exception("Auto-ustawienia %s", u["tg_id"])


def _esc(t: str) -> str:
    import html
    return html.escape(t)
