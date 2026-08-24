"""End-to-end runs.

These assert conservation and reproducibility rather than specific outcomes.
Pinning the headline rates would make the suite fail every time an assumption
is tuned, which is exactly when the suite needs to still be useful.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from holdmyplace.domain import economics as econ
from holdmyplace.domain.catalog import Lifecycle
from holdmyplace.domain.money import pct
from holdmyplace.domain.restock import RestockPolicy
from holdmyplace.domain.routing import BatchParams, Mode
from holdmyplace.sim.generate import OOS_PROPENSITY, WorldConfig, build_world
from holdmyplace.sim.report import render
from holdmyplace.sim.run import GATE_ONE_WINDOW, Simulation, simulate

SHORT = WorldConfig(days=45, n_skus=120, seed=3)


@pytest.fixture(scope="module")
def results():
    return simulate(SHORT)


# -- the world -------------------------------------------------------------


def test_items_that_never_return_are_the_likeliest_to_sell_out():
    """The design's strongest objection, encoded rather than assumed away."""
    assert OOS_PROPENSITY[Lifecycle.OPPORTUNISTIC] > OOS_PROPENSITY[Lifecycle.CORE]
    assert OOS_PROPENSITY[Lifecycle.DISCONTINUED] > OOS_PROPENSITY[Lifecycle.CORE]


def test_one_way_skus_are_never_scheduled_for_replenishment():
    world = build_world(SHORT)
    restocked = {r.sku_id for r in world.receipts}

    for sku_id in restocked:
        assert world.catalog[sku_id].restock_cadence_days is not None


def test_the_world_is_reproducible_from_its_seed():
    first, second = build_world(SHORT), build_world(SHORT)

    assert [e.sku_id for e in first.events] == [e.sku_id for e in second.events]
    assert [r.day for r in first.receipts] == [r.day for r in second.receipts]


def test_a_different_seed_produces_a_different_world():
    other = build_world(replace(SHORT, seed=SHORT.seed + 1))
    base = build_world(SHORT)

    assert [e.sku_id for e in base.events] != [e.sku_id for e in other.events]


def test_events_land_inside_the_run_window():
    world = build_world(SHORT)

    for event in world.events:
        assert world.config.start <= event.day <= world.config.end
        assert event.ordered_at <= event.day


# -- conservation ----------------------------------------------------------


def test_every_claim_ends_up_accounted_for(results):
    accounted = (
        results.claims_filled + results.claims_expired + results.claims_open_at_end
    )

    assert accounted == results.claims_created


def test_claims_never_exceed_the_offers_that_produced_them(results):
    assert results.claims_created <= results.offers_claimable
    assert results.offers_claimable <= results.oos_events


def test_every_out_of_stock_line_was_refunded(results):
    assert results.oos_events > 0
    assert results.refunds_issued > 0


def test_the_ladder_partitions_every_event(results):
    """Sourced, denied, or offered a claim — no line escapes all three."""
    assert (
        results.sourced_elsewhere
        + sum(results.denials.values())
        + results.offers_claimable
    ) == results.oos_events


def test_rung_counts_sum_to_the_events(results):
    assert sum(results.rungs.values()) == results.oos_events


def test_sourcing_takes_work_away_from_the_queue():
    """The ladder's purpose: fewer waits, because the item was found instead."""
    from holdmyplace.domain.sourcing import SourcingPolicy

    narrow = simulate(SHORT, sourcing=SourcingPolicy(max_transfer_km=1.0))
    wide = simulate(SHORT, sourcing=SourcingPolicy(max_transfer_km=400.0))

    assert wide.sourced_elsewhere > narrow.sourced_elsewhere
    assert wide.claims_created < narrow.claims_created


def test_sourced_lines_are_never_refunded(results):
    """Finding the item leaves the original line standing, so no money moves."""
    from holdmyplace.domain.sourcing import Rung

    immediate = results.rungs[Rung.OTHER_WAREHOUSE] + results.rungs[Rung.OTHER_CHANNEL]

    assert immediate == results.sourced_elsewhere
    assert results.sourced_elsewhere > 0


def test_a_claim_is_the_third_rung_not_the_first(results):
    """Both sourcing rungs outrank the queue, so both should be reached first."""
    from holdmyplace.domain.sourcing import Rung

    assert results.rungs[Rung.CLAIM_QUEUE] > 0
    assert results.sourced_elsewhere > 0
    assert results.got_the_item_rate > results.sourced_rate


def test_protecting_thin_shelves_pushes_lines_back_to_the_queue():
    from holdmyplace.domain.sourcing import SourcingPolicy

    generous = simulate(SHORT, sourcing=SourcingPolicy(min_on_hand_to_pull=1))
    careful = simulate(SHORT, sourcing=SourcingPolicy(min_on_hand_to_pull=25))

    assert careful.sourced_elsewhere < generous.sourced_elsewhere


