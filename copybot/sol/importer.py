"""Import list portfeli: eksport z Axiom (Wallet Tracker → Export), GMGN, zwykła lista adresów.

Działa też na uciętym tekście (np. za długie wklejenie) – wyciąga wszystko, co da się odczytać.
Zwraca (adres, nazwa, emoji, priorytet) – priorytet ⭐ to w Axiom portfele z wyskakującymi alertami.
"""
import json
import re

from .ui import valid_address

ADDR = r"[1-9A-HJ-NP-Za-km-z]{32,44}"
KEYS = ("trackedWalletAddress", "walletAddress", "wallet_address", "address", "wallet", "addr")
NAME_KEYS = ("name", "label", "nickname", "tag", "remark")
STR = r'"((?:[^"\\]|\\.)*)"'
# "trackedWalletAddress": "…", "name": "…", "emoji": "…", "alertsOnToast": true – kolejność pól z eksportu Axiom
AXIOM_RE = re.compile(r'"(?:trackedWalletAddress|walletAddress|address|wallet)"\s*:\s*"(' + ADDR + r')"'
                      r'(?:\s*,\s*"name"\s*:\s*' + STR + r')?(?:\s*,\s*"emoji"\s*:\s*' + STR + r')?'
                      r'(?:\s*,\s*"alertsOnToast"\s*:\s*(true|false))?')


def _clean(s) -> str | None:
    if not isinstance(s, str):
        return None
    s = s.strip()[:40]
    return s or None


def _prio(o: dict) -> bool:
    return bool(o.get("alertsOnToast") or o.get("priority") or o.get("starred") or o.get("favorite"))


def _from_obj(o) -> list[tuple]:
    out = []
    if isinstance(o, dict):
        addr = next((o[k] for k in KEYS if isinstance(o.get(k), str)), None)
        if addr and valid_address(addr):
            return [(addr, _clean(next((o[k] for k in NAME_KEYS if o.get(k)), None)), _clean(o.get("emoji")),
                     _prio(o))]
        for v in o.values():
            if isinstance(v, (list, dict)):
                out += _from_obj(v)
    elif isinstance(o, list):
        for v in o:
            if isinstance(v, (list, dict)):
                out += _from_obj(v)
            elif isinstance(v, str) and valid_address(v):
                out.append((v, None, None, False))
    return out


def _unescape(s: str | None) -> str | None:
    if not s:
        return None
    try:
        return _clean(json.loads(f'"{s}"'))
    except ValueError:
        return _clean(s)


def parse_wallets(text: str) -> list[tuple]:
    """[(adres, nazwa, emoji, priorytet)] bez duplikatów, w kolejności z pliku."""
    text = (text or "").strip().lstrip("﻿")
    items: list[tuple] = []
    try:
        items = _from_obj(json.loads(text))
    except ValueError:
        pass
    if not items:  # ucięty JSON albo inny format – szukamy wzorca Axiom, potem gołych adresów
        items = [(a, _unescape(n), _unescape(e), p == "true") for a, n, e, p in AXIOM_RE.findall(text)
                 if valid_address(a)]
    if not items:
        for line in text.replace(",", "\n").splitlines():
            parts = line.strip().split(maxsplit=1)
            if parts and valid_address(parts[0]):
                items.append((parts[0], _clean(parts[1]) if len(parts) > 1 else None, None, False))
    seen, out = set(), []
    for it in items:
        if it[0] not in seen:
            seen.add(it[0])
            out.append(it)
    return out[:2000]


def looks_like_import(text: str) -> bool:
    t = (text or "").lstrip()
    return t.startswith(("[", "{")) or "trackedWalletAddress" in t
