"""Sector classification and valuation reference ranges.

The multiple ranges below are *model assumptions* (broad, long-run heuristic bands for
US large caps), not live market data. Edit them here; every forecast records the values used.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SectorProfile:
    name: str
    pe_low: float
    pe_mid: float
    pe_high: float
    ev_sales_low: float
    ev_sales_mid: float
    ev_sales_high: float
    dcf_applicable: bool = True
    pe_applicable: bool = True
    note: str | None = None


SECTORS: dict[str, SectorProfile] = {
    "Technology": SectorProfile("Technology", 18, 27, 38, 3.0, 6.0, 10.0),
    "Communication Services": SectorProfile("Communication Services", 13, 19, 27, 1.5, 3.0, 5.5),
    "Consumer Discretionary": SectorProfile("Consumer Discretionary", 14, 21, 30, 0.8, 1.8, 3.5),
    "Consumer Staples": SectorProfile("Consumer Staples", 16, 20, 25, 0.8, 1.6, 2.8),
    "Health Care": SectorProfile("Health Care", 14, 20, 28, 1.5, 3.5, 6.0),
    "Financials": SectorProfile("Financials", 9, 13, 17, 1.5, 3.0, 5.0, dcf_applicable=False,
                                note="Free-cash-flow DCF is not meaningful for banks/insurers (debt is operating capital)."),
    "Industrials": SectorProfile("Industrials", 15, 20, 27, 1.0, 2.0, 3.5),
    "Energy": SectorProfile("Energy", 8, 12, 16, 0.6, 1.2, 2.2,
                            note="Earnings are commodity-cyclical; trailing multiples can mislead at cycle peaks/troughs."),
    "Utilities": SectorProfile("Utilities", 14, 18, 22, 2.0, 3.0, 4.0),
    "Materials": SectorProfile("Materials", 12, 17, 22, 1.0, 1.8, 3.0),
    "Real Estate": SectorProfile("Real Estate", 18, 28, 40, 5.0, 9.0, 14.0, pe_applicable=False,
                                 note="REITs are usually valued on P/FFO, which is not implemented; low reliability."),
}
DEFAULT_SECTOR = "Industrials"


def sector_from_sic(sic: str | int | None, description: str | None = None) -> str:
    try:
        s = int(sic) if sic is not None else None
    except (TypeError, ValueError):
        s = None
    if s is None:
        return DEFAULT_SECTOR
    if 1000 <= s <= 1499 or s in (2911, 2990, 4610, 4612, 4613, 4922, 4923):
        return "Materials" if 1000 <= s <= 1099 or 1400 <= s <= 1499 else "Energy"
    if 2000 <= s <= 2199 or s in (2840, 2844, 5411, 5141, 5149, 5912):
        return "Consumer Staples"
    if 2830 <= s <= 2836 or 3841 <= s <= 3851 or 8000 <= s <= 8099 or s == 8731 or s == 5047 or s == 5122:
        return "Health Care"
    if s in (3570, 3571, 3572, 3575, 3576, 3577, 3578, 3579) or 3661 <= s <= 3679 or s in (3825, 3826, 3827, 3829):
        return "Technology"
    if 7370 <= s <= 7379:
        return "Technology"
    if 4800 <= s <= 4899 or s in (2711, 2721, 7812, 7819, 7822, 7829, 7841):
        return "Communication Services"
    if 4900 <= s <= 4999:
        return "Utilities"
    if s in (6324, 6411) and "insur" not in (description or "").lower():
        return "Health Care"  # hospital & medical service plans (managed care, e.g. UNH)
    if s == 7389:
        return "Financials"  # in this universe: payment networks (V, MA, PYPL) — GICS Financials
    if s == 6798 or 6500 <= s <= 6553:
        return "Real Estate"
    if 6000 <= s <= 6799:
        return "Financials"
    if 2200 <= s <= 2399 or s in (3021, 3100, 3140, 3711, 3714, 3716, 3751, 3942, 3944, 3949) or 5000 <= s <= 5999 or 7000 <= s <= 7299 or 5800 <= s <= 5899:
        return "Consumer Discretionary"
    if 2800 <= s <= 2899 or 2600 <= s <= 2699 or 3300 <= s <= 3399 or 3200 <= s <= 3299:
        return "Materials"
    if 3600 <= s <= 3699:
        return "Technology" if "semiconductor" in (description or "").lower() else "Industrials"
    return DEFAULT_SECTOR


def profile(sector: str | None) -> SectorProfile:
    return SECTORS.get(sector or DEFAULT_SECTOR, SECTORS[DEFAULT_SECTOR])
