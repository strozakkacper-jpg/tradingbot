"""Solana RPC (Helius) i Jupiter – nastawione na minimalne opóźnienie."""
import asyncio
import base64
import logging
import time

import aiohttp
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
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


class RateLimiter:
    """Prosty limit zapytań na sekundę (darmowy Helius: ok. 10/s) – bez burz błędów 429."""

    def __init__(self, per_second: float):
        self.interval = 1 / per_second if per_second > 0 else 0
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self):
        if not self.interval:
            return
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.interval


class Rpc:
    def __init__(self, url: str, session: aiohttp.ClientSession, rps: float = 0):
        self.url, self.session = url, session
        self._id = 0
        self.limit = RateLimiter(rps)
        self.auth_error: str | None = None

    async def call(self, method: str, params: list | None = None):
        for attempt in range(4):
            await self.limit.wait()
            self._id += 1
            async with self.session.post(self.url, json={"jsonrpc": "2.0", "id": self._id, "method": method,
                                                         "params": params or []}) as r:
                if r.status in (401, 403):
                    self.auth_error = f"HTTP {r.status}"
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

    async def send_and_confirm(self, tx_bytes: bytes, timeout: float = 45, also=None) -> tuple[str, float]:
        """Wysyła i ponawia co 400 ms aż do potwierdzenia. Zwraca (podpis, czas do potwierdzenia w s).
        also: dodatkowe drogi wysyłki (np. Jito) – ta sama transakcja idzie nimi równolegle."""
        t0 = time.monotonic()
        extra = [f(tx_bytes) for f in (also or [])]
        res = await asyncio.gather(self.send(tx_bytes), *extra, return_exceptions=True)
        sig = next((r for r in res if isinstance(r, str)), None)
        if sig is None:
            raise res[0] if isinstance(res[0], Exception) else ChainError("nie wysłano transakcji")
        last_send = time.monotonic()
        rounds = 0
        while time.monotonic() - t0 < timeout:
            await asyncio.sleep(0.4)
            st = (await self.call("getSignatureStatuses", [[sig]]))["value"][0]
            if st and st.get("err"):
                raise ChainError(f"transakcja odrzucona przez sieć: {st['err']}")
            if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                return sig, time.monotonic() - t0
            if time.monotonic() - last_send >= 0.4:  # rebroadcast – zwiększa szansę szybkiego wejścia do bloku
                rounds += 1
                again = [self.send(tx_bytes)] + ([f(tx_bytes) for f in also] if also and rounds % 3 == 0 else [])
                await asyncio.gather(*again, return_exceptions=True)
                last_send = time.monotonic()
        raise ChainError("brak potwierdzenia w 45 s (blockhash wygasł) – sprawdź saldo")

    async def empty_token_accounts(self, owner: str, mint: str | None = None) -> list[tuple[str, str]]:
        """Puste konta tokenów [(adres konta, program)] – każde blokuje ~0,002 SOL depozytu."""
        out = []
        filters = [{"mint": mint}] if mint else [{"programId": p} for p in TOKEN_PROGRAMS]
        for f in filters:
            res = await self.call("getTokenAccountsByOwner", [owner, f, {"encoding": "jsonParsed",
                                                                         "commitment": "confirmed"}])
            for acc in res["value"]:
                info = acc["account"]["data"]["parsed"]["info"]
                if info["tokenAmount"]["amount"] == "0" and acc["account"]["owner"] in TOKEN_PROGRAMS:
                    out.append((acc["pubkey"], acc["account"]["owner"]))
        return out

    async def close_token_accounts(self, kp: Keypair, accounts: list[tuple[str, str]]) -> int:
        """Zamyka puste konta tokenów (instrukcja CloseAccount) – depozyt wraca do właściciela. Zwraca liczbę."""
        owner = kp.pubkey()
        closed = 0
        for i in range(0, len(accounts), 8):  # po 8 w jednej transakcji
            ixs = [Instruction(Pubkey.from_string(prog), bytes([9]),
                               [AccountMeta(Pubkey.from_string(acc), False, True), AccountMeta(owner, False, True),
                                AccountMeta(owner, True, False)]) for acc, prog in accounts[i:i + 8]]
            bh = (await self.call("getLatestBlockhash", [{"commitment": "confirmed"}]))["value"]["blockhash"]
            tx = Transaction.new_signed_with_payer(ixs, owner, [kp], Hash.from_string(bh))
            try:
                await self.send_and_confirm(bytes(tx), timeout=30)
                closed += len(ixs)
            except ChainError as e:
                log.info("Nie zamknięto kont tokenów: %s", e)
        return closed

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
        self.auth_error: str | None = None  # 401/403 – klucz nieprawidłowy albo wygasł (alert dla admina)
        self.limit_hits: list[float] = []  # kiedy trafiliśmy w limit zapytań
        self._quotes: dict[tuple, tuple[float, asyncio.Future]] = {}  # wspólna wycena dla tych samych parametrów

    async def _request(self, method: str, path: str, **kw) -> tuple[int, dict]:
        """Zapytanie z obsługą limitu Jupitera: przy 429 czekamy do odnowienia okna (nagłówek x-ratelimit-reset)
        zamiast tracić transakcję. Darmowy klucz: 10 zapytań / 10 s."""
        for attempt in range(5):
            async with self.session.request(method, f"{self.base}{path}", headers=self.headers, **kw) as r:
                data = await r.json(content_type=None)
                status = r.status
                reset = r.headers.get("x-ratelimit-reset")
            limited = status == 429 or "Too many requests" in str(data)
            if status in (401, 403):
                self.auth_error = f"{status}: {str(data)[:120]}"
            if limited:
                self.limit_hits.append(time.time())
                self.limit_hits = self.limit_hits[-500:]
            if not limited or attempt == 4:
                return status, data
            wait = 0.2 * (attempt + 1)
            if reset and reset.isdigit():
                wait = max(wait, min(int(reset) - time.time() + 0.05, 10))
            log.info("Jupiter: limit zapytań – czekam %.1f s", wait)
            await asyncio.sleep(wait)
        return status, data

    async def quote(self, in_mint: str, out_mint: str, amount: int, slippage_bps: int, restrict: bool = True) -> dict:
        """Ta sama wycena w ciągu 2 s (np. Ty i kumpel kupujecie za 0,05 SOL) = jedno zapytanie do Jupitera."""
        key = (in_mint, out_mint, amount, slippage_bps, restrict)
        now = time.monotonic()
        hit = self._quotes.get(key)
        if hit and now - hit[0] < 2.0:
            return await asyncio.shield(hit[1])
        fut = asyncio.ensure_future(self._quote(in_mint, out_mint, amount, slippage_bps, restrict))
        self._quotes[key] = (now, fut)
        if len(self._quotes) > 500:
            for k in [k for k, (t, _) in self._quotes.items() if now - t > 5]:
                self._quotes.pop(k, None)
        try:
            return await asyncio.shield(fut)
        except Exception:
            self._quotes.pop(key, None)
            raise

    async def _quote(self, in_mint: str, out_mint: str, amount: int, slippage_bps: int, restrict: bool = True) -> dict:
        params = {"inputMint": in_mint, "outputMint": out_mint, "amount": str(amount),
                  "slippageBps": str(slippage_bps), "restrictIntermediateTokens": "true" if restrict else "false"}
        status, data = await self._request("GET", "/swap/v1/quote", params=params)
        if status != 200 or "outAmount" not in data:
            raise ChainError(f"Jupiter: {data.get('error') or data.get('message') or status}")
        return data

    async def swap(self, quote: dict, kp: Keypair, priority_max_lamports: int, jito_tip: int | None = None) -> bytes:
        """jito_tip: tryb turbo – zamiast opłaty priorytetowej napiwek dla Jito (transakcja idzie wprost do
        walidatorów) i bez symulacji limitu CU po stronie Jupitera (ok. 0,1 s szybciej; przy napiwku Jito
        limit CU nie wpływa na koszt)."""
        fee = ({"jitoTipLamports": int(jito_tip)} if jito_tip else
               {"priorityLevelWithMaxLamports": {"maxLamports": priority_max_lamports, "priorityLevel": "veryHigh"}})
        body = {"quoteResponse": quote, "userPublicKey": str(kp.pubkey()), "wrapAndUnwrapSol": True,
                # bez turbo: dokładny limit CU (symulacja ~0,1 s) – opłata priorytetowa liczy się od CU
                "dynamicComputeUnitLimit": not jito_tip, "skipUserAccountsRpcCalls": bool(jito_tip),
                "prioritizationFeeLamports": fee}
        _, data = await self._request("POST", "/swap/v1/swap", json=body)
        if "swapTransaction" not in data:
            raise ChainError(f"Jupiter swap: {data.get('error') or data}")
        raw = VersionedTransaction.from_bytes(base64.b64decode(data["swapTransaction"]))
        return bytes(VersionedTransaction(raw.message, [kp]))

    async def _cached(self, key, ttl: float, coro_fn):
        cache = self.__dict__.setdefault("_cache", {})
        hit = cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        val = await coro_fn()
        cache[key] = (time.monotonic(), val)
        if len(cache) > 1000:
            for k in [k for k, (t, _) in cache.items() if time.monotonic() - t > 300]:
                cache.pop(k, None)
        return val

    async def token_full(self, mint: str) -> dict | None:
        """Pełne dane tokena: cena, MC, płynność, holderzy, zmiany 5m/1h/24h, audyt (cache 15 s)."""
        async def fetch():
            _, data = await self._request("GET", "/tokens/v2/search", params={"query": mint})
            return next((t for t in data if isinstance(data, list) and t.get("id") == mint), None)
        return await self._cached(("tok", mint), 15, fetch)

    async def prices(self, mints: list[str]) -> dict[str, float]:
        """Ceny w USD wielu tokenów jednym zapytaniem (cache 10 s)."""
        mints = sorted(set(mints))[:50]
        if not mints:
            return {}

        async def fetch():
            _, data = await self._request("GET", "/price/v3", params={"ids": ",".join(mints)})
            return {m: float(v["usdPrice"]) for m, v in (data or {}).items() if isinstance(v, dict) and v.get("usdPrice")}
        return await self._cached(("px", tuple(mints)), 10, fetch)

    async def trending(self, interval: str = "1h", limit: int = 10) -> list[dict]:
        async def fetch():
            _, data = await self._request("GET", f"/tokens/v2/toptrending/{interval}", params={"limit": str(limit)})
            return data if isinstance(data, list) else []
        return await self._cached(("trend", interval, limit), 60, fetch)

    async def token(self, mint: str) -> dict | None:
        _, data = await self._request("GET", "/tokens/v2/search", params={"query": mint})
        for t in data if isinstance(data, list) else []:
            if t.get("id") == mint:
                return {"symbol": t.get("symbol") or mint[:4], "decimals": int(t.get("decimals") or 0)}
        return None


