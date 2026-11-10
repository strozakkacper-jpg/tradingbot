"""Optymalizator ustawień: odtwarza prawdziwe ruchy Twoich traderów z ostatnich dni (portfele wzorcowe)
z różnymi ustawieniami i pokazuje, które zarobiłyby najwięcej.

Uczciwie: liczymy po cenach z transakcji traderów, z karą za opóźnienie kopii (gorsza cena w każdą stronę)
i z opłatami. Stop-lossa / ruchomego stopu nie da się tu odtworzyć (nie mamy pełnego wykresu ceny między
transakcjami), więc optymalizator dobiera kwoty i filtry – a sprzedaż idzie za traderem.
"""
import itertools
import statistics

from ..db import now_ms, ref_id

DAY_MS = 24 * 3600 * 1000
RENT = 0.00203928
PENALTY = 0.02  # 2% gorsza cena przy kopii (opóźnienie, poślizg)


def load_events(s, lids: list[int], days: float = 7) -> list[dict]:
    since = now_ms() - int(days * DAY_MS)
    rids = [ref_id(lid) for lid in lids]
    if not rids:
        return []
    rows = s.db._q(f"""SELECT tg_id, side, mint, sol, tokens, info, ts FROM sol_trades
                       WHERE tg_id IN ({','.join('?' * len(rids))}) AND status = 'filled' AND tokens > 0 AND ts >= ?
                       ORDER BY ts, id""", (*rids, since))
    held: dict[tuple, float] = {}
    out = []
    for r in rows:
        lid, key = -r["tg_id"], (-r["tg_id"], r["mint"])
        before = held.get(key, 0.0)
        if r["side"] == "buy":
            held[key] = before + r["tokens"]
            frac = None
        else:
            if before <= 0:
                continue
            frac = min(r["tokens"] / before, 1.0)
            held[key] = max(before - r["tokens"], 0.0)
        try:
            trader_sol = float(r["info"]) if r["info"] else None
        except ValueError:
            trader_sol = None
        out.append({"lid": lid, "side": r["side"], "mint": r["mint"], "price": r["sol"] / r["tokens"],
                    "trader_sol": trader_sol, "ref_sol": r["sol"], "frac": frac, "ts": r["ts"]})
    return out


def hold_medians(events: list[dict]) -> dict[int, float]:
    start: dict[tuple, int] = {}
    holds: dict[int, list] = {}
    for e in events:
        key = (e["lid"], e["mint"])
        if e["side"] == "buy":
            start.setdefault(key, e["ts"])
        elif e["frac"] and e["frac"] > 0.97 and key in start:
            holds.setdefault(e["lid"], []).append((e["ts"] - start.pop(key)) / 1000)
    return {lid: statistics.median(h) for lid, h in holds.items() if len(h) >= 3}


def simulate(events: list[dict], balance: float, v: dict, holds: dict, fee: float = 0.00041) -> dict:
    cash, pos, by_lead, last_px = balance, {}, {}, {}
    trades = wins = 0
    lead_pnl: dict[int, float] = {}
    cost: dict[tuple, float] = {}
    for e in events:
        last_px[e["mint"]] = e["price"]
        lid, mint = e["lid"], e["mint"]
        if e["side"] == "buy":
            if v["min_hold"] and holds.get(lid) is not None and holds[lid] < v["min_hold"]:
                continue
            if v["min_lead"] and e["trader_sol"] is not None and e["trader_sol"] < v["min_lead"]:
                continue
            if v["no_add"] and pos.get(mint, 0) > 0:
                continue
            if v["max_pos"] and sum(1 for x in pos.values() if x > 0) >= v["max_pos"] and pos.get(mint, 0) <= 0:
                continue
            if v["mode"] == "pct" and e["trader_sol"]:
                amount = e["trader_sol"] * v["pct"] / 100
            else:
                amount = balance * v["size"]
            amount = min(amount, balance * v["size"] * 2, cash - fee - RENT)
            if amount < max(v["min_buy"], 0.001):
                continue
            rent = RENT if pos.get(mint, 0) <= 0 else 0
            tok = amount / e["price"] * (1 - PENALTY)
            cash -= amount + fee / 2 + rent
            pos[mint] = pos.get(mint, 0) + tok
            by_lead[(lid, mint)] = by_lead.get((lid, mint), 0) + tok
            cost[(lid, mint)] = cost.get((lid, mint), 0) + amount
        else:
            mine = by_lead.get((lid, mint), 0)
            if mine <= 0:
                continue
            q = 1.0 if e["frac"] > 0.97 else e["frac"]
            tok = mine * q
            proceeds = tok * e["price"] * (1 - PENALTY)
            basis = cost.get((lid, mint), 0) * q
            by_lead[(lid, mint)] = mine - tok
            cost[(lid, mint)] = cost.get((lid, mint), 0) - basis
            pos[mint] = max(pos.get(mint, 0) - tok, 0)
            cash += proceeds - fee / 2 + (RENT if pos[mint] <= 1e-12 else 0)
            trades += 1
            wins += proceeds > basis
            lead_pnl[lid] = lead_pnl.get(lid, 0) + proceeds - basis
    open_val = sum(t * last_px.get(m, 0) * 0.8 for m, t in pos.items() if t > 0)  # otwarte: z dyskontem 20%
    equity = cash + open_val
    return {"pnl_pct": (equity / balance - 1) * 100, "equity": equity, "trades": trades,
            "win_rate": wins / trades if trades else None, "open_value": open_val, "lead_pnl": lead_pnl}