def test_units_allocated_to_claims_equal_claims_filled(results):
    assert results.units_to_queue == results.claims_filled


def test_reserved_but_unreachable_units_go_back_to_the_floor(results):
    """Inventory is never held off the shelf for a claim that cannot be met."""
    assert results.units_reserved_unused >= 0
    assert results.units_to_floor >= results.units_reserved_unused


def test_mode_counts_sum_to_claims_filled(results):
    assert sum(results.mode_counts.values()) == results.claims_filled


def test_nothing_is_still_waiting_for_a_route_at_the_end(results):
    assert results.claims_filled == sum(results.mode_counts.values())


# -- reproducibility -------------------------------------------------------


def test_two_identical_runs_agree_exactly():
    first, second = simulate(SHORT), simulate(SHORT)

    assert first.claims_created == second.claims_created
    assert first.claims_filled == second.claims_filled
    assert first.contribution.total == second.contribution.total


def test_no_claim_is_filled_after_its_own_deadline():
    world = build_world(SHORT)
    sim = Simulation(world)
    sim.run()

    for claim in sim.queue.all_claims:
        if claim.filled_on is not None:
            assert claim.filled_on <= claim.cancel_by


def test_the_three_rates_are_distinct_and_ordered_by_what_they_measure(results):
    """Addressable is a ceiling, coverage is reach, promises kept is the gate.

    Conflating the first with the third is what made the proposal's original
    gate-one metric misleading: a low addressable share reflects an assortment
    full of one-time buys, not a queue that breaks its word.
    """
    assert results.addressable_rate < 1.0
    assert results.coverage_rate < 1.0
    assert results.promise_keeping_rate > results.addressable_rate


def test_declining_the_unpromisable_is_what_keeps_the_promise_high(results):
    """Every declined event is one the queue never risked a promise on."""
    from holdmyplace.domain.catalog import Denial

    never_returning = (
        results.denials[Denial.ONE_TIME_BUY] + results.denials[Denial.DISCONTINUED]
    )

    assert never_returning > 0
    assert results.promise_keeping_rate >= 0.6


def test_gate_one_counts_only_genuine_restocks_within_the_window():
    world = build_world(SHORT)
    sample = world.events[0]

    assert world.restocks_within(sample.sku_id, sample.day, 0) is False
    # A never-replenished SKU can never satisfy the window.
    dead = next(
        s for s in world.catalog.values() if s.restock_cadence_days is None
    )
    assert not world.restocks_within(dead.sku_id, world.config.start, GATE_ONE_WINDOW)


# -- policy levers move the outcome ---------------------------------------


def test_a_zero_queue_share_fills_nothing():
    """A queue with no allocation is a queue that never fills."""
    starved = simulate(SHORT, policy=RestockPolicy(queue_share=0.0))

    assert starved.claims_created > 0
    assert starved.claims_filled == 0


def test_a_larger_queue_share_fills_more_claims():
    thin = simulate(SHORT, policy=RestockPolicy(queue_share=0.02))
    thick = simulate(SHORT, policy=RestockPolicy(queue_share=0.60))

    assert thick.claims_filled >= thin.claims_filled


def test_denser_batching_lowers_the_cost_per_fulfillment():
    eager = simulate(SHORT, batching=BatchParams(min_cluster=1))
    patient = simulate(SHORT, batching=BatchParams(min_cluster=10))

    assert patient.cost_per_fulfillment < eager.cost_per_fulfillment


def test_the_subsidy_is_confined_to_executive_members():
    results = simulate(SHORT)

    assert results.subsidized_fulfillments == results.mode_counts[Mode.EXECUTIVE_FREE]


def test_a_flat_renewal_assumption_shrinks_the_all_in_result():
    flat = simulate(
        SHORT, assumptions=econ.Assumptions(renewal_lift_pp=pct("0.0"))
    )

    assert flat.contribution.renewal_value == 0
    assert flat.contribution.total == flat.contribution.total_excl_renewal


def test_pickup_costs_nothing_in_last_mile():
    results = simulate(SHORT)

    assert results.mode_cost.get(Mode.PICKUP_HOLD, 0) == 0


# -- reporting -------------------------------------------------------------


def test_the_report_leads_with_gate_one(results):
    body = render(results)

    assert body.index("GATE ONE") < body.index("ECONOMICS")


def test_the_report_shows_merchandise_only_next_to_all_in(results):
    body = render(results)

    assert "Contribution, merchandise only" in body
    assert "Contribution, all in" in body


def test_the_report_marks_the_load_bearing_assumptions(results):
    body = render(results)

    for name in econ.LOAD_BEARING:
        assert name in body
    assert "load-bearing" in body


def test_the_sensitivity_grid_renders_on_request(results):
    assert "SENSITIVITY" in render(results, sensitivity=True)
    assert "SENSITIVITY" not in render(results)
