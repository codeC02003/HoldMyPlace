"""Fulfillment routing and batching.

The load-bearing behaviours: cheapest mode that still meets the member's own
date, cost per stop falling with cluster density, and no claim ever held past
the point where it can still arrive in time.
"""

from __future__ import annotations

import pytest

from holdmyplace.domain.claims import Membership
from holdmyplace.domain.money import money
from holdmyplace.domain.routing import (
    BatchParams,
    CostParams,
    Mode,
    RouteBatcher,
    Trigger,
    choose_mode,
)

from .conftest import day, make_claim

COSTS = CostParams()
BATCHING = BatchParams()


def route(claim, available_offset=0, *, costs=COSTS, batching=BATCHING):
    return choose_mode(
        claim, day(available_offset), costs=costs, batching=batching
    )


# -- cost curve ------------------------------------------------------------


def test_cost_per_stop_falls_as_a_cluster_densifies():
    """Density is the whole reason waiting is worth doing."""
    costs = CostParams()
    per_stop = [costs.cost_per_stop(n) for n in (1, 4, 8, 16)]

    assert per_stop == sorted(per_stop, reverse=True)
    assert per_stop[0] > per_stop[-1] * 3


def test_a_lone_stop_costs_more_than_appending_to_an_existing_route():
    costs = CostParams()

    assert costs.cost_per_stop(1) > costs.adjacent_stop


def test_a_route_only_beats_a_detour_once_it_is_dense_enough():
    """Below break-even, batching costs more than piggybacking. Above it, less."""
    costs = CostParams()
    n = costs.break_even_stops()

    assert costs.cost_per_stop(n - 1) > costs.adjacent_stop
    assert costs.cost_per_stop(n) < costs.adjacent_stop


def test_the_default_cluster_size_clears_break_even():
    """Otherwise every batched route would be worse than doing nothing clever."""
    costs = CostParams()

    assert BATCHING.min_cluster >= costs.break_even_stops()
    assert costs.cost_per_stop(BATCHING.min_cluster) < costs.adjacent_stop


def test_no_cluster_wins_when_a_detour_is_cheaper_than_handling():
    never = CostParams(adjacent_stop=money("1.00"), per_stop=money("1.80"))

    assert never.break_even_stops() == -1


def test_an_empty_route_is_an_error():
    with pytest.raises(ValueError, match="at least one"):
        CostParams().route_cost(0)


# -- mode selection --------------------------------------------------------


def test_pickup_wins_whenever_the_member_prefers_it():
    """Zero last-mile cost, so nothing should ever outrank it."""
    claim = make_claim("A", cancel_offset=1, prefers_pickup=True)

    decision = route(claim)

    assert decision.mode is Mode.PICKUP_HOLD
    assert decision.cost_estimate == money("0.00")


def test_plenty_of_slack_routes_to_a_batch():
    claim = make_claim("A", cancel_offset=60)

    assert route(claim).mode is Mode.BATCHED_ROUTE


def test_a_base_member_in_a_hurry_pays_for_express():
    claim = make_claim("A", cancel_offset=4, membership=Membership.BASE)

    decision = route(claim)

    assert decision.mode is Mode.PAID_EXPRESS
    assert decision.fee_charged == COSTS.express_fee


def test_an_executive_member_in_a_hurry_is_not_charged():
    claim = make_claim("A", cancel_offset=4, membership=Membership.EXECUTIVE)

    decision = route(claim)

    assert decision.mode is Mode.EXECUTIVE_FREE
    assert decision.fee_charged == money("0.00")
    assert decision.subsidized


def test_the_executive_benefit_is_not_available_to_base_members():
    base = make_claim("A", cancel_offset=4, membership=Membership.BASE)
    exec_ = make_claim("B", cancel_offset=4, membership=Membership.EXECUTIVE)

    assert route(base).mode is not Mode.EXECUTIVE_FREE
    assert route(exec_).mode is Mode.EXECUTIVE_FREE


def test_slack_is_preferred_over_tier():
    """An Executive member who left slack still gets the cheap mode."""
    claim = make_claim("A", cancel_offset=60, membership=Membership.EXECUTIVE)

    assert route(claim).mode is Mode.BATCHED_ROUTE


def test_a_date_too_tight_even_for_express_falls_back_to_pickup():
    claim = make_claim("A", cancel_offset=1, membership=Membership.EXECUTIVE)

    assert route(claim).mode is Mode.PICKUP_HOLD


