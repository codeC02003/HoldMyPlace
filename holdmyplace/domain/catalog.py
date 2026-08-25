"""SKU catalog and the eligibility read.

The central design claim of this system: restock intent is not predicted, it is
read. A buyer already decided months ago whether a SKU gets reordered, and that
decision lives in the item master as a lifecycle status. This module maps that
status onto a queue-eligibility answer and does nothing cleverer than that.

Eligibility fails closed. An unrecognized lifecycle, a seasonal item with no
season end recorded, or any other gap in the data yields "not eligible" rather
than an exception or an optimistic default. A claim option that fails to appear
is invisible to the member; a claim promise that cannot be kept is a support
contact and a trust cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum


class Channel(str, Enum):
    """Where a SKU is sold.

    Online and in-warehouse assortments are separate pools, not one shared
    pool: some items exist only in one. A claim therefore has to ask whether the
    item restocks *in the channel the member ordered through*, not merely
    whether it restocks somewhere.
    """

    WAREHOUSE = "warehouse"
    ONLINE = "online"


BOTH_CHANNELS: frozenset[Channel] = frozenset({Channel.WAREHOUSE, Channel.ONLINE})


class Lifecycle(str, Enum):
    """Item lifecycle status, as an item master would carry it."""

    CORE = "core"
    """Regularly stocked year-round. Reorders are automatic."""

    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    """Normally stocked, currently disrupted upstream. Will return."""

    SEASONAL = "seasonal"
    """Returns on a calendar. Outside its window it is simply absent."""

    OPPORTUNISTIC = "opportunistic"
    """One-time buy, the treasure hunt. Sells through and is gone."""

    DISCONTINUED = "discontinued"
    """Flagged for no further reorder. The asterisk on the shelf sign."""


#: Lifecycles for which a claim may be offered at all. Anything absent from
#: this set is ineligible by default. The allowlist *is* the fail-closed rule.
CLAIMABLE: frozenset[Lifecycle] = frozenset(
    {
        Lifecycle.CORE,
        Lifecycle.TEMPORARILY_UNAVAILABLE,
        Lifecycle.SEASONAL,
    }
)


@dataclass(frozen=True, slots=True)
class Sku:
    """A catalog item.

    `restock_cadence_days` is how often this SKU is replenished at a warehouse
    when it is being replenished at all. For lifecycles that never return it is
    None, and any code reading it must treat that as "no estimate exists"
    rather than substituting a default.
    """

    sku_id: str
    name: str
    unit_price: Decimal
    lifecycle: Lifecycle
    restock_cadence_days: int | None = None
    season_end: date | None = None
    channels: frozenset[Channel] = BOTH_CHANNELS

    def __post_init__(self) -> None:
        if not self.channels:
            raise ValueError(f"{self.sku_id}: a SKU must be sold in some channel")
        if self.restock_cadence_days is not None and self.restock_cadence_days <= 0:
            raise ValueError(f"{self.sku_id}: restock cadence must be positive")

    @property
    def ever_returns(self) -> bool:
        """Whether this SKU is replenished at all, ignoring timing."""
        return self.lifecycle in CLAIMABLE and self.restock_cadence_days is not None


class Denial(str, Enum):
    """Why a claim was not offered. Recorded so the funnel is auditable."""

    ONE_TIME_BUY = "one_time_buy"
    DISCONTINUED = "discontinued"
    SEASON_CLOSED = "season_closed"
    NO_RESTOCK_SIGNAL = "no_restock_signal"
    UNKNOWN_LIFECYCLE = "unknown_lifecycle"
    CHANNEL_UNAVAILABLE = "channel_unavailable"


#: Member-facing copy for each denial. The member is never shown a status code,
#: and never told to wait for something that is not coming.
DENIAL_COPY: dict[Denial, str] = {
    Denial.ONE_TIME_BUY: "This was a one-time buy. Refunded, and here are similar items.",
    Denial.DISCONTINUED: "We're not restocking this one. Refunded, and here are similar items.",
    Denial.SEASON_CLOSED: "This one's out of season. We can tell you when it's back next year.",
    Denial.NO_RESTOCK_SIGNAL: "We can't tell when this will return, so we won't hold your place.",
    Denial.UNKNOWN_LIFECYCLE: "We can't confirm this will restock. Refunded in full.",
    Denial.CHANNEL_UNAVAILABLE: "This one isn't restocked for delivery. Refunded in full.",
}


@dataclass(frozen=True, slots=True)
class Eligibility:
    """The outcome of the eligibility read."""

    eligible: bool
    denial: Denial | None = None
    latest_cancel_by: date | None = None
    """Hard ceiling on the deadline a member may set, when one applies.

    Set for seasonal items, where a claim past the season end can never be
    filled. None means only the global claim-length cap applies.
    """

    @property
    def member_copy(self) -> str | None:
        """What to show a member who was declined, or None if eligible."""
        return None if self.eligible else DENIAL_COPY[self.denial]


ELIGIBLE = Eligibility(eligible=True)


def assess(
    sku: Sku, as_of: date, *, channel: Channel | None = None
) -> Eligibility:
    """Decide whether a claim may be offered for `sku` on `as_of`.

    This is a lookup with boundary checks, not a model. That is the point: it
    is explainable to a buyer, auditable after the fact, and cheap enough to
    run on every out-of-stock line.

    `channel` is where the member ordered. Supplying it rejects a claim the
    channel could never fill. A warehouse-only item cannot be restocked into a
    delivery. Omitting it checks restock intent alone.
    """
    if channel is not None and channel not in sku.channels:
        return Eligibility(False, Denial.CHANNEL_UNAVAILABLE)

    if sku.lifecycle not in CLAIMABLE:
        if sku.lifecycle is Lifecycle.OPPORTUNISTIC:
            return Eligibility(False, Denial.ONE_TIME_BUY)
        if sku.lifecycle is Lifecycle.DISCONTINUED:
            return Eligibility(False, Denial.DISCONTINUED)
        # A lifecycle we do not recognise. Fail closed rather than guess.
        return Eligibility(False, Denial.UNKNOWN_LIFECYCLE)

    if sku.restock_cadence_days is None:
        # Claimable status but no replenishment history to reason from. The
        # item master and the receipt history disagree; do not promise.
        return Eligibility(False, Denial.NO_RESTOCK_SIGNAL)

    if sku.lifecycle is Lifecycle.SEASONAL:
        if sku.season_end is None:
            return Eligibility(False, Denial.NO_RESTOCK_SIGNAL)
        if as_of >= sku.season_end:
            return Eligibility(False, Denial.SEASON_CLOSED)
        return Eligibility(True, latest_cancel_by=sku.season_end)

    return ELIGIBLE


def stale_flag_candidates(
    catalog: dict[str, Sku],
    last_received: dict[str, date],
    as_of: date,
    silence_days: int = 120,
) -> list[str]:
    """SKUs the item master calls claimable but that receipts say are dead.

    This is the one place the design admits it needs to distrust its own input.
    Lifecycle flags go stale: a vendor stops shipping, a buyer moves on, and
    nobody updates the record. Reconciling declared status against actual
    receipt history surfaces the divergence for a human to resolve, and is
    worth doing on its own merits whether or not the queue ever ships.
    """
    cutoff = as_of - timedelta(days=silence_days)
    return sorted(
        sku_id
        for sku_id, sku in catalog.items()
        if sku.lifecycle in CLAIMABLE and last_received.get(sku_id, date.min) < cutoff
    )
