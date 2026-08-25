"""The resolution ladder: a claim is the fourth answer, not the first.

Inventory at a warehouse club is location-specific and channel-specific. An item
missing from one warehouse can be sitting on a shelf twenty miles away, and the
online and in-warehouse assortments are separate pools rather than one shared
one. A design that jumps straight from "out of stock here" to "join a queue"
therefore offers a wait in cases where the actual item could be in the member's
hands this week.

So the rungs are ordered by how close each outcome is to what the member asked
for, not by what is cheapest or easiest to build:

    1. another warehouse   the item itself, from nearby stock
    2. the other channel   the item itself, from the other assortment
    3. a claim             the item itself, later
    4. a substitute        a different item, now
    5. a refund            nothing

Every rung the ladder passes over is recorded with the reason it was rejected.
That trail is what makes the resolution explainable to a member and auditable
afterwards, and it is the difference between "no" and "no, because".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

from .catalog import Channel, Sku


class Rung(str, Enum):
    OTHER_WAREHOUSE = "other_warehouse"
    OTHER_CHANNEL = "other_channel"
    CLAIM_QUEUE = "claim_queue"
    SUBSTITUTE = "substitute"
    REFUND_ONLY = "refund_only"


#: Ladder order. `resolve` walks this and stops at the first rung that answers.
LADDER: tuple[Rung, ...] = (
    Rung.OTHER_WAREHOUSE,
    Rung.OTHER_CHANNEL,
    Rung.CLAIM_QUEUE,
    Rung.SUBSTITUTE,
    Rung.REFUND_ONLY,
)


@dataclass(frozen=True, slots=True)
class StockPoint:
    """On-hand for one SKU at one location, and how far away it is."""

    warehouse: str
    distance_km: float
    on_hand: int

    def __post_init__(self) -> None:
        if self.distance_km < 0:
            raise ValueError("distance cannot be negative")
        if self.on_hand < 0:
            raise ValueError("on-hand cannot be negative")


@dataclass(frozen=True, slots=True)
class SourcingPolicy:
    """When pulling from elsewhere is allowed.

    `min_on_hand_to_pull` is the guard that makes this acceptable to the
    warehouse being pulled from: a location down to its last couple of units is
    left alone rather than being stripped to cover someone else's shortfall.
    """

    max_transfer_km: float = 80.0
    min_on_hand_to_pull: int = 3
    transfer_days: int = 3

    def __post_init__(self) -> None:
        if self.max_transfer_km <= 0:
            raise ValueError("transfer radius must be positive")
        if self.min_on_hand_to_pull < 1:
            raise ValueError("pulling from a location with no stock is not sourcing")


@dataclass(frozen=True, slots=True)
class Rejection:
    rung: Rung
    reason: str


@dataclass(frozen=True, slots=True)
class Resolution:
    """Which rung answered, and why the ones above it did not."""

    rung: Rung
    passed_over: tuple[Rejection, ...] = ()
    source: StockPoint | None = None
    substitutes: int = 0

    @property
    def gets_the_item(self) -> bool:
        """Whether the member ends up with the thing they actually ordered."""
        return self.rung in (
            Rung.OTHER_WAREHOUSE,
            Rung.OTHER_CHANNEL,
            Rung.CLAIM_QUEUE,
        )

    @property
    def immediate(self) -> bool:
        """Whether it arrives on normal delivery timescales, with no waiting."""
        return self.rung in (Rung.OTHER_WAREHOUSE, Rung.OTHER_CHANNEL)

    @property
    def member_copy(self) -> str:
        if self.rung is Rung.OTHER_WAREHOUSE:
            assert self.source is not None
            return (
                f"Found it at our {self.source.warehouse} warehouse. "
                "We'll bring it over, no change to your order."
            )
        if self.rung is Rung.OTHER_CHANNEL:
            return "It's available online, so we'll ship it from there instead."
        if self.rung is Rung.CLAIM_QUEUE:
            return "Not in stock anywhere nearby. We can hold your place for the next delivery."
        if self.rung is Rung.SUBSTITUTE:
            plural = "s" if self.substitutes != 1 else ""
            return f"Refunded. We found {self.substitutes} close alternative{plural}."
        return "Refunded in full. Nothing else to do."


def _nearest_pullable(
    nearby: Sequence[StockPoint], policy: SourcingPolicy
) -> StockPoint | None:
    """Closest location with enough on hand to spare a unit."""
    candidates = [
        point
        for point in nearby
        if point.distance_km <= policy.max_transfer_km
        and point.on_hand >= policy.min_on_hand_to_pull
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda p: (p.distance_km, p.warehouse))


def _why_no_warehouse(
    nearby: Sequence[StockPoint], policy: SourcingPolicy
) -> str:
    if not nearby:
        return "no other warehouse carries it"
    in_range = [p for p in nearby if p.distance_km <= policy.max_transfer_km]
    if not in_range:
        closest = min(p.distance_km for p in nearby)
        return f"nearest stock is {closest:.0f} km away, beyond the transfer radius"
    return (
        f"{len(in_range)} location(s) in range, none holding "
        f"{policy.min_on_hand_to_pull}+ units to spare"
    )


def resolve(
    sku: Sku,
    *,
    channel: Channel,
    nearby: Sequence[StockPoint] = (),
    other_channel_has_stock: bool = False,
    substitutes: int = 0,
    claim_available: bool,
    policy: SourcingPolicy | None = None,
) -> Resolution:
    """Walk the ladder and return the first rung that answers.

    `claim_available` is supplied by the caller rather than computed here,
    because deciding it needs the eligibility read and the return estimate,
    which this module deliberately knows nothing about. Sourcing answers "can
    the item be found now"; eligibility answers "will it come back".
    """
    policy = policy or SourcingPolicy()
    passed_over: list[Rejection] = []

    source = _nearest_pullable(nearby, policy)
    if source is not None:
        return Resolution(Rung.OTHER_WAREHOUSE, tuple(passed_over), source=source)
    passed_over.append(
        Rejection(Rung.OTHER_WAREHOUSE, _why_no_warehouse(nearby, policy))
    )

    other = Channel.ONLINE if channel is Channel.WAREHOUSE else Channel.WAREHOUSE
    if other not in sku.channels:
        passed_over.append(
            Rejection(
                Rung.OTHER_CHANNEL, f"not part of the {other.value} assortment"
            )
        )
    elif not other_channel_has_stock:
        passed_over.append(
            Rejection(
                Rung.OTHER_CHANNEL,
                f"out of stock in the {other.value} assortment as well",
            )
        )
    else:
        return Resolution(Rung.OTHER_CHANNEL, tuple(passed_over))

    if claim_available:
        return Resolution(Rung.CLAIM_QUEUE, tuple(passed_over))
    passed_over.append(
        Rejection(Rung.CLAIM_QUEUE, "no confident signal that it restocks here")
    )

    if substitutes > 0:
        return Resolution(
            Rung.SUBSTITUTE, tuple(passed_over), substitutes=substitutes
        )
    passed_over.append(Rejection(Rung.SUBSTITUTE, "no close alternatives in the assortment"))

    return Resolution(Rung.REFUND_ONLY, tuple(passed_over))
