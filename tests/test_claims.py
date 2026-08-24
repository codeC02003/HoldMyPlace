"""Claim mechanics: deadlines, the price lock, and state transitions."""

from __future__ import annotations

import pytest

from holdmyplace.domain.claims import (
    DEADLINE_PROMPT,
    MAX_CLAIM_DAYS,
    PRICE_LOCK_DAYS,
    Claim,
    ClaimStatus,
    DeadlinePreset,
    Membership,
    resolve_deadline,
)
from holdmyplace.domain.money import money

from .conftest import day, make_claim


# -- deadlines -------------------------------------------------------------


@pytest.mark.parametrize(
    "preset,expected_days",
    [
        (DeadlinePreset.TWO_WEEKS, 14),
        (DeadlinePreset.ONE_MONTH, 30),
        (DeadlinePreset.THREE_MONTHS, 90),
        (DeadlinePreset.UNTIL_CANCELLED, MAX_CLAIM_DAYS),
    ],
)
def test_presets_resolve_to_their_intervals(preset, expected_days, today):
    assert resolve_deadline(preset, today) == day(expected_days)


def test_until_cancelled_is_still_bounded():
    """No claim runs forever. An unbounded wait is an unbounded liability."""
    assert PRICE_LOCK_DAYS < MAX_CLAIM_DAYS < 400


def test_an_eligibility_ceiling_overrides_the_member_choice(today):
    ceiling = day(20)

    chosen = resolve_deadline(
        DeadlinePreset.THREE_MONTHS, today, ceiling=ceiling
    )

    assert chosen == ceiling


def test_an_exact_date_is_capped_at_the_global_maximum(today):
    chosen = resolve_deadline(
        DeadlinePreset.EXACT_DATE, today, exact=day(900)
    )

    assert chosen == day(MAX_CLAIM_DAYS)


def test_an_exact_date_in_the_past_is_rejected(today):
    with pytest.raises(ValueError, match="future"):
        resolve_deadline(DeadlinePreset.EXACT_DATE, today, exact=day(-1))


def test_exact_date_requires_a_date(today):
    with pytest.raises(ValueError, match="EXACT_DATE"):
        resolve_deadline(DeadlinePreset.EXACT_DATE, today)


def test_the_prompt_is_phrased_as_cancel_by_not_deliver_by():
    """Copy discipline, asserted. The framing is the feature."""
    lowered = DEADLINE_PROMPT.lower()

    assert "cancel" in lowered
    assert "deliver" not in lowered


# -- construction ----------------------------------------------------------


def test_a_deadline_at_or_before_creation_is_rejected():
    with pytest.raises(ValueError, match="cancel-by"):
        make_claim("A", ordered_offset=0, cancel_offset=0)


def test_a_claim_cannot_predate_its_own_order():
    with pytest.raises(ValueError, match="precedes"):
        Claim(
            claim_id="A",
            member_id="M",
            sku_id="S",
            zip_code="85719",
            locked_price=money("10.00"),
            ordered_at=day(5),
            created_at=day(2),
            cancel_by=day(40),
        )


def test_price_is_quantized_on_construction():
    claim = make_claim("A", price="10.005")
    assert claim.locked_price == money("10.01")


# -- slack and reachability ------------------------------------------------


def test_slack_counts_days_to_the_deadline():
    claim = make_claim("A", ordered_offset=0, cancel_offset=30)
    assert claim.slack_days(day(10)) == 20


def test_arrival_on_the_deadline_day_still_satisfies_the_claim():
    claim = make_claim("A", ordered_offset=0, cancel_offset=30)

    assert claim.deliverable_by(day(30))
    assert not claim.deliverable_by(day(31))


def test_lapse_begins_the_day_after_the_deadline():
    claim = make_claim("A", ordered_offset=0, cancel_offset=30)

    assert not claim.has_lapsed(day(30))
    assert claim.has_lapsed(day(31))


# -- the price lock --------------------------------------------------------


