"""⚡ Szybka kopia na Pump.fun – transakcja składana w bocie, bez pytania Jupitera/Raptora o wycenę i budowę.

Transakcja tradera zawiera dokładną instrukcję zakupu/sprzedaży na krzywej Pump.fun (wszystkie konta: krzywa,
opłaty, twórca…). Kopiujemy ją, podmieniając TYLKO konta zależne od portfela: portfel, jego konto tokena i licznik
wolumenu użytkownika. Cenę liczymy z krzywej (jedno lekkie zapytanie o konto krzywej). Zalety:
- brak 2 zapytań przez internet (wycena + budowa) – ok. 1–2 bloki szybciej,
- ta sama pula co trader,
- działa też dla świeżych tokenów, których Jupiter jeszcze nie zna (dawniej robił to PumpPortal).
Każdy błąd = None, a engine używa zwykłej drogi (Jupiter/Raptor)."""
import base64
import hashlib
import logging
import struct
import time

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

log = logging.getLogger(__name__)

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM = "11111111111111111111111111111111"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"


def _disc(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


BUY = _disc("buy")                         # (tokeny do kupienia, maks. SOL)
BUY_EXACT_SOL = _disc("buy_exact_sol_in")  # (SOL do wydania, min. tokenów)
SELL = _disc("sell")                       # (tokeny do sprzedania, min. SOL)
KINDS = {BUY: "buy", BUY_EXACT_SOL: "buy_exact_sol_in", SELL: "sell"}

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + B58.index(c)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + raw


def _pk(s) -> Pubkey:
    return s if isinstance(s, Pubkey) else Pubkey.from_string(str(s))


def ata(owner, mint, token_program) -> Pubkey:
    return Pubkey.find_program_address([bytes(_pk(owner)), bytes(_pk(token_program)), bytes(_pk(mint))],
                                       _pk(ATA_PROGRAM))[0]


def user_volume(owner) -> Pubkey:
    return Pubkey.find_program_address([b"user_volume_accumulator", bytes(_pk(owner))], _pk(PUMP))[0]


def bonding_curve(mint) -> Pubkey:
    return Pubkey.find_program_address([b"bonding-curve", bytes(_pk(mint))], _pk(PUMP))[0]


def template_from_tx(tx: dict, wallet: str, mint: str) -> dict | None:
    """Instrukcja Pump.fun tradera (najwyższego poziomu – bez routerów pośrednich) dla danego tokena."""
    try:
        msg = tx["transaction"]["message"]
        keys = {(k["pubkey"] if isinstance(k, dict) else k): k for k in msg["accountKeys"]}
        for ix in msg["instructions"]:
            if ix.get("programId") != PUMP or "data" not in ix:
                continue
            data = b58decode(ix["data"])
            kind = KINDS.get(data[:8])
            accs = ix["accounts"]
            if not kind or len(accs) < 12 or accs[2] != mint or wallet not in accs:
                continue
            return {"kind": kind, "accounts": list(accs), "writable": [bool(keys.get(a, {}).get("writable")) for a in accs],
                    "wallet": wallet, "mint": mint, "token_program": accs[8] if kind != "sell" else accs[9],
                    "slot": int(tx.get("slot") or 0)}
    except (KeyError, IndexError, ValueError, TypeError):
        return None
    return None


def _swap_accounts(tpl: dict, me: str) -> list[AccountMeta] | None:
    """Konta instrukcji tradera z podmienionymi kontami portfela. Nieznane konto zależne od tradera → None."""
    trader = tpl["wallet"]
    subs = {trader: me,
            str(ata(trader, tpl["mint"], tpl["token_program"])): str(ata(me, tpl["mint"], tpl["token_program"])),
            str(user_volume(trader)): str(user_volume(me))}
    out = []
    for a, w in zip(tpl["accounts"], tpl["writable"]):
        new = subs.get(a, a)
        out.append(AccountMeta(_pk(new), is_signer=(new == me), is_writable=w or new == me))
    return out


async def curve(rpc, mint: str) -> dict | None:
    """Stan krzywej: rezerwy wirtualne i rzeczywiste, czy zakończona (token przeszedł na giełdę)."""
    res = await rpc.call("getAccountInfo", [str(bonding_curve(mint)), {"encoding": "base64", "commitment": "processed"}])
    v = (res or {}).get("value")
    if not v:
        return None
    d = base64.b64decode(v["data"][0])
    if len(d) < 49:
        return None
    vt, vs, rt, rs, _ = struct.unpack_from("<5Q", d, 8)
    return {"vt": vt, "vs": vs, "rt": rt, "rs": rs, "complete": bool(d[48])}


FEE_BPS = 125  # opłaty Pump.fun (protokół + twórca) ok. 1,25% – liczymy ostrożnie


def tokens_for_sol(c: dict, lamports: int) -> int:
    net = lamports * 10_000 // (10_000 + FEE_BPS)
    return min(c["vt"] - (c["vt"] * c["vs"]) // (c["vs"] + net), c["rt"])


def sol_for_tokens(c: dict, raw: int) -> int:
    gross = (c["vs"] * raw) // (c["vt"] + raw)
    return gross * (10_000 - FEE_BPS) // 10_000


def _cu(limit: int, micro: int) -> list[Instruction]:
    cb = _pk(COMPUTE_BUDGET)
    return [Instruction(cb, bytes([2]) + struct.pack("<I", limit), []),
            Instruction(cb, bytes([3]) + struct.pack("<Q", micro), [])]


def _ata_idempotent(me: str, mint: str, token_program: str) -> Instruction:
    m = _pk(me)
    return Instruction(_pk(ATA_PROGRAM), bytes([1]), [
        AccountMeta(m, True, True), AccountMeta(ata(me, mint, token_program), False, True),
        AccountMeta(m, False, False), AccountMeta(_pk(mint), False, False),
        AccountMeta(_pk(SYSTEM), False, False), AccountMeta(_pk(token_program), False, False)])


def _tip(me: str, to: str, lamports: int) -> Instruction:
    return Instruction(_pk(SYSTEM), struct.pack("<IQ", 2, lamports),
                       [AccountMeta(_pk(me), True, True), AccountMeta(_pk(to), False, True)])


def instructions(tpl: dict, me: str, side: str, amount_raw: int, limit_raw: int, tip_lamports: int = 0,
                 tip_account: str | None = None, cu_price: int = 0) -> list[Instruction] | None:
    """side=buy: amount_raw = lamporty do wydania, limit_raw = min. tokenów. side=sell: amount_raw = tokeny,
    limit_raw = min. lamportów. None = brak pasującego szablonu."""
    if (side == "buy") == (tpl["kind"] == "sell") or tpl["kind"] == "buy":
        return None  # inna strona szablonu albo „buy” (starszy, nie sprawdzony symulacją) – zwykła droga
    accs = _swap_accounts(tpl, me)
    if accs is None:
        return None
    if side == "buy":  # „wydaj dokładnie X SOL, min. Y tokenów”
        data = BUY_EXACT_SOL + struct.pack("<QQ", int(amount_raw), int(limit_raw)) + b"\x01\x00"
    else:
        data = SELL + struct.pack("<QQ", int(amount_raw), int(limit_raw))
    ixs = _cu(150_000 if side == "buy" else 120_000, cu_price)
    if side == "buy":
        ixs.append(_ata_idempotent(me, tpl["mint"], tpl["token_program"]))
    ixs.append(Instruction(_pk(PUMP), data, accs))
    if tip_lamports and tip_account:
        ixs.append(_tip(me, tip_account, tip_lamports))
    return ixs


def build(tpl: dict, kp: Keypair, side: str, amount_raw: int, limit_raw: int, blockhash: str,
          tip_lamports: int = 0, tip_account: str | None = None, cu_price: int = 0) -> bytes | None:
    """Podpisana transakcja szybkiej kopii albo None (brak pasującego szablonu)."""
    ixs = instructions(tpl, str(kp.pubkey()), side, amount_raw, limit_raw, tip_lamports, tip_account, cu_price)
    if ixs is None:
        return None
    msg = MessageV0.try_compile(kp.pubkey(), ixs, [], Hash.from_string(blockhash))
    return bytes(VersionedTransaction(msg, [kp]))


class Blockhash:
    """Świeży blockhash z pamięci (≤ 1,5 s) – pobierany od razu, gdy przychodzi ruch tradera, równolegle z filtrami."""

    def __init__(self, rpc):
        self.rpc, self.value, self.at = rpc, None, 0.0
        self._task = None

    async def get(self) -> str:
        if self.value and time.monotonic() - self.at < 1.5:
            return self.value
        res = await self.rpc.call("getLatestBlockhash", [{"commitment": "processed"}])
        self.value, self.at = res["value"]["blockhash"], time.monotonic()
        return self.value

    def warm(self):
        import asyncio
        if self.value and time.monotonic() - self.at < 1.0 or (self._task and not self._task.done()):
            return
        self._task = asyncio.ensure_future(self.get())
        self._task.add_done_callback(lambda f: f.cancelled() or f.exception())
