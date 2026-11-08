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
