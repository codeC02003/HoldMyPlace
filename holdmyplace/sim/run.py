"""The simulation loop and its results.

One simulated day, in order:

  1. Receipts land. Each is split between the claim queue and the sales floor,
     the queue's share is allocated first-in-first-out, and every filled claim
     is routed to a fulfillment mode.
  2. Batched clusters that are dense enough, or out of time, ship.
  3. New out-of-stock lines are resolved by walking the sourcing ladder:
     another warehouse, the other channel, then a claim, then substitutes, then
     a bare refund. A claim is the third rung, so the queue only sees lines the
     network genuinely cannot fill today.
  4. Claims past their member-declared date expire; claims approaching it get
     their single nudge.

Nothing in here reads the wall clock. Every date is derived from the world's
start date so a given seed always produces the same run.
"""

from __future__ import annotations

import argparse
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from ..domain import economics as econ
from ..domain.catalog import Denial, stale_flag_candidates
from ..domain.claims import Claim, DeadlinePreset, resolve_deadline
from ..domain.money import ZERO, money
from ..domain.offers import Feasibility, assess_deadline, resolve_line
from ..domain.queue import ClaimQueue, DemandCluster, demand_signal
from ..domain.restock import RestockPolicy, split_receipt
from ..domain.sourcing import Rung, SourcingPolicy
from ..domain.routing import (
    BatchParams,
    CostParams,
    Dispatch,
    Mode,
    RouteBatcher,
    choose_mode,
)
from .generate import World, WorldConfig, build_world

#: Days a SKU has to reappear to count as restockable.
GATE_ONE_WINDOW = 30

#: Thresholds on the promise-keeping rate: of claims actually filed, the share
#: filled before the member's own deadline.
#:
#: The proposal put gate one on a different number: the share of *all*
#: out-of-stock events falling on SKUs that restock within 30 days. Running the
#: model showed that to be the wrong test. It counts events the design never
#: makes a promise about. A one-time buy is refunded cleanly, with no claim
#: offered and so nothing to break, and it therefore reads as a failure when
#: eligibility screening is doing exactly its job. The restockable share is
#: still worth reporting, as the ceiling on how much of the problem a queue can
#: address at all, but the gate belongs on promises kept.
GATE_PASS = 0.75
GATE_FAIL = 0.60


