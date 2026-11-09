# Copy Trading Bot (Telegram) – wersja prywatna

Prywatny bot do copytradingu dla kilku osób, np. Ciebie i kumpla. **Dodajesz traderów, wybierasz, kogo kopiujesz,
a kogo tylko obserwujesz. Bot w około sekundę powtarza ich transakcje na Twoim koncie, na wybranej przez Ciebie giełdzie.**

## Co potrafi

- **🔒 Dostęp na zaproszenie.** Pierwsza osoba, która napisze `/start`, zostaje właścicielem. Każda kolejna dostaje
  „bot prywatny”, a właściciel przycisk **✅ Wpuść / ❌ Odrzuć**. Dostęp zabiera się w **🛠 Admin**.
- **➕ Dodawanie traderów** (każda osoba z dostępem, w 3 krokach):
  - **📡 konto na giełdzie**: klucz API *tylko do odczytu*, a bot widzi transakcje w czasie rzeczywistym (WebSocket);
  - **✍️ sygnały w bocie**: `/sygnal buy BTC/USDT 10` (kup za 10% salda), `/sygnal sell BTC/USDT 100`.
- **👥 Śledzenie wielu traderów.** Przy każdym wybierasz **✅ kopiuj** albo **👁 tylko powiadomienia**. Kopiować możesz
  kilku naraz. Tradera można przemianować albo usunąć (robi to osoba, która go dodała, albo właściciel).
- **🏠 Czytelny pulpit:** stan kopiowania, tryb, wartość portfela, kogo kopiujesz. Ekrany: Portfel, Traderzy,
  Historia, Ustawienia (pogrupowane), Giełda, Admin.
- **Wyniki traderów** z portfela wzorcowego (każde zlecenie 1:1): 30 dni, od startu, największy spadek, trafność.
- **🔁 8 giełd:** Binance, Bybit, OKX, Coinbase, KuCoin, Bitget, Gate.io i MEXC. Pary dopasowują się między
  stablecoinami, więc BTC/USDT u tradera to BTC/USD na Coinbase.
- **🪞 Lustro portfela 1:1** (⚙️ Ustawienia → Sposób kopiowania). Bot cały czas porównuje Twój portfel z portfelem
  tradera i trzyma te same proporcje (np. 50% BTC / 20% ETH / 30% USDT). Domyślnie **kopiuje też od razu każdą
  transakcję** tradera, 1:1 (bez mnożnika i limitu), a potem dorównuje resztę. Wyrównuje zaraz po każdym ruchu tradera
  i co `MIRROR_MINUTES` minut, więc nadrabia też to, co w zwykłym trybie by umknęło. Odchyleń poniżej tolerancji
  (1/2/5%) nie poprawia, żeby nie przepalać prowizji. Rusza tylko monety tradera i te kupione przez bota.
  Ekran **🔍 Porównaj z traderem** pokazuje Twoje % obok % tradera i zgodność.
- **Model kopiowania: procent portfela.** Trader kupi za 20% salda, to Ty kupisz za 20% (× mnożnik, maks. limit).
  Sprzeda połowę, to Ty też sprzedasz połowę.
