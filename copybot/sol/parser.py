"""Rozpoznawanie wymian (swapów) w transakcji Solany – czysta logika, bez sieci.

Działa dla każdej giełdy (Jupiter, Raydium, Pump.fun, Meteora, Orca…), bo nie czyta instrukcji,
tylko zmiany sald portfela: ubyło SOL i przybyło tokena = KUPNO, odwrotnie = SPRZEDAŻ.
"""
from dataclasses import dataclass

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
LAMPORTS = 1_000_000_000
STABLES = {USDC_MINT: "USDC", USDT_MINT: "USDT"}


@dataclass(frozen=True)
class Swap:
    signature: str
    wallet: str
    side: str  # "buy" | "sell"
    mint: str
    token_raw: int  # ilość tokena (jednostki bazowe), wartość bezwzględna
    decimals: int
    quote_symbol: str  # czym lider płacił / co dostał: SOL, USDC lub USDT
    quote_amount: float
    fraction: float  # jaki ułamek salda użyto (SOL przy kupnie, tokena przy sprzedaży)
    ts: int


def _keys(tx: dict) -> list[str]:
    return [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]


def parse_swap(tx: dict | None, wallet: str, min_sol: float = 0.0005, min_usd: float = 0.1) -> Swap | None:
    if not tx or not tx.get("meta") or tx["meta"].get("err") is not None:
        return None
    meta = tx["meta"]
    keys = _keys(tx)

    sol_delta, pre_sol = 0, 0
    if wallet in keys:
        i = keys.index(wallet)
        pre_sol = meta["preBalances"][i]
        sol_delta = meta["postBalances"][i] - pre_sol
        if i == 0:  # płacący opłatę – nie liczymy opłaty sieci jako „wydanych” SOL
            sol_delta += meta.get("fee", 0)

    tokens: dict[str, list[int]] = {}  # mint -> [przed, po, decimals]
    for field, idx in (("preTokenBalances", 0), ("postTokenBalances", 1)):
        for b in meta.get(field) or []:
            if b.get("owner") != wallet:
                continue
            t = tokens.setdefault(b["mint"], [0, 0, int(b["uiTokenAmount"]["decimals"])])
            t[idx] += int(b["uiTokenAmount"]["amount"])

    if wsol := tokens.pop(SOL_MINT, None):  # wrapped SOL traktujemy jak SOL
        sol_delta += wsol[1] - wsol[0]
        pre_sol += wsol[0]

    stables = {m: tokens.pop(m) for m in list(tokens) if m in STABLES}
    changed = {m: v for m, v in tokens.items() if v[0] != v[1]}
    if len(changed) != 1:  # brak zmiany albo wymiana token↔token – pomijamy
        return None
    mint, (pre, post, decimals) = next(iter(changed.items()))
    tok_delta = post - pre
    side = "buy" if tok_delta > 0 else "sell"
    want_sign = -1 if side == "buy" else 1  # przy kupnie waluta ubywa, przy sprzedaży przybywa

    # najpierw stablecoiny (przy płatności w USDC zmiana SOL to tylko opłaty / rent za konto)
    quote = None
    for m, (q_pre, q_post, q_dec) in stables.items():
        d = q_post - q_pre
        if d * want_sign > 0 and abs(d) / 10 ** q_dec >= min_usd:
            quote = (STABLES[m], abs(d) / 10 ** q_dec, q_pre, abs(d))
    if quote is None and sol_delta * want_sign > 0 and abs(sol_delta) / LAMPORTS >= min_sol:
        quote = ("SOL", abs(sol_delta) / LAMPORTS, pre_sol, abs(sol_delta))
    if quote is None:
        return None
    q_symbol, q_amount, q_pre, q_raw = quote
    if side == "buy":
        fraction = q_raw / q_pre if q_pre > 0 else 0.0
    else:
        fraction = -tok_delta / pre if pre > 0 else 1.0
    return Swap(signature=tx["transaction"]["signatures"][0], wallet=wallet, side=side, mint=mint,
                token_raw=abs(tok_delta), decimals=decimals, quote_symbol=q_symbol, quote_amount=q_amount,
                fraction=min(max(fraction, 0.0), 1.0), ts=int(tx.get("blockTime") or 0))
