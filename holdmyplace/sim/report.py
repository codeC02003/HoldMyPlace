"""Console rendering of a simulation run.

Ordered the way the decision is actually made: gate one first, because nothing
downstream matters if the promise is not keepable; then the operational result;
then the economics, split so that the merchandise-only figure and the
renewal-inclusive figure sit next to each other rather than one hiding the
other; then the assumptions, marked with their provenance.
"""

from __future__ import annotations

from decimal import Decimal

from ..domain import economics as econ
from ..domain.catalog import DENIAL_COPY, Denial
from ..domain.money import fmt, money
from ..domain.routing import Mode, Trigger
from ..domain.sourcing import LADDER, Rung
from .run import GATE_FAIL, GATE_PASS, Results

WIDTH = 66

_MODE_LABEL = {
    Mode.PICKUP_HOLD: "Pickup hold",
    Mode.BATCHED_ROUTE: "Batched route",
    Mode.PAID_EXPRESS: "Paid express",
    Mode.EXECUTIVE_FREE: "Executive free",
}

_RUNG_LABEL = {
    Rung.OTHER_WAREHOUSE: "Pulled from another warehouse",
    Rung.OTHER_CHANNEL: "Filled from the other channel",
    Rung.CLAIM_QUEUE: "Claim offered",
    Rung.SUBSTITUTE: "Refunded, substitutes shown",
    Rung.REFUND_ONLY: "Refunded, nothing else",
}

_DENIAL_LABEL = {
    Denial.ONE_TIME_BUY: "One-time buy",
    Denial.DISCONTINUED: "Discontinued",
    Denial.SEASON_CLOSED: "Season closed",
    Denial.NO_RESTOCK_SIGNAL: "No confident signal",
    Denial.UNKNOWN_LIFECYCLE: "Unknown lifecycle",
}


def _leader(label: str, value: str, indent: int = 0) -> str:
    pad = " " * indent
    room = WIDTH - len(pad) - len(value) - 1
    dots = "." * max(2, room - len(label))
    return f"{pad}{label} {dots} {value}"


def _rule(title: str = "") -> str:
    if not title:
        return "─" * WIDTH
    return f"── {title} " + "─" * max(0, WIDTH - len(title) - 4)


def _pctf(value: float) -> str:
    return f"{value * 100:.1f}%"


