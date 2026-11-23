"""Solana RPC (Helius) i Jupiter – nastawione na minimalne opóźnienie."""
import asyncio
import os
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
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"


def ata_address(owner: str, mint: str, program: str) -> str:
    """Adres konta tokena (Associated Token Account) – liczony lokalnie, bez pytania sieci."""
    return str(Pubkey.find_program_address(
        [bytes(Pubkey.from_string(owner)), bytes(Pubkey.from_string(program)), bytes(Pubkey.from_string(mint))],
        Pubkey.from_string(ATA_PROGRAM))[0])


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
        # „pokaż wszystkie tokeny portfela” – ciężkie zapytanie z niższym limitem u Helius (darmowy plan odrzucał je
        # przy kilku kontach naraz, a odrzucenie przerywało kopię) – osobny, wolniejszy licznik + krótka pamięć
        self.heavy = RateLimiter(float(os.getenv("SOL_RPC_HEAVY_RPS", "2")))
        self._accs: dict[str, tuple[float, asyncio.Future]] = {}
        self.auth_error: str | None = None

    async def call(self, method: str, params: list | None = None):
        heavy = method == "getTokenAccountsByOwner"
        for attempt in range(4):
            if heavy:
                await self.heavy.wait()
            await self.limit.wait()
            self._id += 1
            try:
                async with self.session.post(self.url, json={"jsonrpc": "2.0", "id": self._id, "method": method,
                                                             "params": params or []}) as r:
                    if r.status in (401, 403):
                        self.auth_error = f"HTTP {r.status}"
                    if r.status == 429:
                        await asyncio.sleep((1.0 if heavy else 0.5) * (attempt + 1))
                        continue
                    data = await r.json(content_type=None)
            except (aiohttp.ClientConnectionError, ConnectionResetError, asyncio.TimeoutError) as e:
                # zerwane połączenie z RPC (np. „Connection reset by peer”) – chwila i ponawiamy
                if attempt == 3:
                    raise ChainError(f"{method}: brak połączenia z RPC ({type(e).__name__})") from e
                await asyncio.sleep(0.3 * (attempt + 1))
                continue
            if "error" in data:
                raise ChainError(f"{method}: {data['error'].get('message', data['error'])}")
            return data["result"]
        raise ChainError(f"{method}: limit zapytań RPC – ustaw HELIUS_API_KEY")

    async def balance(self, pubkey: str) -> int:
        return (await self.call("getBalance", [pubkey, {"commitment": "processed"}]))["value"]

    async def token_accounts(self, owner: str, max_age: float = 3.0) -> dict[str, tuple[int, int]]:
        """Wszystkie tokeny portfela. Te same zapytania w ciągu kilku sekund (aplikacja, auto-sprzedaż, limit
        pozycji) = jedno zapytanie do sieci."""
        hit = self._accs.get(owner)
        if hit and time.monotonic() - hit[0] < max_age and not (hit[1].done() and hit[1].exception()):
            return dict(await asyncio.shield(hit[1]))
        fut = asyncio.ensure_future(self._token_accounts(owner))
        fut.add_done_callback(lambda f: f.cancelled() or f.exception())
        self._accs[owner] = (time.monotonic(), fut)
        return dict(await asyncio.shield(fut))

    async def _token_accounts(self, owner: str) -> dict[str, tuple[int, int]]:
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

    async def token_accounts_full(self, owner: str) -> list[dict]:
        """Każde konto tokena osobno: adres, program, mint, ilość, miejsca po przecinku, czy zamrożone."""
        out = []
        for program in TOKEN_PROGRAMS:
            res = await self.call("getTokenAccountsByOwner", [owner, {"programId": program},
                                                              {"encoding": "jsonParsed", "commitment": "confirmed"}])
            for acc in res["value"]:
                info = acc["account"]["data"]["parsed"]["info"]
                out.append({"account": acc["pubkey"], "program": program, "mint": info["mint"],
                            "raw": int(info["tokenAmount"]["amount"]), "decimals": int(info["tokenAmount"]["decimals"]),
                            "frozen": info.get("state") == "frozen"})
        return out

    async def burn_and_close(self, kp: Keypair, items: list[dict]) -> int:
        """Pył: spala resztkę tokenów (Burn) i zamyka konto (CloseAccount) – depozyt ~0,002 SOL wraca.
        Po 4 konta w transakcji. Zwraca liczbę zamkniętych."""
        owner = kp.pubkey()
        done = 0
        for i in range(0, len(items), 4):
            ixs = []
            for it in items[i:i + 4]:
                prog, acc = Pubkey.from_string(it["program"]), Pubkey.from_string(it["account"])
                if it["raw"] > 0:
                    ixs.append(Instruction(prog, bytes([8]) + int(it["raw"]).to_bytes(8, "little"),
                                           [AccountMeta(acc, False, True), AccountMeta(Pubkey.from_string(it["mint"]), False, True),
                                            AccountMeta(owner, True, False)]))
                ixs.append(Instruction(prog, bytes([9]), [AccountMeta(acc, False, True), AccountMeta(owner, False, True),
                                                          AccountMeta(owner, True, False)]))
            bh = (await self.call("getLatestBlockhash", [{"commitment": "confirmed"}]))["value"]["blockhash"]
            tx = Transaction.new_signed_with_payer(ixs, owner, [kp], Hash.from_string(bh))
            try:
                await self.send_and_confirm(bytes(tx), timeout=30)
                done += len(items[i:i + 4])
            except ChainError as e:
                log.info("Nie spalono pyłu: %s", e)
        return done

    async def token_balance(self, owner: str, mint: str) -> int:
        """Ile tokena ma portfel. Lekkie zapytanie o konto tokena (adres liczony lokalnie, obie wersje programu
        tokenów naraz); tylko gdy takiego konta nie ma – pełna lista tokenów (z pamięci kilku sekund)."""
        try:
            addrs = [ata_address(owner, mint, p) for p in TOKEN_PROGRAMS]
        except (ValueError, TypeError):
            addrs = []
        if addrs:
            res = await self.call("getMultipleAccounts", [addrs, {"encoding": "base64", "commitment": "processed"}])
            found, total = False, 0
            for v in res.get("value") or []:
                if not v:
                    continue
                data = base64.b64decode(v["data"][0])
                if len(data) >= 72:  # mint(32) + właściciel(32) + ilość(u64) – ten sam układ w Token i Token-2022
                    found, total = True, total + int.from_bytes(data[64:72], "little")
            if found:
                return total
        try:  # rzadko: tokeny na koncie innym niż standardowe
            return (await self.token_accounts(owner, max_age=10)).get(mint, (0, 0))[0]
        except ChainError as e:
            if "could not find mint" in str(e):
                return 0
            raise

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
                slots = self.__dict__.setdefault("slots", {})
                slots[sig] = int(st.get("slot") or 0)
                if len(slots) > 500:
                    slots.pop(next(iter(slots)))
                return sig, time.monotonic() - t0
            if time.monotonic() - last_send >= 0.4:  # rebroadcast – zwiększa szansę szybkiego wejścia do bloku
                rounds += 1
                again = [self.send(tx_bytes)] + ([f(tx_bytes) for f in also] if also and rounds % 3 == 0 else [])
                await asyncio.gather(*again, return_exceptions=True)
                last_send = time.monotonic()
        raise ChainError("brak potwierdzenia w 45 s (blockhash wygasł) – sprawdź saldo")

    async def balance_change(self, sig: str, owner: str, mint: str) -> tuple[int, int, bool] | None:
        """Co NAPRAWDĘ zmieniła transakcja na portfelu: (zmiana SOL w lamportach – z opłatą, napiwkiem i
        depozytem konta, zmiana tokena w jednostkach, czy utworzono nowe konto tokena). None = brak danych."""
        for delay in (0.3, 0.6, 1.0, 1.5, 2.5):
            await asyncio.sleep(delay)
            try:
                tx = await self.call("getTransaction", [sig, {"encoding": "jsonParsed", "commitment": "confirmed",
                                                              "maxSupportedTransactionVersion": TX_VERSION}])
            except ChainError:
                tx = None
            if not tx or not tx.get("meta"):
                continue
            meta, keys = tx["meta"], tx["transaction"]["message"]["accountKeys"]
            idx = next((i for i, k in enumerate(keys) if (k["pubkey"] if isinstance(k, dict) else k) == owner), None)
            if idx is None:
                return None
            sol = meta["postBalances"][idx] - meta["preBalances"][idx]

            def tok(rows):
                return sum(int(r["uiTokenAmount"]["amount"]) for r in rows or []
                           if r.get("owner") == owner and r.get("mint") == mint)
            pre_rows = [r for r in meta.get("preTokenBalances") or [] if r.get("owner") == owner and r.get("mint") == mint]
            post_rows = [r for r in meta.get("postTokenBalances") or [] if r.get("owner") == owner and r.get("mint") == mint]
            return sol, tok(post_rows) - tok(pre_rows), bool(post_rows) and not pre_rows
        return None

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