def test_inside_the_lock_the_original_price_holds():
    claim = make_claim("A", ordered_offset=0, cancel_offset=120, price="18.99")

    assert claim.price_on(day(PRICE_LOCK_DAYS), money("25.99")) == money("18.99")


def test_past_the_lock_the_current_price_applies():
    claim = make_claim("A", ordered_offset=0, cancel_offset=120, price="18.99")

    assert claim.price_on(day(PRICE_LOCK_DAYS + 1), money("25.99")) == money("25.99")


def test_a_long_claim_does_not_extend_the_price_lock():
    """A six-month claim must not imply a six-month locked price."""
    claim = make_claim("A", ordered_offset=0, cancel_offset=MAX_CLAIM_DAYS)

    assert claim.lock_expires_on() == day(PRICE_LOCK_DAYS)


def test_material_increases_past_the_lock_require_reconsent():
    claim = make_claim("A", ordered_offset=0, cancel_offset=120, price="20.00")
    later = day(PRICE_LOCK_DAYS + 1)

    assert claim.requires_reconsent(later, money("22.00"))
    assert not claim.requires_reconsent(later, money("20.50"))


def test_no_reconsent_needed_inside_the_lock():
    claim = make_claim("A", ordered_offset=0, cancel_offset=120, price="20.00")

    assert not claim.requires_reconsent(day(5), money("99.00"))


def test_a_price_drop_never_requires_reconsent():
    claim = make_claim("A", ordered_offset=0, cancel_offset=120, price="20.00")

    assert not claim.requires_reconsent(day(PRICE_LOCK_DAYS + 1), money("12.00"))


# -- extensions ------------------------------------------------------------


def test_extending_moves_the_deadline_and_rearms_the_nudge():
    claim = make_claim("A", ordered_offset=0, cancel_offset=10)
    claim.mark_nudged()

    claim.extend(day(40), day(8))

    assert claim.cancel_by == day(40)
    assert claim.extensions == 1
    assert claim.due_for_nudge(day(38))


def test_an_extension_is_capped_at_the_global_maximum():
    claim = make_claim("A", ordered_offset=0, cancel_offset=10)

    claim.extend(day(900), day(5))

    assert claim.cancel_by == day(MAX_CLAIM_DAYS)


def test_an_extension_must_move_the_deadline_later():
    claim = make_claim("A", ordered_offset=0, cancel_offset=30)

    with pytest.raises(ValueError, match="later"):
        claim.extend(day(20), day(5))


def test_a_closed_claim_cannot_be_extended():
    claim = make_claim("A", ordered_offset=0, cancel_offset=30)
    claim.fill(day(5))

    with pytest.raises(ValueError, match="cannot extend"):
        claim.extend(day(60), day(6))


def test_extension_does_not_touch_the_fifo_key():
    claim = make_claim("A", ordered_offset=3, cancel_offset=30)
    before = claim.fifo_key

    claim.extend(day(90), day(5))

    assert claim.fifo_key == before


# -- transitions -----------------------------------------------------------


def test_fill_closes_the_claim_and_stamps_the_date():
    claim = make_claim("A", ordered_offset=0, cancel_offset=30)

    claim.fill(day(9))

    assert claim.status is ClaimStatus.FILLED
    assert claim.filled_on == day(9)
    assert not claim.is_open


@pytest.mark.parametrize("transition", ["fill", "expire", "cancel"])
def test_a_closed_claim_rejects_further_transitions(transition):
    claim = make_claim("A", ordered_offset=0, cancel_offset=30)
    claim.cancel(day(4))

    with pytest.raises(ValueError, match="already"):
        getattr(claim, transition)(day(5))


def test_skipping_a_closed_claim_is_an_error():
    claim = make_claim("A", ordered_offset=0, cancel_offset=30)
    claim.fill(day(4))

    with pytest.raises(ValueError):
        claim.skip()


def test_membership_defaults_to_base():
    assert make_claim("A").membership is Membership.BASE
