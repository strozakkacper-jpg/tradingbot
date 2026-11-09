"""Solana RPC (Helius) i Jupiter – nastawione na minimalne opóźnienie."""
import asyncio
import base64
import logging
import time

import aiohttp
from solders.hash import Hash
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction, VersionedTransaction

log = logging.getLogger(__name__)
# Solana ma już transakcje w wersji 1 (np. trasy Jupitera) – z „0” RPC odrzuca je błędem -32015
TX_VERSION = 1
TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")


class ChainError(Exception):
    pass


class Rpc:
    def __init__(self, url: str, session: aiohttp.ClientSession):
        self.url, self.session = url, session
        self._id = 0

    async def call(self, method: str, params: list | None = None):
        for attempt in range(4):
            self._id += 1
            async with self.session.post(self.url, json={"jsonrpc": "2.0", "id": self._id, "method": method,
                                                         "params": params or []}) as r:
                if r.status == 429:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                data = await r.json(content_type=None)
            if "error" in data:
                raise ChainError(f"{method}: {data['error'].get('message', data['error'])}")
            return data["result"]
        raise ChainError(f"{method}: limit zapytań RPC – ustaw HELIUS_API_KEY")

    async def balance(self, pubkey: str) -> int:
        return (await self.call("getBalance", [pubkey, {"commitment": "processed"}]))["value"]

    async def token_accounts(self, owner: str) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for program in TOKEN_PROGRAMS:
            res = await self.call("getTokenAccountsByOwner", [owner, {"programId": program},
                                                              {"encoding": "jsonParsed", "commitment": "processed"}])
            for acc in res["value"]:
                info = acc["account"]["data"]["parsed"]["info"]
                amt = int(info["tokenAmount"]["amount"])
                if amt:
                    prev = out.get(info["mint"], (0, 0))[0]
                    out[info["mint"]] = (prev + amt, int(info["tokenAmount"]["decimals"]))
        return out

    async def token_balance(self, owner: str, mint: str) -> int:
        res = await self.call("getTokenAccountsByOwner", [owner, {"mint": mint},
                                                          {"encoding": "jsonParsed", "commitment": "processed"}])
        return sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in res["value"])

    async def transaction(self, sig: str) -> dict | None:
        return await self.call("getTransaction", [sig, {"encoding": "jsonParsed", "commitment": "confirmed",
                                                        "maxSupportedTransactionVersion": TX_VERSION}])

    async def send(self, tx_bytes: bytes) -> str:
        # skipPreflight: bez symulacji po stronie RPC – oszczędza ~100–300 ms
        return await self.call("sendTransaction", [base64.b64encode(tx_bytes).decode(),
                                                   {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}])

    async def send_and_confirm(self, tx_bytes: bytes, timeout: float = 45) -> tuple[str, float]:
        """Wysyła i ponawia co 400 ms aż do potwierdzenia. Zwraca (podpis, czas do potwierdzenia w s)."""
        t0 = time.monotonic()
        sig = await self.send(tx_bytes)
        last_send = time.monotonic()
        while time.monotonic() - t0 < timeout:
            await asyncio.sleep(0.4)
            st = (await self.call("getSignatureStatuses", [[sig]]))["value"][0]
            if st and st.get("err"):
                raise ChainError(f"transakcja odrzucona przez sieć: {st['err']}")
            if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                return sig, time.monotonic() - t0
            if time.monotonic() - last_send >= 0.4:  # rebroadcast – zwiększa szansę szybkiego wejścia do bloku
                try:
                    await self.send(tx_bytes)
                except ChainError:
                    pass
                last_send = time.monotonic()
        raise ChainError("brak potwierdzenia w 45 s (blockhash wygasł) – sprawdź saldo")

    async def transfer_sol(self, kp: Keypair, dest: str, lamports: int) -> str:
        bh = (await self.call("getLatestBlockhash", [{"commitment": "confirmed"}]))["value"]["blockhash"]
        ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=Pubkey.from_string(dest), lamports=lamports))
        tx = Transaction.new_signed_with_payer([ix], kp.pubkey(), [kp], Hash.from_string(bh))
        sig, _ = await self.send_and_confirm(bytes(tx))
        return sig


class Jupiter:
    def __init__(self, base: str, api_key: str, session: aiohttp.ClientSession):
        self.base, self.session = base.rstrip("/"), session
        self.headers = {"x-api-key": api_key} if api_key else {}

    async def _request(self, method: str, path: str, **kw) -> tuple[int, dict]:
        """Zapytanie z krótkimi ponowieniami przy limicie (429) – zamiast od razu tracić transakcję."""
        delay = 0.15
        for attempt in range(4):
            async with self.session.request(method, f"{self.base}{path}", headers=self.headers, **kw) as r:
                data = await r.json(content_type=None)
                status = r.status
            limited = status == 429 or "Too many requests" in str(data)
            if not limited or attempt == 3:
                return status, data
            await asyncio.sleep(delay)
            delay *= 2
        return status, data

    async def quote(self, in_mint: str, out_mint: str, amount: int, slippage_bps: int) -> dict:
        params = {"inputMint": in_mint, "outputMint": out_mint, "amount": str(amount),
                  "slippageBps": str(slippage_bps), "restrictIntermediateTokens": "true"}
        status, data = await self._request("GET", "/swap/v1/quote", params=params)
        if status != 200 or "outAmount" not in data:
            raise ChainError(f"Jupiter: {data.get('error') or data.get('message') or status}")
        return data

    async def swap(self, quote: dict, kp: Keypair, priority_max_lamports: int) -> bytes:
        body = {"quoteResponse": quote, "userPublicKey": str(kp.pubkey()), "wrapAndUnwrapSol": True,
                # stały limit CU zamiast symulacji po stronie Jupitera – szybsze budowanie transakcji
                "dynamicComputeUnitLimit": False, "skipUserAccountsRpcCalls": False,
                "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {
                    "maxLamports": priority_max_lamports, "priorityLevel": "veryHigh"}}}
        _, data = await self._request("POST", "/swap/v1/swap", json=body)
        if "swapTransaction" not in data:
            raise ChainError(f"Jupiter swap: {data.get('error') or data}")
        raw = VersionedTransaction.from_bytes(base64.b64decode(data["swapTransaction"]))
        return bytes(VersionedTransaction(raw.message, [kp]))

    async def token(self, mint: str) -> dict | None:
        _, data = await self._request("GET", "/tokens/v2/search", params={"query": mint})
        for t in data if isinstance(data, list) else []:
            if t.get("id") == mint:
                return {"symbol": t.get("symbol") or mint[:4], "decimals": int(t.get("decimals") or 0)}
        return None
