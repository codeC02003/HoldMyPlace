"""The claim queue and its allocation rule.

One invariant governs this module, and the anti-gaming property of the whole
design rests on it:

    Order time decides who gets a unit. The member's deadline decides only
    whether they are still in line.

The deadline is applied as a filter and never as a sort key. A member who
declares an aggressive deadline is passed over by any receipt that cannot
arrive in time, so a tighter date strictly shrinks the set of receipts that can
serve them. Overstating urgency is therefore self-punishing, and honest
reporting needs no verification, no policing, and no abuse team.

Allocation is split into `plan` and `commit`. Planning is pure, which lets the
tests assert the invariant above by re-planning a scenario with one deadline
altered and comparing outcomes.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from .claims import Claim


class SkipReason(str, Enum):
    DEADLINE_UNREACHABLE = "deadline_unreachable"
    """The receipt cannot arrive before the member's cancel-by date."""

    NEEDS_RECONSENT = "needs_reconsent"
    """Price moved materially past the lock; the member must re-approve."""


@dataclass(frozen=True, slots=True)
class Skip:
    claim_id: str
    reason: SkipReason


@dataclass(frozen=True, slots=True)
class AllocationPlan:
    """What a receipt would do, computed without mutating anything."""

    sku_id: str
    arrival: date | None
    """Earliest arrival considered, or None when no claim was in line."""
    fill: tuple[str, ...] = ()
    skip: tuple[Skip, ...] = ()
    units_unused: int = 0

    @property
    def filled(self) -> int:
        return len(self.fill)


class ClaimQueue:
    """All open claims, indexed by SKU and ordered by original order time."""

    def __init__(self) -> None:
        self._by_id: dict[str, Claim] = {}
        self._by_sku: dict[str, list[Claim]] = defaultdict(list)

    # -- membership -------------------------------------------------------

    def add(self, claim: Claim) -> None:
        if claim.claim_id in self._by_id:
            raise ValueError(f"duplicate claim id {claim.claim_id}")
        self._by_id[claim.claim_id] = claim
        self._by_sku[claim.sku_id].append(claim)

    def get(self, claim_id: str) -> Claim:
        return self._by_id[claim_id]

    def __len__(self) -> int:
        return len(self._by_id)

    @property
    def all_claims(self) -> list[Claim]:
        return list(self._by_id.values())

    # -- reads ------------------------------------------------------------

    def open_for(self, sku_id: str) -> list[Claim]:
        """Open claims on one SKU, in allocation order."""
        return sorted(
            (c for c in self._by_sku.get(sku_id, ()) if c.is_open),
            key=lambda c: c.fifo_key,
        )

    def open_count(self, sku_id: str) -> int:
        return sum(1 for c in self._by_sku.get(sku_id, ()) if c.is_open)

    def position_of(self, claim_id: str) -> int:
        """The member-facing "4 of 31". One-indexed."""
        claim = self._by_id[claim_id]
        if not claim.is_open:
            raise ValueError(f"{claim_id} is {claim.status.value}, not queued")
        for index, other in enumerate(self.open_for(claim.sku_id), start=1):
            if other.claim_id == claim_id:
                return index
        raise AssertionError("open claim missing from its own SKU index")

    def open_by_sku(self) -> dict[str, int]:
        return {
            sku_id: count
            for sku_id in self._by_sku
            if (count := self.open_count(sku_id))
        }

    # -- allocation -------------------------------------------------------

    def plan(
        self,
        sku_id: str,
        units: int,
        arrival: date | Callable[[Claim], date],
    ) -> AllocationPlan:
        """Decide which claims a receipt of `units` would fill. Pure.

        Walks the queue strictly in order-time sequence. A claim the receipt
        cannot reach in time is skipped *without consuming a unit* — being
        passed over costs the queue nothing and costs the claim behind it
        nothing, which is what keeps the filter from behaving like a priority.

        `arrival` may be a single date, or a function of the claim when the
        fastest reachable date differs by member — someone collecting at the
        warehouse can be served by a receipt that a delivery could not reach in
        time.
        """
        if units < 0:
            raise ValueError("units cannot be negative")

        arrival_for = arrival if callable(arrival) else (lambda _claim: arrival)

        fill: list[str] = []
        skip: list[Skip] = []
        remaining = units
        earliest: date | None = None

        for claim in self.open_for(sku_id):
            if remaining == 0:
                break
            claim_arrival = arrival_for(claim)
            if earliest is None or claim_arrival < earliest:
                earliest = claim_arrival
            if not claim.deliverable_by(claim_arrival):
                skip.append(Skip(claim.claim_id, SkipReason.DEADLINE_UNREACHABLE))
                continue
            fill.append(claim.claim_id)
            remaining -= 1

        return AllocationPlan(
            sku_id=sku_id,
            arrival=earliest,
            fill=tuple(fill),
            skip=tuple(skip),
            units_unused=remaining,
        )

    def commit(self, plan: AllocationPlan, as_of: date) -> list[Claim]:
        """Apply a plan, returning the claims that were filled."""
        for skipped in plan.skip:
            self._by_id[skipped.claim_id].skip()

        filled: list[Claim] = []
        for claim_id in plan.fill:
            claim = self._by_id[claim_id]
            claim.fill(as_of)
            filled.append(claim)
        return filled

    # -- housekeeping -----------------------------------------------------

    def expire_lapsed(self, as_of: date) -> list[Claim]:
        """Close every claim whose member-declared date has passed.

        This is the whole exit path. Because the refund was already issued when
        the claim was created, expiry costs the member nothing but the wait.
        """
        lapsed = [c for c in self._by_id.values() if c.is_open and c.has_lapsed(as_of)]
        for claim in lapsed:
            claim.expire(as_of)
        return lapsed

    def due_for_nudge(self, as_of: date) -> list[Claim]:
        return sorted(
            (c for c in self._by_id.values() if c.due_for_nudge(as_of)),
            key=lambda c: c.fifo_key,
        )

    def purge_closed(self) -> int:
        """Drop terminal claims from the SKU index to keep scans cheap."""
        removed = 0
        for sku_id, claims in self._by_sku.items():
            keep = [c for c in claims if c.is_open]
            removed += len(claims) - len(keep)
            self._by_sku[sku_id] = keep
        return removed


