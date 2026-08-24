"""Eligibility. The rule under test throughout: fail closed."""

from __future__ import annotations

import pytest

from holdmyplace.domain.catalog import (
    CLAIMABLE,
    DENIAL_COPY,
    Denial,
    Lifecycle,
    assess,
    stale_flag_candidates,
)

from .conftest import day, make_sku


@pytest.mark.parametrize(
    "lifecycle",
    [Lifecycle.CORE, Lifecycle.TEMPORARILY_UNAVAILABLE],
)
def test_replenished_lifecycles_are_claimable(lifecycle, today):
    assert assess(make_sku(lifecycle=lifecycle), today).eligible


@pytest.mark.parametrize(
    "lifecycle,expected",
    [
        (Lifecycle.OPPORTUNISTIC, Denial.ONE_TIME_BUY),
        (Lifecycle.DISCONTINUED, Denial.DISCONTINUED),
    ],
)
def test_one_way_lifecycles_are_refused_with_a_reason(lifecycle, expected, today):
    verdict = assess(make_sku(lifecycle=lifecycle, cadence=None), today)

    assert not verdict.eligible
    assert verdict.denial is expected


def test_claimable_set_excludes_everything_that_does_not_return():
    assert Lifecycle.OPPORTUNISTIC not in CLAIMABLE
    assert Lifecycle.DISCONTINUED not in CLAIMABLE


def test_missing_cadence_fails_closed_even_on_a_claimable_lifecycle(today):
    """The item master says active; receipt history says nothing. Refuse."""
    verdict = assess(make_sku(lifecycle=Lifecycle.CORE, cadence=None), today)

    assert not verdict.eligible
    assert verdict.denial is Denial.NO_RESTOCK_SIGNAL


def test_seasonal_inside_its_window_is_capped_at_the_season_end(today):
    season_end = day(45)
    verdict = assess(
        make_sku(lifecycle=Lifecycle.SEASONAL, season_end=season_end), today
    )

    assert verdict.eligible
    assert verdict.latest_cancel_by == season_end


def test_seasonal_after_its_window_is_refused(today):
    verdict = assess(
        make_sku(lifecycle=Lifecycle.SEASONAL, season_end=day(-1)), today
    )

    assert not verdict.eligible
    assert verdict.denial is Denial.SEASON_CLOSED


def test_season_end_boundary_is_exclusive(today):
    """On the season-end day itself the window is already shut."""
    closed = assess(make_sku(lifecycle=Lifecycle.SEASONAL, season_end=today), today)
    open_ = assess(make_sku(lifecycle=Lifecycle.SEASONAL, season_end=day(1)), today)

    assert not closed.eligible
    assert open_.eligible


def test_seasonal_without_a_season_end_fails_closed(today):
    verdict = assess(
        make_sku(lifecycle=Lifecycle.SEASONAL, season_end=None), today
    )

    assert not verdict.eligible
    assert verdict.denial is Denial.NO_RESTOCK_SIGNAL


def test_every_denial_has_member_facing_copy():
    for denial in Denial:
        assert DENIAL_COPY[denial].strip()


def test_eligible_verdict_has_no_member_copy(today):
    assert assess(make_sku(), today).member_copy is None


def test_refused_verdict_explains_itself(today):
    verdict = assess(make_sku(lifecycle=Lifecycle.DISCONTINUED, cadence=None), today)
    assert "Refunded" in verdict.member_copy


def test_ever_returns_requires_both_a_lifecycle_and_a_cadence():
    assert make_sku(lifecycle=Lifecycle.CORE, cadence=14).ever_returns
    assert not make_sku(lifecycle=Lifecycle.CORE, cadence=None).ever_returns
    assert not make_sku(lifecycle=Lifecycle.DISCONTINUED, cadence=14).ever_returns


def test_non_positive_cadence_is_rejected_at_construction():
    with pytest.raises(ValueError, match="cadence"):
        make_sku(cadence=0)


# -- flag hygiene ----------------------------------------------------------


def test_stale_flags_surface_claimable_skus_with_no_recent_receipts(today):
    catalog = {
        "A": make_sku("A", lifecycle=Lifecycle.CORE),
        "B": make_sku("B", lifecycle=Lifecycle.CORE),
        "C": make_sku("C", lifecycle=Lifecycle.DISCONTINUED, cadence=None),
    }
    last_received = {"A": day(-10), "B": day(-400), "C": day(-400)}

    stale = stale_flag_candidates(catalog, last_received, today, silence_days=120)

    assert stale == ["B"], "only claimable SKUs are worth flagging to a buyer"


def test_a_sku_with_no_receipt_history_at_all_is_flagged(today):
    catalog = {"A": make_sku("A")}

    assert stale_flag_candidates(catalog, {}, today) == ["A"]