class DexPrices:
    """Ceny tokenów z DexScreenera (darmowe, ~300 zapytań/min, do 30 tokenów naraz). Cena z najpłynniejszej puli
    tokena na Solanie. Błąd = pusty wynik (wtedy cenę daje Jupiter)."""

    URL = "https://api.dexscreener.com/tokens/v1/solana/"

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.fail_until = 0.0

    async def prices(self, mints: list[str]) -> dict[str, float]:
        if not mints or time.monotonic() < self.fail_until:
            return {}
        out: dict[str, tuple[float, float]] = {}  # mint -> (płynność, cena)
        try:
            for i in range(0, len(mints), 30):
                async with self.session.get(self.URL + ",".join(mints[i:i + 30]),
                                            timeout=aiohttp.ClientTimeout(total=4)) as r:
                    if r.status != 200:
                        raise ChainError(f"DexScreener {r.status}")
                    data = await r.json(content_type=None)
                for p in data if isinstance(data, list) else []:
                    m = (p.get("baseToken") or {}).get("address")
                    if m not in mints or not p.get("priceUsd"):
                        continue
                    liq = float((p.get("liquidity") or {}).get("usd") or 0)
                    if m not in out or liq > out[m][0]:
                        out[m] = (liq, float(p["priceUsd"]))
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, ValueError, ChainError) as e:
            log.info("DexScreener: brak cen (%s) – ceny z Jupitera przez 60 s", type(e).__name__)
            self.fail_until = time.monotonic() + 60
            return {m: v[1] for m, v in out.items()}
        if not self.__dict__.get("_ok_logged"):
            self._ok_logged = True
            log.info("DexScreener: ceny działają (%d/%d tokenów) – limit Jupitera zostaje na transakcje", len(out), len(mints))
        return {m: v[1] for m, v in out.items()}


