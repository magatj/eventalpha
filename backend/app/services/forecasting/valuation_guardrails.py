"""Valuation guardrails: decide whether a forecast is supported by its assumptions.

Upside is never capped. Instead, large results must be explained by evidence:

  * the base-case multiple at the horizon is within the company's own historical range (median),
  * earnings are not temporarily depressed or inflated (or the model normalized them),
  * the valuation methods broadly agree,
  * the input data passed validation.

Statuses
  valid                  supported by company-specific evidence
  limited_data           usable, but rests on fallbacks (sector reference multiples, short history, ...)
  requires_review        assumptions do not support the result; kept for audit, excluded from rankings
  unsupported_valuation  no suitable method for this business model with the available data
  invalid_inputs         data inconsistent / implausible (e.g. share basis mismatch); targets withheld
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

VALID, LIMITED, REVIEW, UNSUPPORTED, INVALID = "valid", "limited_data", "requires_review", "unsupported_valuation", "invalid_inputs"
STATUSES = (VALID, LIMITED, REVIEW, UNSUPPORTED, INVALID)
ATTRACTIVE_RETURN = 0.5
RANKABLE = (VALID, LIMITED)
STATUS_LABEL = {VALID: "Valid", LIMITED: "Limited data", REVIEW: "Requires review",
                UNSUPPORTED: "Unsupported valuation", INVALID: "Invalid inputs"}


def ranking_exclusion(status: str | None, valuation: dict[str, Any] | None) -> str | None:
    """Reason a forecast may not be ranked/published (None = rankable). The audit record is kept either way.
    Analyses made before the guardrails existed (status None) are judged by their targets alone."""
    if status is None or status in RANKABLE:
        return None
    flags = [f for f in ((valuation or {}).get("guardrails") or {}).get("flags", [])
             if f.get("severity") in ("review", "invalid")]
    detail = flags[0]["message"] if flags else ""
    return f"valuation {STATUS_LABEL.get(status, status).lower()}" + (f": {detail}" if detail else "")


@dataclass
class Flag:
    code: str
    severity: str  # info | warning | review | invalid
    message: str


@dataclass
class GuardrailResult:
    status: str
    flags: list[Flag] = field(default_factory=list)
    upside_check: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "label": STATUS_LABEL[self.status],
                "flags": [asdict(f) for f in self.flags], "upside_check": self.upside_check}


def evaluate(*, base_return: float | None, base_multiple: float | None, current_multiple: float | None,
             company_band: dict[str, Any] | None, selection_basis: str | None, selection_limited: bool,
             method_ratio: float | None, engine_flags: list[str], plan_status: str, plan_missing: list[str],
             financial_status: str, validation: list[str], withheld: bool, share_basis_ok: bool,
             upside_threshold: float = 1.0, max_rerating: float = 0.5, disagreement_review: float = 3.0
             ) -> GuardrailResult:
    flags: list[Flag] = []

    def add(code: str, sev: str, msg: str) -> None:
        flags.append(Flag(code, sev, msg))

    # ---- invalid inputs ---------------------------------------------------------------
    if withheld:
        add("implausible_valuation", "invalid", "Base target was implausible relative to the price (likely a data error); targets withheld.")
    if not share_basis_ok:
        add("share_basis_mismatch", "invalid", "Share count and price are not on a consistent basis (split or unit mismatch).")
    if financial_status == "invalid":
        add("invalid_financials", "invalid", "SEC financial data failed validation: " + "; ".join(validation[:2]))
    if any(f.severity == "invalid" for f in flags):
        return GuardrailResult(INVALID, flags)

    # ---- unsupported --------------------------------------------------------------------
    if plan_status == "unsupported" or financial_status in ("data_unavailable", "unsupported_accounting"):
        why = ", ".join(plan_missing) if plan_missing else financial_status.replace("_", " ")
        add("unsupported_method", "invalid", f"No suitable valuation method with the available data (missing: {why}).")
        return GuardrailResult(UNSUPPORTED, flags)
    if base_return is None:
        add("no_target", "invalid", "No valuation method produced a target.")
        return GuardrailResult(UNSUPPORTED, flags)

    # ---- informational / limiting conditions -------------------------------------------
    for v in validation:
        add("data_validation", "warning", v)
    if selection_limited:
        add("sector_fallback", "warning", "Multiples come from the broad sector reference band (no company history or peers).")
    if "cyclical_earnings" in engine_flags:
        add("cyclical_earnings", "warning", "Earnings are cyclical (margins swing or change sign across years).")
    if "eps_depressed" in engine_flags:
        recovery_bet = base_return is not None and base_return > ATTRACTIVE_RETURN
        add("eps_depressed", "review" if recovery_bet else "warning",
            "EPS is temporarily depressed versus the company's norm; scenarios assume partial normalization"
            + (f" — the {base_return:.0%} base-case upside depends on that recovery." if recovery_bet else "."))
    if "eps_inflated" in engine_flags:
        add("eps_inflated", "warning", "EPS is above the company's historical norm (possible cycle peak); scenarios fade margins.")
    if "growth_inconsistent" in engine_flags:
        add("growth_inconsistent", "info", "Recent and multi-year revenue growth disagree; the lower figure was weighted.")
    if "fcf_excludes_sbc" in engine_flags:
        add("fcf_excludes_sbc", "info", "Stock-based compensation not reported; DCF uses FCF before SBC.")

    # ---- multiple expansion -----------------------------------------------------------------
    p50 = (company_band or {}).get("p50")
    p75 = (company_band or {}).get("p75")
    expansion = None
    if base_multiple and current_multiple and current_multiple > 0:
        expansion = base_multiple / current_multiple - 1
        if expansion > max_rerating:
            supported = p75 is not None and base_multiple <= p75
            add("multiple_expansion", "warning" if supported else "review",
                f"Base case assumes the multiple rises {expansion:.0%} (from {current_multiple:.1f}x to {base_multiple:.1f}x)"
                + (" — within the company's historical range." if supported else " — beyond the company's historical range."))
    if method_ratio is not None and method_ratio > 2.5:
        # disagreement blocks ranking only when it props up an attractive result
        severe = method_ratio > disagreement_review and base_return > ATTRACTIVE_RETURN
        add("methods_disagree", "review" if severe else "warning",
            f"Valuation methods disagree by {method_ratio:.1f}x" + (" while the blended result shows large upside." if severe else "."))

    # ---- large upside must be supported -------------------------------------------------------
    upside: dict[str, Any] = {"threshold": upside_threshold, "base_return_12m": base_return}
    if base_return > upside_threshold:
        checks = {
            "multiple within company's historical median": bool(p50 is not None and base_multiple is not None
                                                               and base_multiple <= p50 * 1.05),
            "company-specific multiple evidence": selection_basis is not None and not selection_limited
                                                  and selection_basis.startswith(("company", "peers")),
            "earnings not depressed/inflated": not ({"eps_depressed", "eps_inflated"} & set(engine_flags)),
            "valuation methods agree (within 2x)": method_ratio is None or method_ratio <= 2.0,
            "financial data passed validation": not validation,
        }
        upside["checks"] = checks
        upside["supported"] = all(checks.values())
        if upside["supported"]:
            add("large_upside_supported", "info",
                f"Base-case upside {base_return:.0%} exceeds {upside_threshold:.0%} but every supporting check passed.")
        else:
            failed = [k for k, ok in checks.items() if not ok]
            add("unsupported_upside", "review",
                f"Base-case upside {base_return:.0%} exceeds {upside_threshold:.0%} without support: " + "; ".join(failed) + ".")
    upside["expansion"] = expansion

    if any(f.severity == "review" for f in flags):
        return GuardrailResult(REVIEW, flags, upside)
    if selection_limited or plan_status == "limited" or validation or plan_missing:
        return GuardrailResult(LIMITED, flags, upside)
    return GuardrailResult(VALID, flags, upside)
