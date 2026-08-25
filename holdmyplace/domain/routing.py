"""Fulfillment routing: getting a restocked unit to the member who claimed it.

Order cadence is the constraint that shapes this module. Members buy in bulk and
reorder every few weeks, so "we'll add it to your next order" can mean a
three-week wait on an item that restocked in three days, long enough for the
member to buy it elsewhere and forget the claim existed. Fulfillment has to beat
the reorder cycle or the queue is pointless.

What makes that affordable is the deadline. A normal order carries a promised
window and cannot be held; a claim carries slack. Claims can therefore be
accumulated by area and dispatched only once a cluster is dense enough to be
cheap, with a member's own date acting as the release valve.

No mode ever dispatches a single item on its own trip. At roughly 11%
merchandise margin a $12 item earns about $1.30, and no stop costs less.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum

from .claims import Claim, Membership
from .money import ZERO, money


class Mode(str, Enum):
    PICKUP_HOLD = "pickup_hold"
    """Held at the warehouse front for the member to collect.

    Costs nothing in last mile and pulls forward an in-warehouse trip, where
    the basket is largest. Strictly better than a refund, and the only mode
    that needs no business case at all.
    """

    BATCHED_ROUTE = "batched_route"
    """Held until the member's area has enough claims to route densely."""

    PAID_EXPRESS = "paid_express"
    """Added to a nearby existing stop, with the member covering the cost."""

    EXECUTIVE_FREE = "executive_free"
    """The same stop, unpriced, as an Executive membership benefit.

    The one subsidized mode. Gated to the tier that already pays a premium so
    it reads as a benefit rather than a cost line, and so the spend lands on
    the members whose renewal is worth most.
    """


@dataclass(frozen=True, slots=True)
class CostParams:
    """Last-mile cost structure.

    `route_fixed` is what it costs to put a vehicle on the road at all;
    `per_stop` is the marginal handling once it is out there. Cost per stop is
    therefore strictly decreasing in cluster size, which is the entire reason
    waiting for density is worth doing.
    """

    route_fixed: Decimal = money("18.00")
    per_stop: Decimal = money("1.80")
    adjacent_stop: Decimal = money("4.00")
    """Marginal cost of appending one stop to a route already passing nearby."""
    express_fee: Decimal = money("4.99")
    pickup_hold: Decimal = ZERO

    def route_cost(self, stops: int) -> Decimal:
        if stops <= 0:
            raise ValueError("a route needs at least one stop")
        return money(self.route_fixed + self.per_stop * stops)

    def cost_per_stop(self, stops: int) -> Decimal:
        return money(self.route_cost(stops) / stops)

    def break_even_stops(self) -> int:
        """Smallest cluster where a purpose-built route beats a detour stop.

        Appending to a route already passing nearby carries no fixed cost, so
        for small clusters the detour is genuinely cheaper, because a dedicated
        vehicle only wins once enough stops amortize putting it on the road.
        Batching below this size costs more than it saves, which makes this the
        floor that `BatchParams.min_cluster` has to clear to be worth doing.

        Returns -1 when no cluster size ever wins.
        """
        headroom = self.adjacent_stop - self.per_stop
        if headroom <= 0:
            return -1
        return int(self.route_fixed / headroom) + 1


@dataclass(frozen=True, slots=True)
class BatchParams:
    """When a held cluster is allowed to go out."""

    min_cluster: int = 12
    """Stops below which a route is not worth putting on the road.

    Must clear `CostParams.break_even_stops()` or batching is more expensive
    than simply appending each claim to a route already passing nearby.
    """
    max_wait_days: int = 10
    """Longest a claim waits for company before it goes anyway."""
    transit_days: int = 2
    express_days: int = 2
    zip_precision: int = 3

    @property
    def worst_case_days(self) -> int:
        """Longest a batched claim can take from availability to arrival."""
        return self.max_wait_days + self.transit_days


@dataclass(frozen=True, slots=True)
class Decision:
    """How one filled claim will be delivered, and what it costs."""

    claim_id: str
    mode: Mode
    arrival: date
    """Latest arrival the chosen mode guarantees. Always <= cancel_by."""
    fee_charged: Decimal = ZERO
    cost_estimate: Decimal = ZERO
    """Provisional. Batched claims get their real cost when the route ships."""

    @property
    def subsidized(self) -> bool:
        return self.mode is Mode.EXECUTIVE_FREE


