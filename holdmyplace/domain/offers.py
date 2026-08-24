"""What the member is actually shown when a paid line goes out of stock.

This module is the composition point: the eligibility read, the return estimate,
the sourcing ladder, and the member's chosen deadline meet here and produce one
resolution for the line.

A claim is the third rung, not the first. `resolve_line` asks sourcing whether
the item can be found now — another warehouse, the other channel — before it
asks eligibility whether it will come back. Offering a wait to someone whose
item is on a shelf twenty miles away is the failure this ordering prevents.

Two behaviours matter more than the plumbing.

The refund is unconditional and comes first. It is not a branch of this logic
and not something the member trades away — it is issued regardless, and the
claim is a separate, optional, clearly secondary step. Anything that reads as
"wait in line instead of getting your money back" is the failure mode this
design most needs to avoid.

Deadlines that cannot plausibly be met are refused at the moment they are set,
not silently accepted and failed later. Under-promising here is free; queuing a
member into a disappointment costs a support contact and some trust.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum

from .catalog import Channel, Denial, Sku, assess
from .claims import DEADLINE_PROMPT, DeadlinePreset, Membership, PRESET_DAYS
from .money import fmt, money
from .restock import ReturnEstimate, estimate_return
from .sourcing import Resolution, Rung, SourcingPolicy, StockPoint, resolve


class Feasibility(str, Enum):
    """How a chosen deadline compares against the return estimate."""

    LIKELY = "likely"
    """The late edge of the estimate still clears the member's date."""

    UNLIKELY = "unlikely"
    """The estimate straddles the date. Say so before accepting the claim."""

    IMPOSSIBLE = "impossible"
    """Even the optimistic edge misses. Do not offer this deadline."""


@dataclass(frozen=True, slots=True)
class Offer:
    """The out-of-stock resolution presented to one member for one line."""

    sku_id: str
    refund_amount: Decimal
    claimable: bool
    denial: Denial | None = None
    estimate: ReturnEstimate | None = None
    latest_cancel_by: date | None = None

    @property
    def headline(self) -> str:
        """The first thing the member reads. The refund, always."""
        return f"Out of stock — {fmt(self.refund_amount)} refunded to your card."

    @property
    def secondary(self) -> str:
        """The optional second step, or the reason there isn't one."""
        if not self.claimable:
            from .catalog import DENIAL_COPY

            return DENIAL_COPY[self.denial]
        assert self.estimate is not None
        return f"Want us to hold your place? It's {self.estimate.member_copy()}."

    def presets(self, as_of: date) -> list[DeadlinePreset]:
        """Which deadline choices to show, in order.

        Two filters. A preset whose window closes before the item could
        plausibly arrive is withheld rather than shown and then rejected. And a
        preset that runs past an eligibility ceiling is withheld too: offering
        a member "three months" on a seasonal item that will be capped at three
        weeks anyway shows them a choice they are not actually being given.
        """
        if not self.claimable or self.estimate is None:
            return []
        from datetime import timedelta

        usable: list[DeadlinePreset] = []
        for preset, days in PRESET_DAYS.items():
            horizon = as_of + timedelta(days=days)
            if self.latest_cancel_by is not None and horizon > self.latest_cancel_by:
                continue
            if horizon >= self.estimate.eta:
                usable.append(preset)
        usable.append(DeadlinePreset.EXACT_DATE)
        return usable

    @property
    def prompt(self) -> str:
        return DEADLINE_PROMPT


def build_offer(
    sku: Sku,
    as_of: date,
    *,
    line_total: Decimal,
    last_received: date | None = None,
    channel: Channel | None = None,
) -> Offer:
    """Resolve one out-of-stock line into a refund plus, maybe, a claim.

    The refund amount is set before anything else is evaluated, so no path
    through this function can produce a resolution without one.
    """
    refund = money(line_total)

    eligibility = assess(sku, as_of, channel=channel)
    if not eligibility.eligible:
        return Offer(sku.sku_id, refund, False, denial=eligibility.denial)

    estimate = estimate_return(sku, as_of, last_received)
    if estimate is None or not estimate.offerable:
        # Eligible lifecycle, but no confident signal about timing. Fail closed:
        # a claim option that does not appear is invisible to the member.
        return Offer(sku.sku_id, refund, False, denial=Denial.NO_RESTOCK_SIGNAL)

    offer = Offer(
        sku_id=sku.sku_id,
        refund_amount=refund,
        claimable=True,
        estimate=estimate,
        latest_cancel_by=eligibility.latest_cancel_by,
    )

    # A claim with no deadline the member could actually choose is not an offer.
    # This happens when every preset closes before the item could arrive — a
    # seasonal item whose window shuts first, most often. Catching it here keeps
    # "claimable" meaning "there is something to accept".
    if not [p for p in offer.presets(as_of) if p is not DeadlinePreset.EXACT_DATE]:
        return Offer(sku.sku_id, refund, False, denial=Denial.NO_RESTOCK_SIGNAL)

    return offer


