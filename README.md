# Copy Trading Bot (Telegram) – wersja 2

Platforma copytradingu w Telegramie, działająca jak copy trading na dużych giełdach, tylko u Ciebie:
**użytkownicy wybierają traderów z rankingu, a bot w około sekundę powtarza ich transakcje na kontach
obserwujących, na wybranej przez każdego giełdzie.**

## Co potrafi

- **🏆 Ranking traderów.** Każdy trader ma publiczne wyniki (30 dni, od startu, największy spadek,
  trafność), wykres i liczbę kopiujących. Wyniki liczy *portfel wzorcowy*, który kopiuje każde zlecenie 1:1,
  więc nie da się ich podkręcić. Kolejność to wynik z 30 dni minus kara za duży spadek.
- **🎓 Zostań traderem** (`/zostan_traderem`). Trader podpina klucz **tylko do odczytu** albo wysyła sygnały
  w bocie (`/sygnal buy BTC/USDT 10`). Admin zatwierdza zgłoszenie jednym przyciskiem. Trader ma swój panel `/trader`.
- **🔁 Wiele giełd:** Binance, Bybit, OKX, KuCoin, Bitget, Gate.io i MEXC (lista w `EXCHANGES`). Każdy użytkownik
  i każdy trader wybiera własną giełdę. Trader może handlować na Binance, a kopiujący na Bybit, bo bot kopiuje
  tę samą parę. Jeśli pary nie ma na giełdzie obserwującego, ten dostaje informację i zlecenie jest pomijane.
- **⚡ Czas rzeczywisty.** Bot nasłuchuje transakcji tradera przez WebSocket giełdy (`watchMyTrades`), z opóźnieniem
  około 1 s. Gdy połączenie padnie, sam przechodzi na odpytywanie i po 2 minutach znów próbuje WebSocketu.
  Pary odkrywa sam z salda tradera, więc nic nie trzeba wpisywać.
- **Model kopiowania: procent portfela.** Trader kupi za 20% swoich USDT, to obserwujący kupi za 20% swoich
  (× mnożnik, maks. limit na zlecenie). Trader sprzeda połowę BTC, to obserwujący też sprzeda połowę.
- **🛡 Ochrona.** Stop-loss portfela, ochrona ceny (bot nie kupi, jeśli cena uciekła o więcej niż X% od ceny
  tradera), limit na zakup, przycisk „🧹 Sprzedaj wszystko”, globalna pauza admina.
- **🌙 Raport dzienny** o 20:00: wartość portfela, zmiana z 24 h, liczba transakcji.
- **📈 Panel (Telegram Mini App)** z trzema zakładkami: Mój portfel, Mój trader i Ranking (z przyciskiem „Kopiuj”).
- **💎 Subskrypcja w Telegram Stars** (opcjonalnie), **🧪 tryb demo** na prawdziwych cenach.

Przy pierwszym starcie dane z wersji 1 przenoszą się same: dotychczasowy lider staje się traderem #1.

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

| Użytkownik | |
|---|---|
| `/start`, `/menu` | menu: portfel, traderzy, start/stop, ustawienia, historia, giełda, demo/LIVE |
| `/traderzy`, `/wyniki` | ranking traderów i wybór, kogo kopiować |
| `/polacz` | wybór giełdy i podłączenie kluczy API (wiadomości z kluczami są od razu kasowane) |
| `/zostan_traderem`, `/trader` | zgłoszenie i panel tradera |
| `/mnoznik 1.5`, `/limit 100`, `/stoploss 20` | własne ustawienia |
| `/wyniki` | wyniki lidera |
| `/subskrypcja`, `/paysupport` | subskrypcja i pomoc w płatnościach |

| Admin | |
|---|---|
| `/admin` | statystyki |
| `/traderzy` | lista traderów z przyciskami zatwierdź/ukryj |
| `/pauza`, `/wznow` | wstrzymanie kopiowania dla wszystkich |
| `/sygnal buy BTC/USDT 10 [#id]` | sygnał tradera (właściciel profilu albo admin): kupno za 10% salda |
| `/sygnal sell BTC/USDT 100 [#id]` | sprzedaż całej pozycji |
| `/ogloszenie tekst` | wiadomość do wszystkich |
| `/zwrot ID` | zwrot ostatniej płatności użytkownika w Stars |

## Struktura

```
copybot/
  sizing.py   – czysta logika wielkości zleceń (testowana)
  stats.py    – wynik, trafność, największy spadek (testowane)
  engine.py   – wykonywanie zleceń (demo + LIVE), rynki wielu giełd, ochrona ceny, stop-loss
  watchers.py – obserwatorzy kont traderów: WebSocket + zapasowe odpytywanie
  traders.py  – ranking, profile, zgłoszenia traderów, sygnały
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
