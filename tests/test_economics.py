"""Unit economics.

The finding these tests pin down: free claim delivery does not clear its own
cost on merchandise margin, and only clears once membership renewal is counted.
If a future edit makes `merchandise_only` come out positive under the default
assumptions, the model has drifted away from the honest result and the
conclusion in the proposal no longer follows.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from holdmyplace.domain import economics as econ
from holdmyplace.domain.claims import Membership
from holdmyplace.domain.money import money, pct
from holdmyplace.domain.routing import CostParams, Mode

from .conftest import day, make_claim

DEFAULTS = econ.Assumptions()
STOP = money("4.00")


# -- provenance ------------------------------------------------------------


def test_every_assumption_declares_its_provenance():
    declared = set(econ.PROVENANCE)
    fields = {f for f in DEFAULTS.__slots__}

    assert fields == declared, "an undocumented economic input crept in"


def test_the_load_bearing_inputs_are_marked_unknown():
    for name in econ.LOAD_BEARING:
        source, _ = econ.PROVENANCE[name]
        assert source is econ.Source.UNKNOWN


def test_public_figures_are_not_dressed_up_as_measurements():
    public = {
        name for name, (src, _) in econ.PROVENANCE.items()
        if src is econ.Source.PUBLIC
    }

    assert public == {
        "merchandise_margin",
        "membership_fee_base",
        "membership_fee_executive",
    }


# -- the isolated subsidy question -----------------------------------------


def test_a_subsidized_stop_does_not_clear_on_merchandise_margin():
    """The honest headline. An $85 basket, 30% new, cannot fund a $4 stop."""
    check = econ.check_subsidy(DEFAULTS, stop_cost=STOP)

    assert not check.clears_on_merchandise
    assert check.merchandise_only < 0


def test_the_worked_example_matches_the_proposal():
    check = econ.check_subsidy(DEFAULTS, stop_cost=STOP)

    assert check.basket == money("85.00")
    assert check.incremental_revenue == money("25.50")
    assert check.gross_margin == money("2.81")
    assert check.merchandise_only == money("-1.19")


def test_counting_renewal_is_what_makes_it_clear():
    check = econ.check_subsidy(DEFAULTS, stop_cost=STOP)

    assert check.clears_with_renewal
    assert check.with_renewal > check.merchandise_only


def test_zero_renewal_lift_leaves_the_subsidy_underwater():
    """If the pilot measures no lift, the free tier should not ship."""
    flat = replace(DEFAULTS, renewal_lift_pp=pct("0.0"))

    check = econ.check_subsidy(flat, stop_cost=STOP)

    assert not check.clears_with_renewal


def test_full_incrementality_would_fund_the_stop_alone():
    """Sanity on the other direction: the model is not rigged to fail."""
    optimistic = replace(DEFAULTS, topup_incrementality=pct("1.0"))

    assert econ.check_subsidy(optimistic, stop_cost=STOP).clears_on_merchandise


def test_a_base_member_carries_less_renewal_value_than_an_executive():
    base = econ.check_subsidy(DEFAULTS, stop_cost=STOP, tier=Membership.BASE)
    exec_ = econ.check_subsidy(
        DEFAULTS, stop_cost=STOP, tier=Membership.EXECUTIVE
    )

    assert exec_.renewal_value > base.renewal_value


def test_gating_to_executive_is_what_makes_the_subsidy_defensible():
    base = econ.check_subsidy(DEFAULTS, stop_cost=STOP, tier=Membership.BASE)
    exec_ = econ.check_subsidy(
        DEFAULTS, stop_cost=STOP, tier=Membership.EXECUTIVE
    )

    assert exec_.with_renewal > base.with_renewal


# -- break-even ------------------------------------------------------------


def test_breakeven_lift_is_positive_when_merchandise_falls_short():
    lift = econ.breakeven_lift(DEFAULTS, stop_cost=STOP)

    assert lift > 0


def test_breakeven_lift_is_zero_when_merchandise_already_clears():
    optimistic = replace(DEFAULTS, topup_incrementality=pct("1.0"))

    assert econ.breakeven_lift(optimistic, stop_cost=STOP) == Decimal("0.0")


def test_a_costlier_stop_demands_more_lift():
    cheap = econ.breakeven_lift(DEFAULTS, stop_cost=money("4.00"))
    dear = econ.breakeven_lift(DEFAULTS, stop_cost=money("9.00"))

    assert dear > cheap


def test_the_assumed_lift_should_be_compared_against_breakeven():
    """The pilot has to be powered to detect this much, or not run."""
    needed = econ.breakeven_lift(DEFAULTS, stop_cost=STOP)

    assert needed < DEFAULTS.renewal_lift_pp, (
        "default assumptions claim the program clears; the pilot must be able "
        "to detect a lift this small or the claim is untestable"
    )


# -- sensitivity -----------------------------------------------------------


def test_sensitivity_covers_both_unknowns():
    grid = econ.sensitivity(DEFAULTS, stop_cost=STOP)

    assert len(grid) == 5
    assert len(grid[0][1]) == 5


def test_contribution_rises_with_both_unknowns():
    grid = dict(econ.sensitivity(DEFAULTS, stop_cost=STOP))
    low_lift = dict(grid[0.0])
    high_lift = dict(grid[2.0])

    assert high_lift[0.3] > low_lift[0.3]
    assert low_lift[0.8] > low_lift[0.1]


def test_the_grid_contains_a_sign_boundary():
    """If every cell shared a sign the grid would tell a reader nothing."""
    values = [
        value
        for _, row in econ.sensitivity(DEFAULTS, stop_cost=STOP)
        for _, value in row
    ]

    assert any(v < 0 for v in values)
    assert any(v > 0 for v in values)


# -- per-claim scoring -----------------------------------------------------


def decision_for(mode: Mode, *, fee="0.00", cost="4.00"):
    from holdmyplace.domain.routing import Decision

    return Decision(
        claim_id="A",
        mode=mode,
        arrival=day(2),
        fee_charged=money(fee),
        cost_estimate=money(cost),
    )


def test_item_margin_is_net_of_members_who_would_have_rebought():
    full = replace(DEFAULTS, repurchase_rate=pct("0.0"))
    half = replace(DEFAULTS, repurchase_rate=pct("0.5"))

    assert econ.item_margin(money("100.00"), half) < econ.item_margin(
        money("100.00"), full
    )


def test_pickup_earns_no_topup_margin():
    """Nothing goes on a vehicle, so no free-delivery threshold is triggered."""
    contribution = econ.evaluate(
        decision_for(Mode.PICKUP_HOLD, cost="0.00"),
        price=money("20.00"),
        tier=Membership.BASE,
        a=DEFAULTS,
    )

    assert contribution.topup_margin == money("0.00")
    assert contribution.stop_cost == money("0.00")


def test_pickup_is_contribution_positive_without_any_assumption():
    """The mode that needs no business case, asserted as needing none."""
    flat = replace(DEFAULTS, renewal_lift_pp=pct("0.0"))
    contribution = econ.evaluate(
        decision_for(Mode.PICKUP_HOLD, cost="0.00"),
        price=money("20.00"),
        tier=Membership.BASE,
        a=flat,
    )

    assert contribution.total > 0


def test_paid_express_collects_a_fee_that_covers_its_stop():
    costs = CostParams()
    contribution = econ.evaluate(
        decision_for(
            Mode.PAID_EXPRESS, fee=str(costs.express_fee), cost=str(costs.adjacent_stop)
        ),
        price=money("20.00"),
        tier=Membership.BASE,
        a=replace(DEFAULTS, renewal_lift_pp=pct("0.0")),
    )

    assert contribution.fee_revenue >= contribution.stop_cost
    assert contribution.total > 0


def test_an_actual_stop_cost_overrides_the_estimate():
    contribution = econ.evaluate(
        decision_for(Mode.BATCHED_ROUTE, cost="4.00"),
        price=money("20.00"),
        tier=Membership.BASE,
        a=DEFAULTS,
        actual_stop_cost=money("2.10"),
    )

    assert contribution.stop_cost == money("2.10")


def test_contributions_sum_component_wise():
    one = econ.evaluate(
        decision_for(Mode.PAID_EXPRESS, fee="4.99"),
        price=money("20.00"),
        tier=Membership.BASE,
        a=DEFAULTS,
    )

    total = one + one

    assert total.fee_revenue == money(one.fee_revenue * 2)
    assert total.total == money(one.total * 2)


def test_total_excluding_renewal_is_reported_separately():
    contribution = econ.evaluate(
        decision_for(Mode.EXECUTIVE_FREE),
        price=money("20.00"),
        tier=Membership.EXECUTIVE,
        a=DEFAULTS,
    )

    assert contribution.total != contribution.total_excl_renewal
    assert contribution.total_excl_renewal < contribution.total


def test_renewal_value_scales_with_the_assumed_lift():
    doubled = replace(DEFAULTS, renewal_lift_pp=pct("1.0"))

    assert econ.renewal_value(Membership.BASE, doubled) == money(
        econ.renewal_value(Membership.BASE, DEFAULTS) * 2
    )


def test_zero_membership_value_is_rejected_rather_than_divided_by():
    broken = replace(DEFAULTS, remaining_tenure_years=pct("0"))

    with pytest.raises(ValueError, match="cannot be zero"):
        econ.breakeven_lift(broken, stop_cost=STOP)
