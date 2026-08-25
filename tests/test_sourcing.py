"""The resolution ladder.

The ordering under test: the actual item now beats the actual item later, which
beats a different item now. A claim is the third rung. These tests exist because
the ladder is easy to get backwards, because reaching for the queue first is
the cheapest thing to build and the worst thing for the member.
"""

from __future__ import annotations

import pytest

from holdmyplace.domain.catalog import BOTH_CHANNELS, Channel, Lifecycle
from holdmyplace.domain.sourcing import (
    LADDER,
    Rejection,
    Resolution,
    Rung,
    SourcingPolicy,
    StockPoint,
    resolve,
)

from .conftest import make_sku

POLICY = SourcingPolicy()


def sku_in(*channels: Channel):
    return make_sku(channels=frozenset(channels) if channels else BOTH_CHANNELS)


def run(sku=None, **kwargs):
    kwargs.setdefault("channel", Channel.ONLINE)
    kwargs.setdefault("claim_available", True)
    return resolve(sku or sku_in(), **kwargs)


# -- ordering --------------------------------------------------------------


def test_the_ladder_puts_the_real_item_before_a_substitute():
    """A different product is a worse answer than the one they ordered."""
    assert LADDER.index(Rung.OTHER_WAREHOUSE) < LADDER.index(Rung.SUBSTITUTE)
    assert LADDER.index(Rung.CLAIM_QUEUE) < LADDER.index(Rung.SUBSTITUTE)


def test_the_ladder_puts_now_before_later():
    assert LADDER.index(Rung.OTHER_WAREHOUSE) < LADDER.index(Rung.CLAIM_QUEUE)
    assert LADDER.index(Rung.OTHER_CHANNEL) < LADDER.index(Rung.CLAIM_QUEUE)


def test_refund_only_is_the_last_resort():
    assert LADDER[-1] is Rung.REFUND_ONLY


# -- rung one: another warehouse ------------------------------------------


def test_nearby_stock_wins_over_a_claim():
    """The headline fix: never offer a wait for something on a shelf nearby."""
    r = run(nearby=(StockPoint("Tucson E", 12.0, 9),))

    assert r.rung is Rung.OTHER_WAREHOUSE
    assert r.source.warehouse == "Tucson E"
    assert r.immediate and r.gets_the_item


def test_the_nearest_qualifying_location_is_chosen():
    r = run(
        nearby=(
            StockPoint("Far", 60.0, 40),
            StockPoint("Near", 9.0, 5),
            StockPoint("Mid", 30.0, 20),
        )
    )

    assert r.source.warehouse == "Near"


def test_stock_beyond_the_transfer_radius_is_not_pulled():
    r = run(nearby=(StockPoint("Phoenix", 180.0, 40),))

    assert r.rung is not Rung.OTHER_WAREHOUSE
    assert "beyond the transfer radius" in _reason(r, Rung.OTHER_WAREHOUSE)


def test_a_thin_shelf_is_left_alone():
    """Covering one shortfall by creating another is not sourcing."""
    r = run(nearby=(StockPoint("Tucson E", 12.0, 1),))

    assert r.rung is not Rung.OTHER_WAREHOUSE
    assert "units to spare" in _reason(r, Rung.OTHER_WAREHOUSE)


def test_exactly_the_minimum_on_hand_qualifies():
    r = run(nearby=(StockPoint("Tucson E", 5.0, POLICY.min_on_hand_to_pull),))

    assert r.rung is Rung.OTHER_WAREHOUSE


def test_exactly_the_maximum_distance_qualifies():
    r = run(nearby=(StockPoint("Edge", POLICY.max_transfer_km, 10),))

    assert r.rung is Rung.OTHER_WAREHOUSE


def test_no_other_location_is_reported_as_such():
    assert "no other warehouse" in _reason(run(), Rung.OTHER_WAREHOUSE)


def test_a_wider_radius_reaches_further_stock():
    far = (StockPoint("Phoenix", 180.0, 40),)

    assert run(nearby=far).rung is not Rung.OTHER_WAREHOUSE
    assert (
        resolve(
            sku_in(),
            channel=Channel.ONLINE,
            nearby=far,
            claim_available=True,
            policy=SourcingPolicy(max_transfer_km=250.0),
        ).rung
        is Rung.OTHER_WAREHOUSE
    )


# -- rung two: the other channel ------------------------------------------