def variants(balance: float, typical: float) -> list[dict]:
    base_pct = max(min(balance * 0.06 / max(typical, 0.05) * 100, 100), 0.1)
    sizing = [{"mode": "fixed", "size": f, "pct": 0} for f in (0.03, 0.06, 0.1)] + \
             [{"mode": "pct", "size": 0.06 * m, "pct": base_pct * m} for m in (0.5, 1, 2)]
    out = []
    for sz, mh, na, ml in itertools.product(sizing, (0, 20, 60), (0, 1), (0, 0.5, 1.0)):
        out.append({**sz, "min_hold": mh, "no_add": na, "min_lead": ml, "max_pos": 15, "min_buy": 0.005})
    return out


def from_user(u, balance: float) -> dict:
    mode = "pct" if u["sol_buy_mode"] == "pct" else "fixed"
    return {"mode": mode, "pct": u["sol_pct"], "size": (u["sol_buy"] / balance) if balance else 0.06,
            "min_hold": u["sol_min_hold"], "no_add": u["sol_no_add"], "min_lead": u["sol_min_lead"],
            "max_pos": u["sol_max_pos"] if "sol_max_pos" in u.keys() else 0,
            "min_buy": u["sol_min_buy"] if "sol_min_buy" in u.keys() else 0}


def describe(v: dict, balance: float) -> str:
    size = f"{v['pct']:.2g}% kwoty tradera" if v["mode"] == "pct" else f"stała {balance * v['size']:.3g} SOL"
    parts = [size]
    if v["no_add"]:
        parts.append("nie dokupuj")
    if v["min_lead"]:
        parts.append(f"tylko zakupy tradera ≥ {v['min_lead']:g} SOL")
    if v["min_hold"]:
        parts.append(f"bez flipperów < {v['min_hold']:g} s")
    return " · ".join(parts)


def to_settings(v: dict, balance: float) -> dict:
    out = {"sol_min_hold": v["min_hold"], "sol_no_add": int(v["no_add"]), "sol_min_lead": v["min_lead"],
           "sol_max": float(f"{balance * v['size'] * 2:.2g}")}
    if v["mode"] == "pct":
        out.update(sol_buy_mode="pct", sol_pct=float(f"{v['pct']:.2g}"))
    else:
        out.update(sol_buy_mode="fixed", sol_buy=float(f"{balance * v['size']:.2g}"))
    return out


def optimize(s, u, balance: float, lids: list[int], typical: float, days: float = 7) -> dict:
    events = load_events(s, lids, days)
    if not events:
        return {"events": 0, "results": [], "current": None, "leaders": []}
    holds = hold_medians(events)
    cur = simulate(events, balance, from_user(u, balance), holds)
    res = []
    for v in variants(balance, typical):
        r = simulate(events, balance, v, holds)
        res.append({"v": v, **r})
    res.sort(key=lambda r: -r["pnl_pct"])
    best = res[0]
    names = {ld["id"]: ld["name"] for ld in s.db.leaders("active")}
    leaders = sorted(({"id": lid, "name": names.get(lid, f"#{lid}"), "pnl": p} for lid, p in best["lead_pnl"].items()),
                     key=lambda x: -x["pnl"])
    top = []
    seen = {describe(from_user(u, balance), balance)}
    for r in res:
        d = describe(r["v"], balance)
        # tylko warianty wyraźnie lepsze od obecnych (min. +0,5 pkt %) i z prawdziwymi sprzedażami
        if d in seen or r["trades"] < 1 or r["pnl_pct"] < cur["pnl_pct"] + 0.5:
            continue
        seen.add(d)
        top.append({"desc": d, "pnl_pct": r["pnl_pct"], "trades": r["trades"], "win_rate": r["win_rate"],
                    "settings": to_settings(r["v"], balance)})
        if len(top) >= 6:
            break
    sells = sum(1 for e in events if e["side"] == "sell")
    return {"events": len(events), "sells": sells, "enough": len(events) >= 30 and sells >= 10,
            "days": days, "results": top, "leaders": leaders,
            "current": {"desc": describe(from_user(u, balance), balance), "pnl_pct": cur["pnl_pct"],
                        "trades": cur["trades"], "win_rate": cur["win_rate"]}}