def choose_mode(
    claim: Claim,
    available_on: date,
    *,
    costs: CostParams,
    batching: BatchParams,
) -> Decision:
    """Pick the cheapest mode that still meets the member's own deadline.

    Ordered by cost to the company, not by speed: pickup first, then batching,
    then a priced express stop, and only then the subsidized one. A member who
    left plenty of slack never triggers a mode that costs money.
    """
    slack = claim.slack_days(available_on)
    if slack < 0:
        raise ValueError(f"{claim.claim_id}: filled after its own cancel-by date")

    if claim.prefers_pickup:
        return Decision(
            claim_id=claim.claim_id,
            mode=Mode.PICKUP_HOLD,
            arrival=available_on,
            cost_estimate=costs.pickup_hold,
        )

    if slack >= batching.worst_case_days:
        return Decision(
            claim_id=claim.claim_id,
            mode=Mode.BATCHED_ROUTE,
            arrival=available_on + timedelta(days=batching.worst_case_days),
            cost_estimate=costs.cost_per_stop(batching.min_cluster),
        )

    express_arrival = available_on + timedelta(days=batching.express_days)
    if not claim.deliverable_by(express_arrival):
        # Too tight even for express. The claim was filled but cannot be
        # delivered in time; hold it for pickup and tell the member.
        return Decision(
            claim_id=claim.claim_id,
            mode=Mode.PICKUP_HOLD,
            arrival=available_on,
            cost_estimate=costs.pickup_hold,
        )

    if claim.membership is Membership.EXECUTIVE:
        return Decision(
            claim_id=claim.claim_id,
            mode=Mode.EXECUTIVE_FREE,
            arrival=express_arrival,
            cost_estimate=costs.adjacent_stop,
        )

    return Decision(
        claim_id=claim.claim_id,
        mode=Mode.PAID_EXPRESS,
        arrival=express_arrival,
        fee_charged=costs.express_fee,
        cost_estimate=costs.adjacent_stop,
    )


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------


class Trigger(str, Enum):
    DENSITY = "density"
    """The cluster reached a size worth routing."""

    MUST_GO = "must_go"
    """A member's deadline forced the route out early, whatever its size."""


@dataclass(frozen=True, slots=True)
class Dispatch:
    area: str
    trigger: Trigger
    claim_ids: tuple[str, ...]
    shipped_on: date
    total_cost: Decimal
    cost_per_stop: Decimal

    @property
    def stops(self) -> int:
        return len(self.claim_ids)


@dataclass(slots=True)
class _Pending:
    claim_id: str
    ready_on: date
    must_go_by: date


class RouteBatcher:
    """Holds claims by area until routing them is cheap, or until it can't wait.

    The member's declared deadline is what makes the waiting legitimate, and
    it is also the release valve, so no claim is ever held past the point where
    it can still arrive in time.
    """

    def __init__(self, *, costs: CostParams, batching: BatchParams) -> None:
        self._costs = costs
        self._batching = batching
        self._pending: dict[str, list[_Pending]] = defaultdict(list)

    def enqueue(self, claim: Claim, available_on: date) -> None:
        area = claim.zip_code[: self._batching.zip_precision]
        latest_ship = claim.cancel_by - timedelta(days=self._batching.transit_days)
        self._pending[area].append(
            _Pending(
                claim_id=claim.claim_id,
                ready_on=available_on,
                must_go_by=latest_ship,
            )
        )

    @property
    def held(self) -> int:
        return sum(len(items) for items in self._pending.values())

    def dispatch_due(self, as_of: date) -> list[Dispatch]:
        """Ship every cluster that is either dense enough or out of time."""
        shipped: list[Dispatch] = []

        for area in sorted(self._pending):
            waiting = [p for p in self._pending[area] if p.ready_on <= as_of]
            if not waiting:
                continue

            forced = any(p.must_go_by <= as_of for p in waiting)
            dense = len(waiting) >= self._batching.min_cluster
            if not (forced or dense):
                continue

            waiting.sort(key=lambda p: (p.must_go_by, p.claim_id))
            claim_ids = tuple(p.claim_id for p in waiting)
            total = self._costs.route_cost(len(claim_ids))
            shipped.append(
                Dispatch(
                    area=area,
                    trigger=Trigger.MUST_GO if forced else Trigger.DENSITY,
                    claim_ids=claim_ids,
                    shipped_on=as_of,
                    total_cost=total,
                    cost_per_stop=money(total / len(claim_ids)),
                )
            )
            remaining = [p for p in self._pending[area] if p.ready_on > as_of]
            self._pending[area] = remaining

        return shipped

    def flush(self, as_of: date) -> list[Dispatch]:
        """Ship everything still held, for end-of-simulation accounting."""
        shipped: list[Dispatch] = []
        for area in sorted(self._pending):
            waiting = self._pending[area]
            if not waiting:
                continue
            claim_ids = tuple(sorted(p.claim_id for p in waiting))
            total = self._costs.route_cost(len(claim_ids))
            shipped.append(
                Dispatch(
                    area=area,
                    trigger=Trigger.MUST_GO,
                    claim_ids=claim_ids,
                    shipped_on=as_of,
                    total_cost=total,
                    cost_per_stop=money(total / len(claim_ids)),
                )
            )
            self._pending[area] = []
        return shipped