class Jupiter:
    def __init__(self, base: str, api_key: str, session: aiohttp.ClientSession):
        self.base, self.session = base.rstrip("/"), session
        self.headers = {"x-api-key": api_key} if api_key else {}
        self.auth_error: str | None = None  # 401/403 – klucz nieprawidłowy albo wygasł (alert dla admina)
        self.limit_hits: list[float] = []  # kiedy trafiliśmy w limit zapytań
        self._quotes: dict[tuple, tuple[float, asyncio.Future]] = {}  # wspólna wycena dla tych samych parametrów
        # własny licznik zapytań (okno 10 s): zakupy/sprzedaże mają pierwszeństwo, ceny do wyświetlania czekają
        # albo biorą ostatnią znaną wartość – żeby odświeżanie aplikacji nie blokowało transakcji
        self.window_limit = int(os.getenv("JUP_LIMIT_PER_10S", "10"))
        self._stamps: list[float] = []
        self._no_route: dict[tuple, float] = {}  # token „not tradable” / bez trasy – nie pytamy co chwilę
        self.dex = DexPrices(session)  # ceny do wyświetlania i automatycznej sprzedaży – poza limitem Jupitera

    def _budget(self) -> int:
        now = time.monotonic()
        self._stamps = [t for t in self._stamps if now - t < 10]
        return self.window_limit - len(self._stamps)

    def busy(self) -> bool:
        """Okno zapytań pełne – transakcja czekałaby w kolejce (do ~10 s). Wtedy lepiej od razu inną drogą."""
        return self._budget() <= 0

    async def _slot(self, bg: bool):
        """Czeka na wolne miejsce w oknie 10 s. Tło (ceny) zostawia ~40% okna dla transakcji."""
        reserve = max(int(self.window_limit * 0.4), 2) if bg else 0
        for _ in range(30 if bg else 40):  # transakcja czeka maks. ~10 s, tło ~15 s – potem i tak próbujemy
            if self._budget() > reserve:
                self._stamps.append(time.monotonic())
                return
            await asyncio.sleep(0.25 if not bg else 0.5)
        self._stamps.append(time.monotonic())

    async def _request(self, method: str, path: str, bg: bool = False, **kw) -> tuple[int, dict]:
        """Zapytanie z obsługą limitu Jupitera: przy 429 czekamy do odnowienia okna (nagłówek x-ratelimit-reset)
        zamiast tracić transakcję. Darmowy klucz: 10 zapytań / 10 s."""
        for attempt in range(5):
            await self._slot(bg)
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
        dead = self._no_route.get((in_mint, out_mint))
        if dead and now - dead[0] < 15:
            raise ChainError(dead[1])
        hit = self._quotes.get(key)
        if hit and now - hit[0] < 2.0:
            return await asyncio.shield(hit[1])
        fut = asyncio.ensure_future(self._quote(in_mint, out_mint, amount, slippage_bps, restrict))
        # gdy nikt już nie czeka (np. zakup odrzucony przez filtr), błąd wyceny nie może „wisieć” w logach
        fut.add_done_callback(lambda f: f.cancelled() or f.exception())
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
            msg = f"Jupiter: {data.get('error') or data.get('message') or status}"
            if any(x in msg for x in ("not tradable", "No routes", "COULD_NOT_FIND_ANY_ROUTE", "TOKEN_NOT_TRADABLE")):
                self._no_route[(in_mint, out_mint)] = (time.monotonic(), msg)
                if len(self._no_route) > 2000:
                    self._no_route.clear()
            raise ChainError(msg)
        self._no_route.pop((in_mint, out_mint), None)
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

        cache = self.__dict__.setdefault("_cache", {})
        key = ("px", tuple(mints))
        hit = cache.get(key)
        if hit and time.monotonic() - hit[0] < 120 and self._budget() <= max(int(self.window_limit * 0.4), 2):
            return hit[1]  # okno prawie pełne – ceny sprzed chwili wystarczą, miejsce zostaje dla transakcji

        async def fetch():
            # najpierw DexScreener (osobny, duży limit) – limit Jupitera zostaje na zakupy i sprzedaże;
            # Jupiter tylko dla tokenów, których DexScreener nie zna
            out = await self.dex.prices(mints) if self.dex is not None else {}
            rest = [m for m in mints if m not in out]
            if rest:
                _, data = await self._request("GET", "/price/v3", bg=True, params={"ids": ",".join(rest)})
                out.update({m: float(v["usdPrice"]) for m, v in (data or {}).items()
                            if isinstance(v, dict) and v.get("usdPrice")})
            return out
        return await self._cached(key, 10, fetch)

    async def trending(self, interval: str = "1h", limit: int = 10) -> list[dict]:
        async def fetch():
            _, data = await self._request("GET", f"/tokens/v2/toptrending/{interval}", bg=True, params={"limit": str(limit)})
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
        try:
            async with self.session.post(self.URL, json=body, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.read()
                if r.status != 200:
                    raise ChainError(f"PumpPortal: {data[:150].decode('utf-8', 'ignore') or r.status}")
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
            raise ChainError(f"PumpPortal niedostępny ({type(e).__name__})") from e
        try:
            raw = VersionedTransaction.from_bytes(data)
        except Exception as e:  # noqa: BLE001
            raise ChainError(f"PumpPortal: zła odpowiedź ({e})") from e
        return bytes(VersionedTransaction(raw.message, [kp]))


JITO_TIP_ACCOUNTS = ("96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5", "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
                     "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY", "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
                     "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh", "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
                     "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL", "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT")


class Raptor:
    """Druga droga wymiany (Solana Tracker Raptor, 71 giełd, bez klucza): gdy Jupiter nie ma trasy albo przytnie
    limit zapytań. Serwer buduje transakcję, my ją podpisujemy i wysyłamy własnym RPC – klucz nie opuszcza bota."""

    URL = "https://raptor.solanatracker.io"

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def _req(self, method: str, path: str, **kw) -> dict:
        try:
            async with self.session.request(method, self.URL + path, timeout=aiohttp.ClientTimeout(total=8), **kw) as r:
                data = await r.json(content_type=None)
                status = r.status
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, ValueError) as e:
            raise ChainError(f"Raptor niedostępny ({type(e).__name__})") from e
        if status != 200 or not isinstance(data, dict) or data.get("error"):
            raise ChainError(f"Raptor: {(data or {}).get('error') or (data or {}).get('message') or status}")
        return data

    async def quote(self, in_mint: str, out_mint: str, amount: int, slippage_bps: int) -> dict:
        q = await self._req("GET", "/quote", params={"inputMint": in_mint, "outputMint": out_mint, "amount": str(amount),
                                                     "slippageBps": str(slippage_bps)})
        if not int(q.get("amountOut") or 0):
            raise ChainError("Raptor: brak trasy")
        return q

    async def swap(self, quote: dict, kp: Keypair, tip_lamports: int, jito: bool) -> bytes:
        body = {"userPublicKey": str(kp.pubkey()), "quoteResponse": quote, "txVersion": "V0", "wrapUnwrapSol": True,
                "priorityFee": "auto"}
        if jito:
            import random
            body.update(tipAccount=random.choice(JITO_TIP_ACCOUNTS), tipLamports=int(tip_lamports))
        data = await self._req("POST", "/swap", json=body)
        if not data.get("swapTransaction"):
            raise ChainError("Raptor: brak transakcji")
        raw = VersionedTransaction.from_bytes(base64.b64decode(data["swapTransaction"]))
        return bytes(VersionedTransaction(raw.message, [kp]))