def test_every_mode_arrives_by_the_members_own_date():
    for cancel_offset in range(1, 40):
        for tier in Membership:
            claim = make_claim("A", cancel_offset=cancel_offset, membership=tier)
            decision = route(claim)
            assert claim.deliverable_by(decision.arrival), (
                f"{decision.mode} would arrive after the cancel-by date"
            )


def test_filling_a_claim_after_its_deadline_is_an_error():
    claim = make_claim("A", cancel_offset=5)

    with pytest.raises(ValueError, match="cancel-by"):
        route(claim, available_offset=6)


# -- batching --------------------------------------------------------------


def make_batcher(**kwargs):
    batching = BatchParams(**kwargs) if kwargs else BATCHING
    return RouteBatcher(costs=COSTS, batching=batching), batching


def test_a_sparse_cluster_is_held():
    batcher, _ = make_batcher()
    for i in range(3):
        batcher.enqueue(make_claim(f"C{i}", cancel_offset=60), day(0))

    assert batcher.dispatch_due(day(1)) == []
    assert batcher.held == 3


def test_a_dense_cluster_ships_on_density():
    batcher, batching = make_batcher()
    for i in range(batching.min_cluster):
        batcher.enqueue(make_claim(f"C{i}", cancel_offset=60), day(0))

    dispatches = batcher.dispatch_due(day(1))

    assert len(dispatches) == 1
    assert dispatches[0].trigger is Trigger.DENSITY
    assert dispatches[0].stops == batching.min_cluster
    assert batcher.held == 0


def test_a_deadline_forces_a_sparse_route_out():
    """The member's own date is the release valve on waiting for company."""
    batcher, _ = make_batcher()
    batcher.enqueue(make_claim("C0", cancel_offset=3), day(0))

    dispatches = batcher.dispatch_due(day(1))

    assert len(dispatches) == 1
    assert dispatches[0].trigger is Trigger.MUST_GO
    assert dispatches[0].stops == 1


def test_a_forced_route_carries_its_neighbours_along():
    batcher, _ = make_batcher()
    batcher.enqueue(make_claim("urgent", cancel_offset=3), day(0))
    for i in range(3):
        batcher.enqueue(make_claim(f"patient{i}", cancel_offset=90), day(0))

    dispatch = batcher.dispatch_due(day(1))[0]

    assert dispatch.stops == 4
    assert dispatch.cost_per_stop < COSTS.cost_per_stop(1)


def test_clusters_are_kept_separate_by_area():
    batcher, batching = make_batcher()
    for i in range(batching.min_cluster):
        batcher.enqueue(
            make_claim(f"A{i}", cancel_offset=60, zip_code="85719"), day(0)
        )
    batcher.enqueue(make_claim("B0", cancel_offset=60, zip_code="99999"), day(0))

    dispatches = batcher.dispatch_due(day(1))

    assert len(dispatches) == 1
    assert batcher.held == 1


def test_claims_not_yet_available_are_not_shipped():
    batcher, _ = make_batcher()
    batcher.enqueue(make_claim("C0", cancel_offset=3), day(10))

    assert batcher.dispatch_due(day(1)) == []


def test_dispatch_cost_is_divided_across_its_stops():
    batcher, _ = make_batcher()
    for i in range(10):
        batcher.enqueue(make_claim(f"C{i}", cancel_offset=2), day(0))

    dispatch = batcher.dispatch_due(day(1))[0]

    assert dispatch.total_cost == COSTS.route_cost(10)
    assert dispatch.cost_per_stop == money(dispatch.total_cost / 10)


def test_flush_ships_everything_still_held():
    batcher, _ = make_batcher()
    for i in range(2):
        batcher.enqueue(make_claim(f"C{i}", cancel_offset=90), day(0))

    dispatches = batcher.flush(day(5))

    assert sum(d.stops for d in dispatches) == 2
    assert batcher.held == 0


def test_a_tighter_min_cluster_ships_sooner():
    eager, params = make_batcher(min_cluster=2)
    for i in range(2):
        eager.enqueue(make_claim(f"C{i}", cancel_offset=90), day(0))

    assert len(eager.dispatch_due(day(1))) == 1


def test_worst_case_batched_wait_is_the_sum_of_wait_and_transit():
    params = BatchParams(max_wait_days=10, transit_days=2)

    assert params.worst_case_days == 12
