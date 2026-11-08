"""Statystyki: wynik, trafność transakcji, największy spadek (czysta logika, bez I/O)."""


def max_drawdown(values: list[float]) -> float:
    """Największy spadek od szczytu, jako ułamek (0.2 = -20%)."""
    peak, worst = 0.0, 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak)
    return worst


def period_return(series: list[tuple[int, float]], since_ts: int, current: float) -> float | None:
    """Zwrot od pierwszego punktu po `since_ts` do wartości bieżącej."""
    for ts, v in series:
        if ts >= since_ts and v > 0:
            return current / v - 1
    return None


def trade_stats(fills: list[dict]) -> dict:
    """Odtwarza pozycje metodą średniej ceny zakupu i liczy wynik każdej sprzedaży."""
    pos: dict[str, list[float]] = {}  # symbol -> [ilość, koszt]
    results: list[float] = []
    for f in fills:
        qty, cost = pos.setdefault(f["symbol"], [0.0, 0.0])
        if f["side"] == "buy":
            pos[f["symbol"]] = [qty + f["amount"], cost + f["cost"]]
        elif qty > 0:
            sold = min(f["amount"], qty)
            basis = cost * sold / qty
            results.append(f["price"] * sold - basis)
            pos[f["symbol"]] = [qty - sold, cost - basis]
    wins = sum(1 for r in results if r > 0)
    return {
        "closed": len(results),
        "wins": wins,
        "win_rate": wins / len(results) if results else None,
        "realized": sum(results),
        "best": max(results, default=0.0),
        "worst": min(results, default=0.0),
    }


def downsample(series: list[tuple[int, float]], max_points: int = 300) -> list[tuple[int, float]]:
    if len(series) <= max_points:
        return series
    step = len(series) / max_points
    out = [series[int(i * step)] for i in range(max_points)]
    if out[-1] != series[-1]:
        out.append(series[-1])
    return out