def test_the_other_channel_is_used_when_it_has_stock():
    r = run(other_channel_has_stock=True)

    assert r.rung is Rung.OTHER_CHANNEL
    assert r.immediate


def test_a_channel_the_item_is_not_sold_in_is_skipped():
    """Separate assortments: online-only items have no warehouse to fall back to."""
    r = run(sku_in(Channel.ONLINE), other_channel_has_stock=True)

    assert r.rung is Rung.CLAIM_QUEUE
    assert "not part of the warehouse assortment" in _reason(r, Rung.OTHER_CHANNEL)


def test_the_other_channel_being_out_too_is_recorded():
    r = run(other_channel_has_stock=False)

    assert "out of stock in the warehouse assortment" in _reason(r, Rung.OTHER_CHANNEL)


def test_the_other_channel_is_relative_to_where_they_ordered():
    r = run(sku_in(Channel.ONLINE), channel=Channel.ONLINE, other_channel_has_stock=True)
    assert r.rung is Rung.CLAIM_QUEUE

    r2 = run(
        sku_in(Channel.ONLINE), channel=Channel.WAREHOUSE, other_channel_has_stock=True
    )
    assert r2.rung is Rung.OTHER_CHANNEL


# -- rung three: the claim ------------------------------------------------


def test_a_claim_is_reached_only_when_sourcing_fails():
    r = run()

    assert r.rung is Rung.CLAIM_QUEUE
    assert r.gets_the_item
    assert not r.immediate


def test_an_ineligible_item_falls_past_the_claim():
    r = run(claim_available=False, substitutes=3)

    assert r.rung is Rung.SUBSTITUTE
    assert "no confident signal" in _reason(r, Rung.CLAIM_QUEUE)


# -- rungs four and five --------------------------------------------------


def test_substitutes_are_offered_when_nothing_else_answers():
    r = run(claim_available=False, substitutes=4)

    assert r.rung is Rung.SUBSTITUTE
    assert r.substitutes == 4
    assert not r.gets_the_item
    assert "4 close alternatives" in r.member_copy


def test_a_single_substitute_reads_in_the_singular():
    assert "1 close alternative." in run(claim_available=False, substitutes=1).member_copy


def test_a_bare_refund_is_the_floor():
    r = run(claim_available=False, substitutes=0)

    assert r.rung is Rung.REFUND_ONLY
    assert not r.gets_the_item
    assert len(r.passed_over) == 4


# -- the audit trail ------------------------------------------------------


def test_the_chosen_rung_never_appears_in_the_trail():
    for kwargs in (
        {"nearby": (StockPoint("A", 5.0, 9),)},
        {"other_channel_has_stock": True},
        {},
        {"claim_available": False, "substitutes": 2},
        {"claim_available": False},
    ):
        r = run(**kwargs)
        assert r.rung not in {rej.rung for rej in r.passed_over}


def test_the_trail_follows_ladder_order():
    r = run(claim_available=False)
    order = [LADDER.index(rej.rung) for rej in r.passed_over]

    assert order == sorted(order)


def test_every_rejection_carries_a_reason():
    for rej in run(claim_available=False).passed_over:
        assert rej.reason.strip()


def test_member_copy_exists_for_every_rung():
    for rung in LADDER:
        r = Resolution(
            rung,
            source=StockPoint("A", 1.0, 9) if rung is Rung.OTHER_WAREHOUSE else None,
            substitutes=2 if rung is Rung.SUBSTITUTE else 0,
        )
        assert r.member_copy.strip()


# -- guards ---------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs", [{"distance_km": -1.0, "on_hand": 5}, {"distance_km": 5.0, "on_hand": -1}]
)
def test_impossible_stock_points_are_rejected(kwargs):
    with pytest.raises(ValueError):
        StockPoint("A", **kwargs)


def test_a_non_positive_radius_is_rejected():
    with pytest.raises(ValueError, match="positive"):
        SourcingPolicy(max_transfer_km=0)


def test_pulling_from_an_empty_shelf_is_rejected():
    with pytest.raises(ValueError, match="not sourcing"):
        SourcingPolicy(min_on_hand_to_pull=0)


def _reason(resolution, rung: Rung) -> str:
    for rejection in resolution.passed_over:
        if rejection.rung is rung:
            return rejection.reason
    raise AssertionError(f"{rung} was not passed over")