@dataclass(slots=True)
class DemandCluster:
    """Open claims for one SKU in one delivery area.

    This is the aggregate the design produces as a by-product: a time-bound,
    geographically resolved demand curve on an item that is currently out of
    stock. No retailer can compute it today, because the intent is destroyed at
    the moment of refund.
    """

    sku_id: str
    zip_code: str
    open_claims: int = 0
    expiring_soon: int = 0
    forfeit_value: object = None
    claims: list[Claim] = field(default_factory=list)


def demand_signal(
    queue: ClaimQueue,
    as_of: date,
    horizon_days: int = 21,
    zip_precision: int = 3,
) -> list[DemandCluster]:
    """Aggregate open claims into (SKU, area) clusters, worst first.

    `expiring_soon` and `forfeit_value` are the operationally useful half: they
    convert a queue into a dated freight decision — if the pallet does not land
    by this date, this much confirmed demand is lost in this area.
    """
    from .money import money, ZERO

    buckets: dict[tuple[str, str], DemandCluster] = {}
    for claim in queue.all_claims:
        if not claim.is_open:
            continue
        key = (claim.sku_id, claim.zip_code[:zip_precision])
        cluster = buckets.get(key)
        if cluster is None:
            cluster = DemandCluster(claim.sku_id, key[1], forfeit_value=ZERO)
            buckets[key] = cluster
        cluster.open_claims += 1
        cluster.claims.append(claim)
        if 0 <= claim.slack_days(as_of) <= horizon_days:
            cluster.expiring_soon += 1
            cluster.forfeit_value = money(cluster.forfeit_value + claim.locked_price)

    return sorted(
        buckets.values(),
        key=lambda c: (-c.expiring_soon, -c.open_claims, c.sku_id, c.zip_code),
    )