class PumpPortal:
    """Zapasowa droga dla świeżych tokenów z Pump.fun, których Jupiter jeszcze nie zna („brak trasy”).
    Local Transaction API: serwer buduje transakcję, my ją podpisujemy i wysyłamy przez własny RPC
    (klucz nigdy nie opuszcza bota). PumpPortal pobiera za to 0,5% wartości transakcji."""

    URL = "https://pumpportal.fun/api/trade-local"

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def tx(self, kp: Keypair, action: str, mint: str, amount: float | str, in_sol: bool,
                 slippage_pct: float, priority_sol: float) -> bytes:
        body = {"publicKey": str(kp.pubkey()), "action": action, "mint": mint, "amount": amount,
                "denominatedInSol": "true" if in_sol else "false", "slippage": round(slippage_pct, 2),
                "priorityFee": round(priority_sol, 6), "pool": "auto"}
        async with self.session.post(self.URL, json=body, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.read()
            if r.status != 200:
                raise ChainError(f"PumpPortal: {data[:150].decode('utf-8', 'ignore') or r.status}")
        try:
            raw = VersionedTransaction.from_bytes(data)
        except Exception as e:  # noqa: BLE001
            raise ChainError(f"PumpPortal: zła odpowiedź ({e})") from e
        return bytes(VersionedTransaction(raw.message, [kp]))


class Jito:
    """Wysyłka transakcji przez Jito Block Engine – trafia bezpośrednio do walidatorów Jito (ok. 90% sieci).
    Transakcja musi zawierać napiwek dla Jito (dokłada go Jupiter w trybie turbo)."""

    def __init__(self, url: str, session: aiohttp.ClientSession):
        self.url, self.session = url, session

    async def send(self, tx_bytes: bytes) -> str:
        body = {"jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [base64.b64encode(tx_bytes).decode(), {"encoding": "base64"}]}
        async with self.session.post(self.url, json=body, timeout=aiohttp.ClientTimeout(total=5)) as r:
            data = await r.json(content_type=None)
        if "error" in data:
            raise ChainError(f"Jito: {data['error'].get('message', data['error'])}")
        return data["result"]