def assess_deadline(offer: Offer, cancel_by: date) -> Feasibility:
    """Grade a member's chosen date against the return estimate."""
    if not offer.claimable or offer.estimate is None:
        raise ValueError("cannot assess a deadline on a non-claimable offer")

    if cancel_by < offer.estimate.eta:
        return Feasibility.IMPOSSIBLE
    if cancel_by < offer.estimate.worst_case:
        return Feasibility.UNLIKELY
    return Feasibility.LIKELY


def deadline_warning(offer: Offer, cancel_by: date, verdict: Feasibility) -> str | None:
    """The copy shown when a date is tight or unreachable.

    Returns None when the date is comfortable. Otherwise names the estimate,
    names the member's date, and offers the three honest ways forward.
    """
    if verdict is Feasibility.LIKELY:
        return None
    assert offer.estimate is not None
    tail = "Hold my place anyway · Move the date out · Just refund me"
    if verdict is Feasibility.IMPOSSIBLE:
        return (
            f"This one's {offer.estimate.member_copy()} — after {cancel_by:%b %-d}. "
            f"We won't make your date. {tail}"
        )
    return (
        f"This one's {offer.estimate.member_copy()}, so {cancel_by:%b %-d} "
        f"is tight. {tail}"
    )


@dataclass(frozen=True, slots=True)
class MemberChoice:
    """A member's response to an offer, as the simulation records it."""

    claimed: bool
    preset: DeadlinePreset | None = None
    exact_date: date | None = None
    membership: Membership = Membership.BASE
    prefers_pickup: bool = False
    accepted_warning: bool = False
    """Whether the member proceeded past an UNLIKELY or IMPOSSIBLE warning."""


# ---------------------------------------------------------------------------
# The full resolution: sourcing first, a claim only if nothing else answers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LineResolution:
    """What happens to one out-of-stock line, start to finish.

    Composes the two independent questions. Sourcing asks whether the item can
    be found now — at another warehouse, or in the other channel. Eligibility
    asks whether it will come back here. A claim is only reached when the answer
    to the first is no and to the second is yes, which is the ordering the
    member would choose for themselves: the actual item now beats the actual
    item later, which beats a different item now.
    """

    resolution: Resolution
    offer: Offer

    @property
    def rung(self) -> Rung:
        return self.resolution.rung

    @property
    def refund_amount(self) -> Decimal:
        return self.offer.refund_amount

    @property
    def claim_offered(self) -> bool:
        return self.rung is Rung.CLAIM_QUEUE

    @property
    def refunded(self) -> bool:
        """Whether money goes back.

        Sourcing the item keeps the original line intact, so there is nothing
        to refund — the member gets what they paid for. Every other rung
        refunds first and unconditionally.
        """
        return not self.resolution.immediate

    @property
    def headline(self) -> str:
        if self.resolution.immediate:
            return "Out of stock at your warehouse — but we found it."
        return self.offer.headline

    @property
    def secondary(self) -> str:
        if self.resolution.immediate or self.rung is not Rung.CLAIM_QUEUE:
            return self.resolution.member_copy
        return self.offer.secondary


def resolve_line(
    sku: Sku,
    as_of: date,
    *,
    line_total: Decimal,
    channel: Channel,
    last_received: date | None = None,
    nearby: tuple[StockPoint, ...] = (),
    other_channel_has_stock: bool = False,
    substitutes: int = 0,
    policy: SourcingPolicy | None = None,
) -> LineResolution:
    """Resolve one out-of-stock line by walking the ladder."""
    offer = build_offer(
        sku,
        as_of,
        line_total=line_total,
        last_received=last_received,
        channel=channel,
    )
    resolution = resolve(
        sku,
        channel=channel,
        nearby=nearby,
        other_channel_has_stock=other_channel_has_stock,
        substitutes=substitutes,
        claim_available=offer.claimable,
        policy=policy,
    )
    return LineResolution(resolution=resolution, offer=offer)