class Jito:
    """Wysyłka transakcji przez Jito Block Engine – trafia bezpośrednio do walidatorów Jito (ok. 90% sieci).
    Transakcja musi zawierać napiwek dla Jito (dokłada go Jupiter w trybie turbo)."""

    REGIONS = ("amsterdam", "frankfurt", "ny", "slc")

    def __init__(self, url: str, session: aiohttp.ClientSession):
        self.session = session
        urls = [u.strip() for u in url.split(",") if u.strip()]
        if len(urls) == 1 and ".block-engine.jito.wtf" in urls[0]:
            # ta sama transakcja do kilku regionów naraz – wchodzi przez ten, który najszybciej trafi do lidera
            # bloku; limit zapytań Jito liczy się osobno w każdym regionie
            path = urls[0].split(".block-engine.jito.wtf", 1)[1]
            urls += [f"https://{r}.mainnet.block-engine.jito.wtf{path}" for r in self.REGIONS]
        self.urls = list(dict.fromkeys(urls))
        self.url = self.urls[0]

    async def _send_one(self, url: str, body: dict) -> str:
        try:
            async with self.session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=5)) as r:
                data = await r.json(content_type=None)
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, ValueError) as e:
            raise ChainError(f"Jito niedostępne ({type(e).__name__})") from e
        if not isinstance(data, dict) or "error" in data:
            err = (data or {}).get("error") if isinstance(data, dict) else data
            raise ChainError(f"Jito: {err.get('message', err) if isinstance(err, dict) else err}")
        return data["result"]

    async def send(self, tx_bytes: bytes) -> str:
        body = {"jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                "params": [base64.b64encode(tx_bytes).decode(), {"encoding": "base64"}]}
        res = await asyncio.gather(*(self._send_one(u, body) for u in self.urls), return_exceptions=True)
        sig = next((r for r in res if isinstance(r, str)), None)
        if sig is None:
            raise res[0] if isinstance(res[0], Exception) else ChainError("Jito: nie wysłano")
        return sig
