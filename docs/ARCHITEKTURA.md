# Mapa kodu – jak zbudowany jest bot

Bot Telegram + aplikacja (Telegram Mini App) do kopiowania traderów: **Solana** (memecoiny, główna część)
oraz giełdy CEX (starsza część). Wdrażany na Railway z gałęzi `main`.

## Przepływ jednej kopii (Solana)

```
sieć Solana ──► sol/watcher.py ──► sol/parser.py ──► sol/engine.py ──► sol/chain.py ──► sieć
 (WebSocket       wykrywa ruch      rozpoznaje       filtry, kwota,     Jupiter / PumpPortal /
  logsSubscribe)  tradera ~30 ms    kupno/sprzedaż   ćwiczenia/prawdziwe   Jito, potwierdzenie
                                                          │
                                                          ├─► sol_trades (baza) ──► aplikacja / raporty
                                                          └─► powiadomienie w Telegramie
```

## Pliki – Solana (`copybot/sol/`)

| Plik | Co robi |
|---|---|
| `watcher.py` | nasłuch portfeli traderów (WebSocket), ponowne łączenie, nadrabianie ruchów po przerwie |
| `parser.py` | z transakcji odczytuje: kto, kupno/sprzedaż, token, ile SOL/USDC, jaki % salda |
| `engine.py` | **serce kopiowania**: filtry (`guard_buy`), kwota (`buy_amount`), wykonanie (`execute`), ćwiczenia (`paper_*`), portfel wzorcowy tradera (`_reference`), wyniki |
| `chain.py` | klienci sieci: RPC (Helius), Jupiter (wyceny, wymiany, limit zapytań), Raptor (druga droga – gdy Jupiter zajęty), DexScreener (ceny poza limitem Jupitera), PumpPortal, Jito (kilka regionów naraz) |
| `axiom.py` | pulpit w czacie, ręczne kupno/sprzedaż, **automatyczna sprzedaż** (`tpsl_job`: TP/SL, drabinka zysku, ruchomy stop, wyjście po czasie, ucieczka twórcy) |
| `api.py` | API aplikacji (`/api/sol/*`): stan, akcje (`h_action`), traderzy, ranking, raporty, admin |
| `autocfg.py` | **✨ Ustaw za mnie / 🤖 Auto** – kwoty pod saldo, osobny % dla każdego tradera, poziomy ryzyka; nie rusza bezpieczników (`SAFETY_KEYS`) |
| `autopilot.py` | **🚀 autopilot zarobku** – dobór i rotacja traderów, zadania jednorazowe po wdrożeniach |
| `leaderboard.py` | ranking traderów: światowy (Solana Tracker / Vybe) + liczony przez bota z łańcucha; nazwy z rankingu |
| `research.py` | badanie portfela tradera z łańcucha (zysk, skuteczność, czas trzymania) |
| `optimizer.py` | 🧪 optymalizator – odtwarza ruchy traderów z różnymi ustawieniami |
| `risk.py` | ocena ryzyka tokena, filtr pułapek |
| `session.py` | raport sesji (od włączenia do wyłączenia kopiowania) |
| `extras.py` | kopia zapasowa bazy, alerty, raporty dnia/tygodnia i `/raporty`, eksport CSV, inteligentne kwoty, PIN |
| `admin.py` | panel admina (osoby, podgląd, kopiowanie cudzych traderów) + **diagnostyka kont w logach** (`DIAG …` po każdym starcie) |
| `ui.py` | ustawienia i przyciski w czacie Telegrama |
| `importer.py` | import listy portfeli z Axiom |
| `events.py` | odświeżanie aplikacji na żywo (long-poll) |

## Pliki – wspólne (`copybot/`)

| Plik | Co robi |
|---|---|
| `main.py` | start bota: usługi, rejestracja handlerów, **wszystkie zadania cykliczne i jednorazowe w jednym miejscu** |
| `db.py` | baza SQLite: tabele, migracje kolumn (`MIGRATIONS`), zapytania |
| `config.py` | ustawienia ze zmiennych środowiskowych (Railway) |
| `bot.py` | wspólne komendy i menu Telegrama, role (właściciel / admin) |
| `web.py` | serwer aplikacji (gzip, ETag), autoryzacja Telegram WebApp |
| `webapp/sol.html` | **cała aplikacja** (jeden plik: style, widoki, logika) |
| `engine.py`, `watchers.py`, `mirror.py`, `traders.py`, `demo.py` … | kopiowanie na giełdach CEX (starsza część) |

## Aplikacja (`webapp/sol.html`) – układ

- **Pulpit** – saldo, wykres, status bota, kopiowani traderzy (rozwijani), ostatnie ruchy
- **Traderzy** – Moi traderzy (lista → narzędzia → dodawanie) / 🌍 Ranking top 100 (świat / od bota, filtry łączone)
- **Tokeny** – sprawdzanie tokena, moje tokeny, popularne
- **Historia** – wynik dnia, raport sesji, 📑 raporty, CSV, wszystkie ruchy
- **Czat** – czat grupowy
- **Opcje** – podsumowanie na górze, potem grupy: 🤖 Automatyka · 💵 Kupowanie · 🛡 Sprzedaż i ochrona · 👤 Konto
- **Admin** – panel admina (tylko admini)

## Zasady

- **🛡 Bezpieczniki ustawia tylko człowiek** – automaty (✨, 🤖, 🚀) ich nie zmieniają (`autocfg.SAFETY_KEYS`).
- **Ćwiczenia = prawdziwe warunki**: te same opłaty, depozyty kont, brak zakupów bez trasy wymiany (poza
  platformami startowymi), sprzedaż nigdy więcej niż posiadane tokeny.
- **Sekrety tylko w zmiennych Railway** – nigdy w repozytorium.
- Testy: `python3 -m pytest -q`, kontrola kodu: `python3 -m pyflakes copybot`.
