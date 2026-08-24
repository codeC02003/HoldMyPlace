"""Return-date estimation and the split of an incoming receipt.

Two separate jobs live here.

The estimator answers "when does this come back, and how sure are we" from
replenishment history. This is the one place in the design where a real model
earns its keep — everything else is a field read — and the confidence it
returns is what gates whether a claim is offered at all.

The split answers the genuinely contested operational question: of the units
arriving on a truck, how many are set aside for claim holders before the rest
reaches the sales floor? Nobody who walks in and picks an item off the shelf
can be intercepted, so a queue with a zero share is a queue that never fills.
That share is the substantive ask of the whole proposal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

from .catalog import Lifecycle, Sku

#: Estimator confidence below which no claim is offered, however eligible the
#: lifecycle. This is the "confident positive signal" half of failing closed.
MIN_OFFER_CONFIDENCE = 0.50

#: Per-lifecycle estimator character: how tight the band is as a fraction of
#: the replenishment cadence, and how much to trust the result.
_BAND_FRACTION: dict[Lifecycle, float] = {
    Lifecycle.CORE: 0.25,
    Lifecycle.TEMPORARILY_UNAVAILABLE: 0.70,
    Lifecycle.SEASONAL: 0.40,
}

_BASE_CONFIDENCE: dict[Lifecycle, float] = {
    Lifecycle.CORE: 0.88,
    Lifecycle.TEMPORARILY_UNAVAILABLE: 0.55,
    Lifecycle.SEASONAL: 0.72,
}

#: Confidence penalty applied once a SKU is past its own expected return date.
#: An overdue restock is evidence against the cadence, not for it.
_OVERDUE_PENALTY = 0.30


@dataclass(frozen=True, slots=True)
class ReturnEstimate:
    """When a SKU is expected back, and how much to trust that."""

    eta: date
    band_days: int
    confidence: float
    overdue: bool = False

    @property
    def worst_case(self) -> date:
        """The late edge of the band — what a deadline must clear to be safe."""
        return self.eta + timedelta(days=self.band_days)

    @property
    def offerable(self) -> bool:
        return self.confidence >= MIN_OFFER_CONFIDENCE

    def member_copy(self) -> str:
        """How the estimate is described to a member: a range, never a date."""
        if self.band_days <= 3:
            return f"usually back around {self.eta:%b %-d}"
        return f"usually back between {self.eta:%b %-d} and {self.worst_case:%b %-d}"


def estimate_return(
    sku: Sku,
    as_of: date,
    last_received: date | None,
) -> ReturnEstimate | None:
    """Estimate when `sku` returns to the warehouse that just ran out.

    Returns None when there is nothing to reason from — no cadence, or a
    lifecycle with no replenishment character. Callers treat None as "do not
    offer a claim", never as "assume the default".
    """
    if sku.restock_cadence_days is None:
        return None
    if sku.lifecycle not in _BASE_CONFIDENCE:
        return None

    cadence = sku.restock_cadence_days
    anchor = last_received or as_of
    expected = anchor + timedelta(days=cadence)

    overdue = expected <= as_of
    if overdue:
        # The cadence has already been missed. Push the estimate out by half a
        # cycle and discount confidence rather than repeating a stale date.
        expected = as_of + timedelta(days=max(1, math.ceil(cadence / 2)))

    band = max(2, math.ceil(cadence * _BAND_FRACTION[sku.lifecycle]))
    confidence = _BASE_CONFIDENCE[sku.lifecycle]
    if overdue:
        confidence *= 1.0 - _OVERDUE_PENALTY

    if sku.lifecycle is Lifecycle.SEASONAL and sku.season_end is not None:
        if expected >= sku.season_end:
            # It will not return before the window shuts.
            return ReturnEstimate(expected, band, 0.0, overdue)

    return ReturnEstimate(expected, band, round(confidence, 4), overdue)


@dataclass(frozen=True, slots=True)
class RestockPolicy:
    """How an incoming receipt is divided between claims and the sales floor.

    `queue_share` is the fraction of arriving units reserved for claim holders.
    `max_units_per_receipt` caps that reservation so a deep queue on one SKU
    cannot strip a shelf. `min_floor_units` guarantees the floor is never left
    empty, which is what makes the policy defensible to warehouse operations.
    """

    queue_share: float = 0.25
    max_units_per_receipt: int | None = None
    min_floor_units: int = 0

    def __post_init__(self) -> None:
        if not 0.0 <= self.queue_share <= 1.0:
            raise ValueError("queue_share must be a fraction between 0 and 1")
        if self.min_floor_units < 0:
            raise ValueError("min_floor_units cannot be negative")


@dataclass(frozen=True, slots=True)
class Split:
    to_queue: int
    to_floor: int

    @property
    def units(self) -> int:
        return self.to_queue + self.to_floor


def split_receipt(units: int, open_claims: int, policy: RestockPolicy) -> Split:
    """Divide `units` between the claim queue and the sales floor.

    Never reserves more than there are claims waiting: an unfilled reservation
    is inventory held off the floor for nobody, which is the exact behaviour a
    warehouse would rightly refuse to accept.
    """
    if units < 0 or open_claims < 0:
        raise ValueError("units and open_claims must be non-negative")
    if units == 0 or open_claims == 0:
        return Split(0, units)

    wanted = math.floor(units * policy.queue_share)
    if policy.max_units_per_receipt is not None:
        wanted = min(wanted, policy.max_units_per_receipt)
    wanted = min(wanted, open_claims)

    headroom = max(0, units - policy.min_floor_units)
    to_queue = min(wanted, headroom)
    return Split(to_queue, units - to_queue)