- **🛡 Ochrona:** stop-loss portfela, ochrona ceny, limit na zakup, „🧹 Sprzedaj wszystko”, globalna pauza.
- **🟣 Kopiowanie portfeli Solany** (traderzy z Axiom, Pump.fun, GMGN…): w **➕ Dodaj tradera** wybierz
  „🟣 Portfel Solana” i wklej adres. Bot nasłuchuje portfel przez **Helius** i kopiuje wymiany przez **Jupitera**:
  stała kwota albo ten sam % SOL, sprzedaż proporcjonalna, limit na zakup, poślizg, demo/LIVE, wypłata i eksport
  klucza. Każde powiadomienie pokazuje czas „⚡ X ms od wykrycia”. Obsługiwany jest też nowy format transakcji (wersja 1).
  Opóźnienie (zmierzone na mainnecie, darmowy plan Helius i Jupiter): **wykrycie ok. 75 ms** od potwierdzenia
  transakcji tradera, **zlecenie po ok. 250 ms** od sygnału.
  - płatny plan Helius: Enhanced WebSocket na statusie *processed*, pełna transakcja w powiadomieniu
    (jeszcze ok. 0,5–1 s wcześniej); darmowy: `logsSubscribe` na *confirmed* i jedno `getTransaction`;
  - logi filtrowane po programach giełd (Jupiter, Pump.fun, PumpSwap, Raydium, Meteora, Orca, Moonshot), więc bot
    nie pobiera setek zbędnych transakcji z portfeli botów;
  - kopie spóźnione bardziej niż `SOL_MAX_DELAY_S` (10 s) są pomijane, bo cena memecoina mogła już uciec;
  - wspólna wycena Jupitera, gdy kilka osób kupuje za tę samą kwotę (oszczędza limit: darmowy klucz to 10 zapytań / 10 s);
  - wykonanie: wycena i budowa transakcji w Jupiterze (`JUPITER_API_KEY` = wyższe limity), wysyłka bez sprawdzania
    wstępnego, ponawianie co 400 ms aż do potwierdzenia, równolegle dla wszystkich kopiujących.
- **🟣 Pulpit główny w stylu Axiom (copytrading Solany).** `/start` i `/menu` otwierają: cena SOL, saldo, pozycje,
  wynik, **kopiowane portfele** (karta z wynikami, kopiuj / tylko powiadomienia, **osobna kwota zakupu dla każdego
  portfela**), ostatnie ruchy z czasem reakcji, pozycje z PnL, trendy, automatyczne take-profit / stop-loss.
  Wklej adres portfela, żeby go kopiować, albo adres tokena (CA), żeby dostać kartę tokena (MC, płynność, holderzy,
  zmiany 5m/1h/24h, audyt) z kupnem 0.05/0.1/0.5/1/X SOL i sprzedażą 25/50/100%.
  Giełdy (CEX) są na drugim pulpicie: **🏦 Giełdy (CEX) →**.
- **📊 Raport sesji**: sesja trwa od włączenia ▶️ do wyłączenia ⏸ kopiowania (albo stop-lossa). Po jej końcu bot sam
  wysyła raport: wynik portfela, transakcje, obrót, trafność, zrealizowany i niezrealizowany PnL, najlepsza i najgorsza
  transakcja, szybkość kopii (ms), wyniki według portfeli, powody pominięć i podsumowanie giełd.
  Podgląd w trakcie: przycisk na pulpicie albo `/raport`.
- **🎮 Handel demo**: ręczne kupno i sprzedaż za wirtualne USDT po prawdziwych cenach. Przyciski: kup za 10/50/100 USDT
  lub 25/50/100% gotówki, sprzedaj 25/50/100%. Komendy: `/kup PEPE 20`, `/sprzedaj BTC 50%`.
- **📌 Na żywo u góry czatu**: przypięta wiadomość odświeżana co `LIVE_SECONDS` s, z cenami (`LIVE_COINS`) i zmianą 24 h,
  wartością portfela, wynikiem, stanem kopiowania i ostatnią transakcją. Pulpit też pokazuje ceny u góry.
- **🌙 Raport dzienny**, **📈 panel Mini App** (portfel, mój trader, ranking), **🧪 tryb demo** na prawdziwych cenach.

## Uruchomienie

