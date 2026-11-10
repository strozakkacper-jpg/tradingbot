"""Ocena ryzyka tokena (🟢/🟡/🔴) z danych Jupitera – z wyjaśnieniami dla laika."""


def usd(x: float | None) -> str:
    if x is None:
        return "—"
    if x >= 1_000_000_000:
        return f"${x / 1e9:.2f}B"
    if x >= 1_000_000:
        return f"${x / 1e6:.2f}M"
    if x >= 1_000:
        return f"${x / 1e3:.1f}K"
    if x >= 1:
        return f"${x:.2f}"
    from ..engine import fmt_price
    return f"${fmt_price(x)}"


def risk(info: dict) -> tuple[str, list[str]]:
    """Prosta ocena ryzyka tokena (🟢/🟡/🔴) + wyjaśnienia dla laika. To podpowiedź, nie gwarancja."""
    a = info.get("audit") or {}
    score, notes = 0, []
    if a.get("mintAuthorityDisabled") is not None:
        if a["mintAuthorityDisabled"]:
            notes.append("✅ mint wył. – nikt nie dodrukuje nowych tokenów")
        else:
            score += 2
            notes.append("⚠️ twórca może dodrukować tokeny (Twoje stracą wartość)")
    if a.get("freezeAuthorityDisabled") is not None:
        if a["freezeAuthorityDisabled"]:
            notes.append("✅ nikt nie może zamrozić Twoich tokenów")
        else:
            score += 2
            notes.append("⚠️ twórca może zamrozić Twoje tokeny (nie sprzedasz ich)")
    liq = info.get("liquidity")
    if liq is not None:
        if liq < 10_000:
            score += 2
            notes.append(f"⚠️ bardzo mało pieniędzy w puli ({usd(liq)}) – sprzedaż może mocno zbić cenę")
        elif liq < 50_000:
            score += 1
            notes.append(f"🟡 niewiele pieniędzy w puli ({usd(liq)})")
    holders = info.get("holderCount")
    if holders is not None:
        if holders < 100:
            score += 2
            notes.append("⚠️ bardzo mało posiadaczy – token jest świeży albo nikt go nie chce")
        elif holders < 1000:
            score += 1
    top = a.get("topHoldersPercentage")
    if top is not None:
        if top > 50:
            score += 2
            notes.append(f"⚠️ 10 największych portfeli ma {top:.0f}% tokenów – mogą nagle sprzedać i zbić cenę")
        elif top > 30:
            score += 1
            notes.append(f"🟡 10 największych portfeli ma {top:.0f}% tokenów")
    level = "🟢 niskie" if score <= 1 else "🟡 średnie" if score <= 3 else "🔴 wysokie"
    return level, notes


def is_risky(info: dict | None) -> bool:
    return bool(info) and risk(info)[0].startswith("🔴")


def trap_reason(info: dict | None) -> str | None:
    """Prawdziwa pułapka (powód) albo None. Świeży token z Pump.fun ma zawsze małą pulę i mało posiadaczy,
    a bonding curve trzyma większość podaży – to NIE są pułapki (a właśnie takie tokeny robią ×10).
    Pułapka to: twórca może zamrozić albo dodrukować tokeny, albo w puli nie ma prawie nic."""
    if not info:
        return None
    a = info.get("audit") or {}
    if a.get("freezeAuthorityDisabled") is False:
        return "twórca może zamrozić Twoje tokeny (nie sprzedasz ich)"
    if a.get("mintAuthorityDisabled") is False:
        return "twórca może dodrukować tokeny (Twoje stracą wartość)"
    liq = info.get("liquidity")
    if liq is not None and liq < 1500:
        return f"w puli prawie nie ma pieniędzy ({usd(liq)}) – nie dałoby się sprzedać"
    return None