@dataclass(slots=True)
class Results:
    """Everything the run measured."""

    world: World
    assumptions: econ.Assumptions
    policy: RestockPolicy

    oos_events: int = 0
    refunds_issued: Decimal = ZERO
    offers_claimable: int = 0
    denials: Counter = field(default_factory=Counter)
    rungs: Counter = field(default_factory=Counter)
    sourced_elsewhere: int = 0
    """Lines the network filled without a refund or a wait."""

    claims_created: int = 0
    claims_filled: int = 0
    claims_expired: int = 0
    claims_open_at_end: int = 0
    declined_after_warning: int = 0
    extended_after_warning: int = 0
    proceeded_after_warning: int = 0
    nudges_sent: int = 0

    units_to_queue: int = 0
    units_to_floor: int = 0
    units_reserved_unused: int = 0

    fill_days: list[int] = field(default_factory=list)
    mode_counts: Counter = field(default_factory=Counter)
    mode_cost: dict = field(default_factory=dict)
    contribution: econ.Contribution = econ.EMPTY
    dispatches: list[Dispatch] = field(default_factory=list)

    gate_one_restockable: int = 0
    demand_clusters: list[DemandCluster] = field(default_factory=list)
    stale_flags: list[str] = field(default_factory=list)

    # -- derived ----------------------------------------------------------

    @property
    def claim_optin_rate(self) -> float:
        return _ratio(self.claims_created, self.offers_claimable)

    @property
    def coverage_rate(self) -> float:
        """Share of out-of-stock lines where a claim was offered at all."""
        return _ratio(self.offers_claimable, self.oos_events)

    @property
    def sourced_rate(self) -> float:
        """Share resolved by finding the item, with no wait and no refund.

        The ladder's payoff. Every point here is a line that would have been a
        queue entry, or a refund, under a design that reached for the queue
        first.
        """
        return _ratio(self.sourced_elsewhere, self.oos_events)

    @property
    def got_the_item_rate(self) -> float:
        """Share of lines where the member ends up with what they ordered."""
        return _ratio(self.sourced_elsewhere + self.claims_filled, self.oos_events)

    @property
    def settled_claims(self) -> int:
        """Claims that reached an outcome: filled, or expired unfilled.

        Claims still open at the horizon are undecided, not failures. Counting
        them against the promise rate makes a short run look worse than a long
        one for no reason other than where the window happened to close.
        """
        return self.claims_filled + self.claims_expired

    @property
    def promise_keeping_rate(self) -> float:
        """Of claims that reached an outcome, the share filled in time.

        The actual gate. Measured over settled claims only, which is what makes
        it comparable across run lengths.
        """
        return _ratio(self.claims_filled, self.settled_claims)

    @property
    def undecided_rate(self) -> float:
        """Share of claims still waiting when the run ended."""
        return _ratio(self.claims_open_at_end, self.claims_created)

    @property
    def addressable_rate(self) -> float:
        """Share of out-of-stock lines on SKUs that restocked within 30 days.

        The ceiling on how much of the problem any queue could address.
        Context for the coverage figure, not a pass/fail test in itself.
        """
        return _ratio(self.gate_one_restockable, self.oos_events)

    @property
    def gate_verdict(self) -> str:
        rate = self.promise_keeping_rate
        if rate >= GATE_PASS:
            return "PASS"
        if rate < GATE_FAIL:
            return "FAIL"
        return "MARGINAL"

    @property
    def median_fill_days(self) -> float | None:
        return statistics.median(self.fill_days) if self.fill_days else None

    @property
    def total_stop_cost(self) -> Decimal:
        return self.contribution.stop_cost

    @property
    def cost_per_fulfillment(self) -> Decimal:
        if not self.claims_filled:
            return ZERO
        return money(self.total_stop_cost / self.claims_filled)

    @property
    def subsidized_fulfillments(self) -> int:
        return self.mode_counts[Mode.EXECUTIVE_FREE]

    @property
    def breakeven_lift(self) -> Decimal:
        return econ.breakeven_lift(
            self.assumptions, stop_cost=self.assumptions_stop_cost
        )

    @property
    def assumptions_stop_cost(self) -> Decimal:
        """Observed cost of a subsidized stop, falling back to the estimate."""
        observed = self.mode_cost.get(Mode.EXECUTIVE_FREE)
        count = self.mode_counts[Mode.EXECUTIVE_FREE]
        if observed and count:
            return money(observed / count)
        return money("4.00")


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


