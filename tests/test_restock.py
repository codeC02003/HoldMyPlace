"""Return estimation and the receipt split."""

from __future__ import annotations

import pytest

from holdmyplace.domain.catalog import Lifecycle
from holdmyplace.domain.restock import (
    MIN_OFFER_CONFIDENCE,
    RestockPolicy,
    estimate_return,
    split_receipt,
)

from .conftest import day, make_sku


# -- estimation ------------------------------------------------------------


def test_estimate_projects_from_the_last_receipt(today):
    sku = make_sku(cadence=14)

    estimate = estimate_return(sku, today, last_received=day(-4))

    assert estimate.eta == day(10)
    assert not estimate.overdue


def test_core_items_estimate_confidently(today):
    estimate = estimate_return(make_sku(cadence=7), today, day(-1))

    assert estimate.offerable
    assert estimate.confidence > MIN_OFFER_CONFIDENCE


def test_a_disrupted_item_is_less_certain_than_a_core_one(today):
    core = estimate_return(make_sku(cadence=30), today, day(-1))
    disrupted = estimate_return(
        make_sku(lifecycle=Lifecycle.TEMPORARILY_UNAVAILABLE, cadence=30),
        today,
        day(-1),
    )

    assert disrupted.confidence < core.confidence
    assert disrupted.band_days > core.band_days


def test_an_overdue_sku_is_pushed_out_and_discounted(today):
    sku = make_sku(cadence=14)

    on_time = estimate_return(sku, today, day(-2))
    overdue = estimate_return(sku, today, day(-40))

    assert overdue.overdue
    assert overdue.eta > today
    assert overdue.confidence < on_time.confidence


def test_no_cadence_means_no_estimate(today):
    assert estimate_return(make_sku(cadence=None), today, day(-5)) is None


def test_a_lifecycle_that_never_returns_has_no_estimate(today):
    sku = make_sku(lifecycle=Lifecycle.DISCONTINUED, cadence=14)

    assert estimate_return(sku, today, day(-5)) is None


def test_seasonal_arrival_past_the_season_end_is_not_offerable(today):
    sku = make_sku(
        lifecycle=Lifecycle.SEASONAL, cadence=28, season_end=day(10)
    )

    estimate = estimate_return(sku, today, day(-1))

    assert not estimate.offerable


def test_missing_history_falls_back_to_today_as_the_anchor(today):
    estimate = estimate_return(make_sku(cadence=21), today, last_received=None)

    assert estimate.eta == day(21)


def test_worst_case_is_the_late_edge_of_the_band(today):
    estimate = estimate_return(make_sku(cadence=20), today, day(-1))

    assert estimate.worst_case == estimate.eta + __import__(
        "datetime"
    ).timedelta(days=estimate.band_days)


def test_member_copy_gives_a_range_not_a_promise(today):
    estimate = estimate_return(make_sku(cadence=30), today, day(-1))

    copy = estimate.member_copy()

    assert "between" in copy
    assert "guarantee" not in copy


def test_a_tight_band_reads_as_a_single_date(today):
    estimate = estimate_return(make_sku(cadence=7), today, day(-1))

    assert estimate.band_days <= 3
    assert "around" in estimate.member_copy()


# -- the receipt split -----------------------------------------------------


def test_the_queue_share_is_taken_off_the_top(today):
    split = split_receipt(40, open_claims=20, policy=RestockPolicy(queue_share=0.25))

    assert split.to_queue == 10
    assert split.to_floor == 30
    assert split.units == 40


def test_never_reserves_more_than_there_are_claims_waiting():
    """Inventory held off the floor for nobody is what operations would refuse."""
    split = split_receipt(40, open_claims=3, policy=RestockPolicy(queue_share=0.5))

    assert split.to_queue == 3
    assert split.to_floor == 37


def test_an_empty_queue_sends_everything_to_the_floor():
    split = split_receipt(40, open_claims=0, policy=RestockPolicy(queue_share=1.0))

    assert split.to_queue == 0
    assert split.to_floor == 40


def test_a_zero_share_never_fills_the_queue():
    """A queue with no allocation is a queue that never fills. Explicitly so."""
    split = split_receipt(100, open_claims=50, policy=RestockPolicy(queue_share=0.0))

    assert split.to_queue == 0


def test_a_per_receipt_cap_limits_the_reservation():
    policy = RestockPolicy(queue_share=0.9, max_units_per_receipt=5)

    assert split_receipt(100, 100, policy).to_queue == 5


def test_a_floor_minimum_protects_the_shelf():
    policy = RestockPolicy(queue_share=1.0, min_floor_units=6)

    split = split_receipt(10, open_claims=10, policy=policy)

    assert split.to_queue == 4
    assert split.to_floor == 6


def test_a_floor_minimum_larger_than_the_receipt_reserves_nothing():
    policy = RestockPolicy(queue_share=1.0, min_floor_units=50)

    assert split_receipt(10, 10, policy).to_queue == 0


def test_an_empty_receipt_splits_to_nothing():
    split = split_receipt(0, open_claims=10, policy=RestockPolicy())

    assert split.units == 0


def test_the_split_always_conserves_units():
    policy = RestockPolicy(queue_share=0.33, min_floor_units=2)
    for units in range(0, 60, 7):
        for claims in range(0, 30, 5):
            assert split_receipt(units, claims, policy).units == units


@pytest.mark.parametrize("share", [-0.1, 1.5])
def test_an_out_of_range_share_is_rejected(share):
    with pytest.raises(ValueError, match="fraction"):
        RestockPolicy(queue_share=share)


def test_a_negative_floor_minimum_is_rejected():
    with pytest.raises(ValueError, match="negative"):
        RestockPolicy(min_floor_units=-1)


def test_negative_inputs_are_rejected():
    with pytest.raises(ValueError, match="non-negative"):
        split_receipt(-1, 5, RestockPolicy())