def render(results: Results, *, sensitivity: bool = False) -> str:
    r = results
    cfg = r.world.config
    out: list[str] = []

    out.append("")
    out.append("HOLD MY PLACE — claim-queue simulation")
    out.append(
        f"{cfg.days} days from {cfg.start:%b %-d %Y} · {cfg.n_skus} SKUs · seed {cfg.seed}"
    )
    out.append("")

    # -- the gate ---------------------------------------------------------
    out.append(_rule("GATE ONE — is the promise keepable"))
    out.append(_leader("Out-of-stock lines after payment", f"{r.oos_events:,}"))
    out.append(
        _leader(
            "Addressable — SKU restocked within 30d",
            f"{r.gate_one_restockable:,} ({_pctf(r.addressable_rate)})",
        )
    )
    out.append(
        _leader(
            "Covered — a claim was offered",
            f"{r.offers_claimable:,} ({_pctf(r.coverage_rate)})",
        )
    )
    out.append(
        _leader(
            "Promises kept — claims filed then filled",
            f"{r.claims_filled:,} / {r.claims_created:,} "
            f"({_pctf(r.promise_keeping_rate)})",
        )
    )
    out.append(
        _leader(
            f"Verdict on promises kept (pass ≥{_pctf(GATE_PASS)})",
            r.gate_verdict,
        )
    )
    out.append("")
    out.append(
        "  The addressable share is a ceiling, not a failure: a one-time buy is"
    )
    out.append(
        "  refunded with no claim offered, so there is no promise to break."
    )
    out.append("")

    # -- the ladder -------------------------------------------------------
    out.append(_rule("RESOLUTION LADDER"))
    out.append(
        "  A claim is the third rung. Sourcing the item is tried first, because"
    )
    out.append(
        "  offering a wait for something on a shelf 14 km away is a worse answer."
    )
    out.append("")
    for rung in LADDER:
        count = r.rungs.get(rung, 0)
        if not count:
            continue
        share = _pctf(count / r.oos_events) if r.oos_events else "0.0%"
        out.append(_leader(_RUNG_LABEL[rung], f"{count:,} ({share})", indent=2))
    out.append("")
    out.append(
        _leader("Found without a refund or a wait", _pctf(r.sourced_rate))
    )
    out.append(
        _leader("Member ends up with the item they ordered", _pctf(r.got_the_item_rate))
    )
    out.append("")

    # -- funnel -----------------------------------------------------------
    out.append(_rule("CLAIM FUNNEL"))
    out.append(_leader("Refunds issued", fmt(r.refunds_issued)))
    out.append(
        _leader("Claim offered", f"{r.offers_claimable:,} ({_pctf(r.coverage_rate)})")
    )
    out.append(
        _leader(
            "Claims filed", f"{r.claims_created:,} ({_pctf(r.claim_optin_rate)} of offers)"
        )
    )
    if r.denials:
        out.append("")
        out.append("  Declined, by reason:")
        for denial, count in r.denials.most_common():
            out.append(_leader(_DENIAL_LABEL[denial], f"{count:,}", indent=4))
    out.append("")
    out.append("  Deadline warnings shown before filing:")
    out.append(_leader("Filed anyway", f"{r.proceeded_after_warning:,}", indent=4))
    out.append(_leader("Moved the date out", f"{r.extended_after_warning:,}", indent=4))
    out.append(_leader("Took the refund", f"{r.declined_after_warning:,}", indent=4))
    out.append("")

    # -- operations -------------------------------------------------------
    out.append(_rule("OPERATIONS"))
    out.append(
        _leader(
            "Claims filled",
            f"{r.claims_filled:,} / {r.claims_created:,} ({_pctf(r.promise_keeping_rate)})",
        )
    )
    out.append(_leader("Claims expired unfilled", f"{r.claims_expired:,}"))
    out.append(_leader("Still open at horizon", f"{r.claims_open_at_end:,}"))
    median = r.median_fill_days
    out.append(
        _leader(
            "Median days, claim filed → arrival",
            f"{median:.0f}" if median is not None else "n/a",
        )
    )
    out.append(_leader("Nudges sent before expiry", f"{r.nudges_sent:,}"))
    out.append("")
    out.append("  Receipt units:")
    out.append(_leader("Allocated to claims", f"{r.units_to_queue:,}", indent=4))
    out.append(_leader("Released to the floor", f"{r.units_to_floor:,}", indent=4))
    out.append(
        _leader(
            "Reserved but unreachable → floor",
            f"{r.units_reserved_unused:,}",
            indent=4,
        )
    )
    out.append("")
    out.append("  Fulfillment mode:")
    for mode in Mode:
        count = r.mode_counts.get(mode, 0)
        if not count:
            continue
        spend = r.mode_cost.get(mode, Decimal("0.00"))
        per = money(spend / count) if count else Decimal("0.00")
        out.append(
            _leader(
                _MODE_LABEL[mode],
                f"{count:,} @ {fmt(per)}/stop",
                indent=4,
            )
        )
    if r.dispatches:
        density = [d for d in r.dispatches if d.trigger is Trigger.DENSITY]
        forced = [d for d in r.dispatches if d.trigger is Trigger.MUST_GO]
        avg_stops = sum(d.stops for d in r.dispatches) / len(r.dispatches)
        out.append("")
        out.append("  Batched routes:")
        out.append(_leader("Shipped on density", f"{len(density):,}", indent=4))
        out.append(_leader("Forced out by a deadline", f"{len(forced):,}", indent=4))
        out.append(_leader("Mean stops per route", f"{avg_stops:.1f}", indent=4))
    out.append("")

    # -- economics --------------------------------------------------------
    c = r.contribution
    out.append(_rule("ECONOMICS"))
    out.append(_leader("Recovered item margin", fmt(c.item_margin)))
    out.append(_leader("Top-up margin (incremental only)", fmt(c.topup_margin)))
    out.append(_leader("Express fees collected", fmt(c.fee_revenue)))
    out.append(_leader("Support contacts avoided", fmt(c.support_avoided)))
    out.append(_leader("Last-mile cost", "-" + fmt(c.stop_cost).lstrip("-")))
    out.append(_rule())
    out.append(
        _leader("Contribution, merchandise only", fmt(c.total_excl_renewal))
    )
    out.append(_leader("Renewal value at assumed lift", fmt(c.renewal_value)))
    out.append(_leader("Contribution, all in", fmt(c.total)))
    out.append("")
    out.append(
        _leader("Cost per fulfillment", fmt(r.cost_per_fulfillment))
    )
    if r.claims_filled:
        out.append(
            _leader(
                "All-in contribution per claim filled",
                fmt(money(c.total / r.claims_filled)),
            )
        )
    out.append("")

    # -- the subsidized stop, isolated ------------------------------------
    stop_cost = r.assumptions_stop_cost
    check = econ.check_subsidy(r.assumptions, stop_cost=stop_cost)
    out.append(_rule("THE SUBSIDIZED STOP, ON ITS OWN"))
    out.append(_leader("Top-up basket", fmt(check.basket)))
    out.append(
        _leader(
            "Genuinely new demand",
            f"{fmt(check.incremental_revenue)} "
            f"({_pctf(float(r.assumptions.topup_incrementality))})",
        )
    )
    out.append(_leader("Gross margin on it", fmt(check.gross_margin)))
    out.append(_leader("Observed cost of the stop", "-" + fmt(check.stop_cost)))
    out.append(_rule())
    verdict = "clears" if check.clears_on_merchandise else "does not clear"
    out.append(
        _leader(f"Merchandise only — {verdict}", fmt(check.merchandise_only))
    )
    out.append(_leader("With renewal value", fmt(check.with_renewal)))
    out.append("")
    out.append(
        _leader(
            "Renewal lift needed to break even",
            f"{r.breakeven_lift}pp",
        )
    )
    out.append(
        _leader(
            "Renewal lift assumed here",
            f"{r.assumptions.renewal_lift_pp}pp",
        )
    )
    out.append("")

    if sensitivity:
        out.extend(_sensitivity_block(r, stop_cost))

    # -- demand signal ----------------------------------------------------
    if r.demand_clusters:
        out.append(_rule("DEMAND SIGNAL — top open clusters at horizon"))
        out.append(
            f"  {'SKU':<9} {'AREA':<5} {'OPEN':>5} {'≤21d':>5}  FORFEIT"
        )
        for cluster in r.demand_clusters:
            out.append(
                f"  {cluster.sku_id:<9} {cluster.zip_code:<5} "
                f"{cluster.open_claims:>5} {cluster.expiring_soon:>5}  "
                f"{fmt(cluster.forfeit_value)}"
            )
        out.append("")

    if r.stale_flags:
        out.append(_rule("STALE ITEM-MASTER FLAGS"))
        out.append(
            "  Claimable status, no receipt in 120 days. For a buyer to resolve:"
        )
        out.append("  " + ", ".join(r.stale_flags))
        out.append("")

    # -- provenance -------------------------------------------------------
    out.append(_rule("ASSUMPTIONS"))
    by_source: dict[econ.Source, list[str]] = {}
    for name, (source, note) in econ.PROVENANCE.items():
        value = getattr(r.assumptions, name)
        marker = " ←" if name in econ.LOAD_BEARING else ""
        by_source.setdefault(source, []).append(
            _leader(f"{name}{marker}", f"{value}", indent=4) + f"   {note}"
        )
    for source in econ.Source:
        rows = by_source.get(source)
        if not rows:
            continue
        out.append(f"  {source.value.upper()}")
        out.extend(rows)
    out.append("")
    out.append("  ← load-bearing: plausible values here flip the conclusion.")
    out.append("")
    return "\n".join(out)


def _sensitivity_block(r: Results, stop_cost: Decimal) -> list[str]:
    grid = econ.sensitivity(r.assumptions, stop_cost=stop_cost)
    incrementalities = [inc for inc, _ in grid[0][1]]

    out = [_rule("SENSITIVITY — contribution per subsidized stop")]
    header = "  lift\\inc " + "".join(f"{inc:>9.0%}" for inc in incrementalities)
    out.append(header)
    for lift, row in grid:
        cells = "".join(f"{fmt(value):>9}" for _, value in row)
        out.append(f"  {lift:>6.2f}pp{cells}")
    out.append("")
    out.append(
        "  Read the sign boundary, not the cells: it is the renewal lift a"
    )
    out.append("  pilot has to be powered to detect.")
    out.append("")
    return out


def render_member_view(results: Results) -> str:
    """The copy a member would actually see, sampled from denial reasons.

    Included so the framing stays visible next to the mechanics: the refund is
    always the first sentence, and nobody is told to wait for something that is
    not coming back.
    """
    out = ["", _rule("MEMBER-FACING COPY"), ""]
    for denial in results.denials:
        out.append(f"  {DENIAL_COPY[denial]}")
    out.append("")
    return "\n".join(out)
