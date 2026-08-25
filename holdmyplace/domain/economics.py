"""Whether any of this pays for itself.

The honest answer is that it depends almost entirely on numbers Costco holds and
this package does not, so every input below carries its provenance. Read the
provenance table before the results: four of these are guesses, and two of the
guesses can flip the sign of the conclusion on their own.

The structural finding the model exists to expose: free claim delivery does not
clear its own cost on merchandise margin. It clears only when membership renewal
is counted, because merchandise is close to break-even at Costco and membership
fees supply roughly half of operating income. That makes this a retention
program wearing a fulfillment feature's clothes, and it means a fulfillment
cost-per-stop metric will reject it correctly, every time, forever.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum

from .claims import Membership
from .money import ZERO, money, pct
from .routing import Decision, Mode


class Source(str, Enum):
    PUBLIC = "public"
    """Reported by Costco or otherwise verifiable from outside."""

    POLICY = "policy"
    """A lever this design sets. Not a fact, a choice."""

    ESTIMATED = "estimated"
    """A defensible outside guess. Wrong by some margin, but bounded."""

    UNKNOWN = "unknown"
    """Costco holds this and I do not. The conclusion turns on these."""


@dataclass(frozen=True, slots=True)
class Assumptions:
    """Every economic input, in one place, with nothing hidden in the code."""

    # -- public -----------------------------------------------------------
    merchandise_margin: Decimal = pct("0.11")
    membership_fee_base: Decimal = money("65.00")
    membership_fee_executive: Decimal = money("130.00")

    # -- policy levers ----------------------------------------------------
    topup_threshold: Decimal = money("75.00")
    express_fee: Decimal = money("4.99")

    # -- estimated --------------------------------------------------------
    topup_basket: Decimal = money("85.00")
    remaining_tenure_years: Decimal = pct("6.0")
    support_contact_cost: Decimal = money("5.50")

    # -- unknown: the conclusion rests on these --------------------------
    topup_incrementality: Decimal = pct("0.30")
    """Share of a top-up basket that is new demand rather than pull-forward."""

    repurchase_rate: Decimal = pct("0.45")
    """Share of refunded members who would have re-bought the item anyway."""

    renewal_lift_pp: Decimal = pct("0.5")
    """Percentage points of renewal lift from resolving an out-of-stock well."""

    support_contact_rate: Decimal = pct("0.18")
    """Support contacts generated per out-of-stock refund."""


PROVENANCE: dict[str, tuple[Source, str]] = {
    "merchandise_margin": (Source.PUBLIC, "~11% merchandise gross margin"),
    "membership_fee_base": (Source.PUBLIC, "US base membership"),
    "membership_fee_executive": (Source.PUBLIC, "US Executive membership"),
    "topup_threshold": (Source.POLICY, "free-delivery basket minimum"),
    "express_fee": (Source.POLICY, "priced to cover an adjacent stop"),
    "topup_basket": (Source.ESTIMATED, "baskets cluster just above a threshold"),
    "remaining_tenure_years": (Source.ESTIMATED, "renewal in the low nineties"),
    "support_contact_cost": (Source.ESTIMATED, "loaded cost of one contact"),
    "topup_incrementality": (Source.UNKNOWN, "new demand vs pull-forward"),
    "repurchase_rate": (Source.UNKNOWN, "refund-to-repurchase rate"),
    "renewal_lift_pp": (Source.UNKNOWN, "load-bearing: the entire case"),
    "support_contact_rate": (Source.UNKNOWN, "contacts per OOS refund"),
}

#: Inputs whose plausible range can invert the sign of the result.
LOAD_BEARING = ("renewal_lift_pp", "topup_incrementality")


def membership_fee(tier: Membership, a: Assumptions) -> Decimal:
    return (
        a.membership_fee_executive
        if tier is Membership.EXECUTIVE
        else a.membership_fee_base
    )


@dataclass(frozen=True, slots=True)
class Contribution:
    """One filled claim, decomposed.

    `total_excl_renewal` is reported alongside `total` deliberately. The first
    is what a fulfillment team will measure; the second is what the decision
    actually depends on. Showing only the second is how proposals like this get
    approved and then quietly fail their post-mortem.
    """

    item_margin: Decimal = ZERO
    topup_margin: Decimal = ZERO
    fee_revenue: Decimal = ZERO
    support_avoided: Decimal = ZERO
    stop_cost: Decimal = ZERO
    renewal_value: Decimal = ZERO

    @property
    def total_excl_renewal(self) -> Decimal:
        return money(
            self.item_margin
            + self.topup_margin
            + self.fee_revenue
            + self.support_avoided
            - self.stop_cost
        )

    @property
    def total(self) -> Decimal:
        return money(self.total_excl_renewal + self.renewal_value)

    def __add__(self, other: "Contribution") -> "Contribution":
        return Contribution(
            item_margin=money(self.item_margin + other.item_margin),
            topup_margin=money(self.topup_margin + other.topup_margin),
            fee_revenue=money(self.fee_revenue + other.fee_revenue),
            support_avoided=money(self.support_avoided + other.support_avoided),
            stop_cost=money(self.stop_cost + other.stop_cost),
            renewal_value=money(self.renewal_value + other.renewal_value),
        )


EMPTY = Contribution()


def renewal_value(tier: Membership, a: Assumptions) -> Decimal:
    """Expected value of a renewal-probability lift on one member.

    Undiscounted on purpose. A discount rate would add precision the input
    does not have. `renewal_lift_pp` is a guess, and dressing a guess in a
    net-present-value calculation makes it look like a measurement.
    """
    lift = a.renewal_lift_pp / Decimal("100")
    return money(lift * membership_fee(tier, a) * a.remaining_tenure_years)


def topup_margin(a: Assumptions) -> Decimal:
    """Margin on the incremental portion of a threshold-triggered basket.

    Pulling an order forward is not the same as creating one. Only the share
    the member would not otherwise have bought counts, which is why this figure
    is so much smaller than the basket that produces it.
    """
    incremental = a.topup_basket * a.topup_incrementality
    return money(incremental * a.merchandise_margin)


def item_margin(price: Decimal, a: Assumptions) -> Decimal:
    """Margin on the recovered item, net of members who would have re-bought."""
    recovered = money(price) * (Decimal("1") - a.repurchase_rate)
    return money(recovered * a.merchandise_margin)


def evaluate(
    decision: Decision,
    *,
    price: Decimal,
    tier: Membership,
    a: Assumptions,
    actual_stop_cost: Decimal | None = None,
) -> Contribution:
    """Score one filled claim under `a`.

    `actual_stop_cost` overrides the decision's provisional estimate once a
    batched route has actually shipped and its cost can be divided by the
    stops it made.
    """
    stop_cost = (
        decision.cost_estimate if actual_stop_cost is None else money(actual_stop_cost)
    )
    support = money(a.support_contact_rate * a.support_contact_cost)

    # Only the modes that put something on a vehicle can trigger a top-up.
    tops_up = decision.mode in (
        Mode.BATCHED_ROUTE,
        Mode.PAID_EXPRESS,
        Mode.EXECUTIVE_FREE,
    )

    return Contribution(
        item_margin=item_margin(price, a),
        topup_margin=topup_margin(a) if tops_up else ZERO,
        fee_revenue=money(decision.fee_charged),
        support_avoided=support,
        stop_cost=stop_cost,
        renewal_value=renewal_value(tier, a),
    )


# ---------------------------------------------------------------------------
# The isolated free-delivery question
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SubsidyCheck:
    """The one line item that needs justifying, evaluated on its own.

    Reproduces the worked example from the memo, so the memo and the code
    cannot drift apart: an $85 basket, 30% of it genuinely new, against a $4
    adjacent stop.
    """

    basket: Decimal
    incremental_revenue: Decimal
    gross_margin: Decimal
    stop_cost: Decimal
    renewal_value: Decimal

    @property
    def merchandise_only(self) -> Decimal:
        return money(self.gross_margin - self.stop_cost)

    @property
    def with_renewal(self) -> Decimal:
        return money(self.merchandise_only + self.renewal_value)

    @property
    def clears_on_merchandise(self) -> bool:
        return self.merchandise_only >= 0

    @property
    def clears_with_renewal(self) -> bool:
        return self.with_renewal >= 0


def check_subsidy(
    a: Assumptions,
    *,
    stop_cost: Decimal,
    tier: Membership = Membership.EXECUTIVE,
) -> SubsidyCheck:
    incremental = money(a.topup_basket * a.topup_incrementality)
    return SubsidyCheck(
        basket=a.topup_basket,
        incremental_revenue=incremental,
        gross_margin=money(incremental * a.merchandise_margin),
        stop_cost=money(stop_cost),
        renewal_value=renewal_value(tier, a),
    )


def sensitivity(
    a: Assumptions,
    *,
    stop_cost: Decimal,
    lifts: tuple[float, ...] = (0.0, 0.25, 0.5, 1.0, 2.0),
    incrementalities: tuple[float, ...] = (0.1, 0.2, 0.3, 0.5, 0.8),
    tier: Membership = Membership.EXECUTIVE,
) -> list[tuple[float, list[tuple[float, Decimal]]]]:
    """Contribution per subsidized stop across the two load-bearing unknowns.

    The useful output is not any single cell but the boundary between them: it
    shows how much renewal lift the program has to produce before the spend is
    defensible, which is exactly what a pilot should be sized to detect.
    """
    grid: list[tuple[float, list[tuple[float, Decimal]]]] = []
    for lift in lifts:
        row: list[tuple[float, Decimal]] = []
        for inc in incrementalities:
            trial = replace(
                a,
                renewal_lift_pp=pct(str(lift)),
                topup_incrementality=pct(str(inc)),
            )
            row.append((inc, check_subsidy(trial, stop_cost=stop_cost, tier=tier).with_renewal))
        grid.append((lift, row))
    return grid


def breakeven_lift(
    a: Assumptions,
    *,
    stop_cost: Decimal,
    tier: Membership = Membership.EXECUTIVE,
) -> Decimal:
    """Renewal lift, in percentage points, at which a subsidized stop breaks even.

    This is the single number a pilot has to be powered to detect. If it comes
    out larger than any plausible service effect, the free tier should not ship
    regardless of how attractive the rest of the design is.
    """
    shortfall = check_subsidy(a, stop_cost=stop_cost, tier=tier).merchandise_only
    if shortfall >= 0:
        return Decimal("0.0")
    annual = membership_fee(tier, a) * a.remaining_tenure_years
    if annual == 0:
        raise ValueError("membership value cannot be zero")
    return (-shortfall / annual * Decimal("100")).quantize(Decimal("0.001"))
