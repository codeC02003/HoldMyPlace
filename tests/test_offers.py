"""What the member is shown.

The behaviour these tests protect is the refund coming first, unconditionally,
on every path — and deadlines being refused when they are set, not silently
accepted and failed weeks later.
"""

from __future__ import annotations

from holdmyplace.domain.catalog import Denial, Lifecycle
from holdmyplace.domain.claims import DeadlinePreset
from holdmyplace.domain.money import money
from holdmyplace.domain.offers import (
    Feasibility,
    assess_deadline,
    build_offer,
    deadline_warning,
)

from .conftest import day, make_sku

import pytest


def offer_for(sku, today, *, total="18.99", last=None):
    return build_offer(sku, today, line_total=money(total), last_received=last)


# -- the refund comes first ------------------------------------------------


@pytest.mark.parametrize(
    "lifecycle,cadence",
    [
        (Lifecycle.CORE, 14),
        (Lifecycle.OPPORTUNISTIC, None),
        (Lifecycle.DISCONTINUED, None),
    ],
)
def test_every_path_refunds_the_full_line(lifecycle, cadence, today):
    sku = make_sku(lifecycle=lifecycle, cadence=cadence)

    offer = offer_for(sku, today, total="42.50", last=day(-2))

    assert offer.refund_amount == money("42.50")
    assert "refunded" in offer.headline.lower()


def test_the_headline_leads_with_the_refund_not_the_queue(today):
    offer = offer_for(make_sku(), today, last=day(-2))

    assert "refunded" in offer.headline.lower()
    assert "hold your place" not in offer.headline.lower()


def test_the_claim_is_the_secondary_step(today):
    offer = offer_for(make_sku(), today, last=day(-2))

    assert offer.claimable
    assert "hold your place" in offer.secondary.lower()


# -- eligibility flows through ---------------------------------------------


def test_a_one_time_buy_is_refunded_with_substitutions(today):
    sku = make_sku(lifecycle=Lifecycle.OPPORTUNISTIC, cadence=None)

    offer = offer_for(sku, today)

    assert not offer.claimable
    assert offer.denial is Denial.ONE_TIME_BUY
    assert "similar items" in offer.secondary


def test_a_low_confidence_estimate_fails_closed(today):
    """Eligible lifecycle, unusable timing signal. No claim option appears."""
    sku = make_sku(
        lifecycle=Lifecycle.SEASONAL, cadence=28, season_end=day(5)
    )

    offer = offer_for(sku, today, last=day(-1))

    assert not offer.claimable
    assert offer.denial is Denial.NO_RESTOCK_SIGNAL


def test_a_seasonal_offer_carries_the_season_ceiling(today):
    sku = make_sku(lifecycle=Lifecycle.SEASONAL, cadence=14, season_end=day(60))

    offer = offer_for(sku, today, last=day(-1))

    assert offer.claimable
    assert offer.latest_cancel_by == day(60)


def test_no_presets_are_offered_when_no_claim_is(today):
    sku = make_sku(lifecycle=Lifecycle.DISCONTINUED, cadence=None)

    assert offer_for(sku, today).presets(today) == []


# -- presets are filtered to what could actually arrive --------------------


def test_presets_shorter_than_the_estimate_are_withheld(today):
    sku = make_sku(cadence=45)

    offer = offer_for(sku, today, last=day(-1))
    presets = offer.presets(today)

    assert DeadlinePreset.TWO_WEEKS not in presets
    assert DeadlinePreset.THREE_MONTHS in presets


def test_a_fast_moving_item_offers_every_preset(today):
    offer = offer_for(make_sku(cadence=7), today, last=day(-1))

    presets = offer.presets(today)

    assert DeadlinePreset.TWO_WEEKS in presets
    assert DeadlinePreset.EXACT_DATE in presets


def test_a_season_ceiling_narrows_the_presets(today):
    sku = make_sku(lifecycle=Lifecycle.SEASONAL, cadence=14, season_end=day(20))

    presets = offer_for(sku, today, last=day(-1)).presets(today)

    assert DeadlinePreset.THREE_MONTHS not in presets


# -- deadline feasibility --------------------------------------------------


def test_a_comfortable_date_is_likely(today):
    offer = offer_for(make_sku(cadence=14), today, last=day(-1))

    assert assess_deadline(offer, day(90)) is Feasibility.LIKELY


def test_a_date_inside_the_band_is_unlikely(today):
    offer = offer_for(make_sku(cadence=20), today, last=today)
    # eta is day 20, band pushes worst case later
    verdict = assess_deadline(offer, day(21))

    assert verdict is Feasibility.UNLIKELY


def test_a_date_before_the_estimate_is_impossible(today):
    offer = offer_for(make_sku(cadence=30), today, last=today)

    assert assess_deadline(offer, day(5)) is Feasibility.IMPOSSIBLE


def test_assessing_a_deadline_on_a_refused_offer_is_an_error(today):
    sku = make_sku(lifecycle=Lifecycle.DISCONTINUED, cadence=None)
    offer = offer_for(sku, today)

    with pytest.raises(ValueError, match="non-claimable"):
        assess_deadline(offer, day(30))


# -- the warning copy ------------------------------------------------------


def test_a_comfortable_date_produces_no_warning(today):
    offer = offer_for(make_sku(cadence=10), today, last=day(-1))

    assert deadline_warning(offer, day(90), Feasibility.LIKELY) is None


def test_an_impossible_date_is_named_and_three_ways_out_are_offered(today):
    offer = offer_for(make_sku(cadence=30), today, last=today)
    warning = deadline_warning(offer, day(5), Feasibility.IMPOSSIBLE)

    assert "won't make your date" in warning
    assert "Hold my place anyway" in warning
    assert "Move the date out" in warning
    assert "Just refund me" in warning


def test_a_tight_date_is_flagged_as_tight_not_impossible(today):
    offer = offer_for(make_sku(cadence=20), today, last=today)
    warning = deadline_warning(offer, day(21), Feasibility.UNLIKELY)

    assert "tight" in warning