class Simulation:
    def __init__(
        self,
        world: World,
        *,
        policy: RestockPolicy | None = None,
        costs: CostParams | None = None,
        batching: BatchParams | None = None,
        assumptions: econ.Assumptions | None = None,
        sourcing: SourcingPolicy | None = None,
    ) -> None:
        import random

        self.world = world
        self.policy = policy or RestockPolicy()
        self.costs = costs or CostParams()
        self.batching = batching or BatchParams()
        self.assumptions = assumptions or econ.Assumptions()
        self.sourcing = sourcing or SourcingPolicy()

        self.queue = ClaimQueue()
        self.batcher = RouteBatcher(costs=self.costs, batching=self.batching)
        self.results = Results(
            world=world, assumptions=self.assumptions, policy=self.policy
        )

        # Decisions awaiting a route dispatch, keyed by claim id.
        self._deferred: dict[str, tuple] = {}
        self._last_received = dict(world.last_received)
        self._claim_seq = 0
        # The day currently being processed. `_arrival_for` is handed to the
        # queue as a callable, so it reads the day from here rather than from a
        # closure rebuilt for every receipt.
        self._today = world.config.start
        # Member behaviour is stochastic but reproducible, and kept on a
        # separate stream from world generation so changing one does not
        # reshuffle the other.
        self._rng = random.Random(world.config.seed * 7919 + 13)

    # -- main loop --------------------------------------------------------

    def run(self) -> Results:
        cfg = self.world.config
        receipts = self.world.receipts_by_day()
        events = self.world.events_by_day()

        for offset in range(cfg.days):
            today = cfg.start + timedelta(days=offset)
            self._today = today
            for receipt in receipts.get(today, ()):
                self._handle_receipt(receipt.sku_id, receipt.units, today)
            self._ship_batches(today)
            for event in events.get(today, ()):
                self._handle_oos(event, today)
            self._close_out_day(today)

        self._finalize(cfg.end)
        return self.results

    # -- receipts ---------------------------------------------------------

    def _handle_receipt(self, sku_id: str, units: int, today: date) -> None:
        r = self.results
        self._last_received[sku_id] = today

        open_claims = self.queue.open_count(sku_id)
        split = split_receipt(units, open_claims, self.policy)
        if split.to_queue == 0:
            r.units_to_floor += units
            return

        plan = self.queue.plan(sku_id, split.to_queue, self._arrival_for)
        filled = self.queue.commit(plan, today)

        # Units reserved for claims that turned out to be unreachable go back
        # to the floor rather than sitting off it for nobody.
        r.units_to_queue += len(filled)
        r.units_reserved_unused += plan.units_unused
        r.units_to_floor += split.to_floor + plan.units_unused
        r.claims_filled += len(filled)

        for claim in filled:
            self._route(claim, today)

    def _arrival_for(self, claim: Claim) -> date:
        """Fastest date this member could receive a unit available today.

        Pickup is same-day, so a member collecting at the warehouse can be
        served by a receipt that no delivery could reach in time.
        """
        base = self._today
        if claim.prefers_pickup:
            return base
        return base + timedelta(days=self.batching.express_days)

    def _route(self, claim: Claim, today: date) -> None:
        decision = choose_mode(
            claim, today, costs=self.costs, batching=self.batching
        )
        sku = self.world.catalog[claim.sku_id]
        price = claim.price_on(decision.arrival, sku.unit_price)

        if decision.mode is Mode.BATCHED_ROUTE:
            # Cost is unknown until the route ships and can be divided by its
            # stops, so scoring waits for the dispatch.
            self.batcher.enqueue(claim, today)
            self._deferred[claim.claim_id] = (decision, price, claim)
            return

        self._score(decision, price, claim, decision.arrival, None)

    def _score(
        self,
        decision,
        price: Decimal,
        claim: Claim,
        arrival: date,
        actual_stop_cost: Decimal | None,
    ) -> None:
        r = self.results
        contribution = econ.evaluate(
            decision,
            price=price,
            tier=claim.membership,
            a=self.assumptions,
            actual_stop_cost=actual_stop_cost,
        )
        r.contribution = r.contribution + contribution
        r.mode_counts[decision.mode] += 1
        r.mode_cost[decision.mode] = money(
            r.mode_cost.get(decision.mode, ZERO) + contribution.stop_cost
        )
        r.fill_days.append((arrival - claim.created_at).days)

    def _ship_batches(self, today: date) -> None:
        for dispatch in self.batcher.dispatch_due(today):
            self._record_dispatch(dispatch)

    def _record_dispatch(self, dispatch: Dispatch) -> None:
        self.results.dispatches.append(dispatch)
        arrival = dispatch.shipped_on + timedelta(days=self.batching.transit_days)
        for claim_id in dispatch.claim_ids:
            decision, price, claim = self._deferred.pop(claim_id)
            self._score(decision, price, claim, arrival, dispatch.cost_per_stop)

    # -- out-of-stock lines -----------------------------------------------

    def _handle_oos(self, event, today: date) -> None:
        r = self.results
        r.oos_events += 1

        sku = self.world.catalog[event.sku_id]
        line = resolve_line(
            sku,
            today,
            line_total=event.line_total,
            channel=event.channel,
            last_received=self._last_received.get(event.sku_id),
            nearby=event.nearby,
            other_channel_has_stock=event.other_channel_has_stock,
            substitutes=event.substitutes,
            policy=self.sourcing,
        )
        offer = line.offer
        r.rungs[line.rung] += 1

        if self.world.restocks_within(event.sku_id, today, GATE_ONE_WINDOW):
            r.gate_one_restockable += 1

        if line.resolution.immediate:
            # The item was found. The original line stands, so there is nothing
            # to refund and nothing to wait for.
            r.sourced_elsewhere += 1
            return

        # Every remaining rung refunds first, before anything else is decided.
        r.refunds_issued = money(r.refunds_issued + offer.refund_amount)

        if line.rung is not Rung.CLAIM_QUEUE:
            r.denials[offer.denial or Denial.NO_RESTOCK_SIGNAL] += 1
            return

        r.offers_claimable += 1
        cfg = self.world.config
        if self._rng.random() >= cfg.claim_optin_rate:
            return

        # `build_offer` guarantees at least one usable preset on a claimable
        # offer, so this list is never empty.
        presets = [p for p in offer.presets(today) if p is not DeadlinePreset.EXACT_DATE]
        weights = [cfg.deadline_mix.get(p, 0.0) for p in presets]
        if sum(weights) == 0:
            weights = [1.0] * len(presets)
        preset = self._rng.choices(presets, weights=weights, k=1)[0]
        cancel_by = resolve_deadline(
            preset, today, ceiling=offer.latest_cancel_by
        )

        verdict = assess_deadline(offer, cancel_by)
        if verdict is not Feasibility.LIKELY:
            roll = self._rng.random()
            if roll < cfg.proceed_past_warning_rate:
                r.proceeded_after_warning += 1
            elif roll < cfg.proceed_past_warning_rate + cfg.extend_on_warning_rate:
                # The member takes the suggested later date.
                assert offer.estimate is not None
                extended = offer.estimate.worst_case + timedelta(days=2)
                if offer.latest_cancel_by is not None:
                    extended = min(extended, offer.latest_cancel_by)
                if extended <= today:
                    r.declined_after_warning += 1
                    return
                cancel_by = extended
                r.extended_after_warning += 1
            else:
                r.declined_after_warning += 1
                return

        self._claim_seq += 1
        claim = Claim(
            claim_id=f"C{self._claim_seq:06d}",
            member_id=event.member_id,
            sku_id=event.sku_id,
            zip_code=event.zip_code,
            locked_price=event.line_total,
            ordered_at=event.ordered_at,
            created_at=today,
            cancel_by=cancel_by,
            membership=event.membership,
            prefers_pickup=event.prefers_pickup,
        )
        self.queue.add(claim)
        r.claims_created += 1

    # -- housekeeping -----------------------------------------------------

    def _close_out_day(self, today: date) -> None:
        r = self.results
        for claim in self.queue.due_for_nudge(today):
            claim.mark_nudged()
            r.nudges_sent += 1
        r.claims_expired += len(self.queue.expire_lapsed(today))

    def _finalize(self, last_day: date) -> None:
        r = self.results
        for dispatch in self.batcher.flush(last_day):
            self._record_dispatch(dispatch)
        r.claims_open_at_end = sum(
            1 for c in self.queue.all_claims if c.is_open
        )
        # Aggregated at full ZIP: within a single metro every claim shares the
        # three-digit prefix, so a coarser key would report one useless cluster.
        r.demand_clusters = demand_signal(self.queue, last_day, zip_precision=5)[:8]
        r.stale_flags = stale_flag_candidates(
            self.world.catalog, self._last_received, last_day
        )[:10]