**Krok 1 – utwórz bota (2 minuty, z telefonu):** w Telegramie otwórz [@BotFather](https://t.me/BotFather),
wyślij `/newbot`, podaj nazwę i login kończący się na `bot`. Skopiuj **token**, który dostaniesz.

**Krok 2 – uruchom go:**

- **Serwer 24/7 (zalecane)** – dowolny VPS z Ubuntu/Debian (np. mikr.us, Hetzner, OVH).
  Wgraj i rozpakuj ten folder na serwerze, wejdź do niego i uruchom:
  ```bash
  sudo bash install.sh
  ```
  Instalator zapyta o token (i opcjonalnie klucze lidera), zainstaluje wszystko i ustawi autostart.
- **Własny komputer (do testów, działa tylko gdy komputer jest włączony):**
  Windows – kliknij dwa razy `start.bat` (potrzebny Python z python.org).
  Mac/Linux – `bash start.sh`.

**Krok 3:** otwórz swojego bota w Telegramie i napisz `/start`. **Pierwsza osoba zostaje adminem.**

Klucz szyfrujący (`secret.key`) i baza tworzą się same. **Rób kopię `secret.key`**, bo bez niego
zapisane klucze API użytkowników przestaną działać.

Docker (alternatywa):

```bash
docker build -t copybot . && docker run -d --env-file .env -v copybot-data:/data --restart unless-stopped copybot
```

## Komendy

| | |
|---|---|
| `/start`, `/menu` | pulpit z przyciskami |
| `/traderzy` | lista traderów: kopiuj / obserwuj / szczegóły |
| `/dodaj` | dodaj tradera (konto giełdowe albo sygnały) |
| `/sygnal buy BTC/USDT 10 [#id]` | sygnał dla tradera typu „sygnały w bocie” |
| `/kup BTC 50` · `/sprzedaj BTC 50%` | handel demo (wirtualne środki) |
| `/polacz` | podłącz giełdę (klucze są szyfrowane, wiadomości od razu kasowane) |
| `/mnoznik 1.5` · `/limit 100` · `/stoploss 25` | własne ustawienia |
| `/admin` | właściciel: osoby z dostępem, pauza |
| `/ogloszenie tekst` | właściciel: wiadomość do wszystkich |
| `/anuluj` | przerwij bieżącą czynność |

## Struktura

```
copybot/
  sizing.py   – czysta logika wielkości zleceń (testowana)
  stats.py    – wynik, trafność, największy spadek (testowane)
  engine.py   – wykonywanie zleceń (demo + LIVE), rynki wielu giełd, ochrona ceny, stop-loss
  watchers.py – obserwatorzy kont traderów: WebSocket + zapasowe odpytywanie
  traders.py  – lista, profil, kopiuj/obserwuj, dodawanie, zmiana nazwy, usuwanie, sygnały
  mirror.py   – lustro portfela 1:1: porównanie proporcji i wyrównywanie
  demo.py     – handel demo (ręczne kupno/sprzedaż)
  live.py     – przypięta wiadomość „na żywo” u góry czatu
  sol/        – Solana: nasłuch portfeli (Helius), wymiany (Jupiter), silnik, ekran w Telegramie
  reports.py  – raporty: wyniki lidera, portfel użytkownika
  jobs.py     – zadania okresowe: wartości portfeli, stop-loss, wygasanie subskrypcji
  billing.py  – subskrypcje w Telegram Stars
  web.py      – serwer panelu + weryfikacja podpisu Telegrama
  webapp/     – strona panelu (Mini App)
  bot.py      – interfejs Telegram
  db.py       – SQLite: użytkownicy, saldo demo, historia
  security.py – szyfrowanie kluczy API
  main.py     – start, harmonogram sprawdzania lidera
tests/        – pytest
```

## Ograniczenia

- Tylko **spot** i zlecenia rynkowe, bez dźwigni.
- Użytkownik kopiuje jednego tradera naraz. Po zmianie tradera pozycje od poprzedniego zostają (zamkniesz je przyciskiem 🧹).
- Nieudane kopiowanie (np. brak środków) nie jest ponawiane. To celowe, bo ponowienie mogłoby zdublować zlecenie.
- Jeśli bot był wyłączony dłużej niż 15 minut, transakcji tradera z tego czasu nie kopiuje (byłyby po nieaktualnych cenach).

## ⚠️ Prawo i ryzyko

Jeśli bot ma kopiować transakcje **cudzym ludziom za pieniądze** (abonament, prowizja od zysku),
w UE/Polsce może to wymagać licencji (MiCA/CASP, nadzór KNF). Zanim wpuścisz płacących użytkowników, skonsultuj to z prawnikiem.
Nie obiecuj zysków w reklamach. Bot nie jest doradcą inwestycyjnym.