def simulate(
    cfg: WorldConfig | None = None,
    *,
    policy: RestockPolicy | None = None,
    costs: CostParams | None = None,
    batching: BatchParams | None = None,
    assumptions: econ.Assumptions | None = None,
    sourcing: SourcingPolicy | None = None,
) -> Results:
    """Build a world from `cfg` and run it."""
    world = build_world(cfg)
    sim = Simulation(
        world,
        policy=policy,
        costs=costs,
        batching=batching,
        assumptions=assumptions,
        sourcing=sourcing,
    )
    return sim.run()


def main(argv: list[str] | None = None) -> int:
    from .report import render

    parser = argparse.ArgumentParser(
        prog="holdmyplace.sim.run",
        description="Run the Hold My Place claim-queue simulation.",
    )
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--skus", type=int, default=400)
    parser.add_argument(
        "--queue-share",
        type=float,
        default=0.25,
        help="fraction of each receipt reserved for claim holders",
    )
    parser.add_argument(
        "--min-cluster",
        type=int,
        default=8,
        help="stops a batched route waits for before shipping",
    )
    parser.add_argument(
        "--renewal-lift",
        type=float,
        default=0.5,
        help="percentage points of renewal lift to assume (the unknown)",
    )
    parser.add_argument(
        "--incrementality",
        type=float,
        default=0.30,
        help="share of a top-up basket that is new demand (the other unknown)",
    )
    parser.add_argument(
        "--sensitivity",
        action="store_true",
        help="print the contribution grid over both load-bearing unknowns",
    )
    args = parser.parse_args(argv)

    from ..domain.money import pct

    results = simulate(
        WorldConfig(seed=args.seed, days=args.days, n_skus=args.skus),
        policy=RestockPolicy(queue_share=args.queue_share),
        batching=BatchParams(min_cluster=args.min_cluster),
        assumptions=econ.Assumptions(
            renewal_lift_pp=pct(str(args.renewal_lift)),
            topup_incrementality=pct(str(args.incrementality)),
        ),
    )
    print(render(results, sensitivity=args.sensitivity))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
