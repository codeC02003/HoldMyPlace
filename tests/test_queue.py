"""The allocation invariant, which is where the design's fairness lives.

    Order time decides who gets a unit. The deadline decides only whether
    they are still in line.

The first two tests below establish ordering. The third is the one that matters
most: it is the mechanism-design property that lets members self-report urgency
without any verification, because declaring a tighter deadline can only ever
cost them opportunities.
"""

from __future__ import annotations

import pytest

from holdmyplace.domain.queue import ClaimQueue, SkipReason, demand_signal
from holdmyplace.domain.money import money

from .conftest import day, make_claim


def build(*claims) -> ClaimQueue:
    queue = ClaimQueue()
    for claim in claims:
        queue.add(claim)
    return queue


# -- ordering ---------------------------------------------------------------


def test_allocation_follows_order_time_not_filing_time():
    early_order = make_claim("A", ordered_offset=0, created_offset=5, cancel_offset=60)
    late_order = make_claim("B", ordered_offset=3, created_offset=3, cancel_offset=60)
    queue = build(late_order, early_order)

    plan = queue.plan("1500001", units=1, arrival=day(10))

    assert plan.fill == ("A",)


def test_a_nearer_deadline_does_not_jump_the_queue():
    patient = make_claim("A", ordered_offset=0, cancel_offset=90)
    urgent = make_claim("B", ordered_offset=1, cancel_offset=8)
    queue = build(patient, urgent)

    plan = queue.plan("1500001", units=1, arrival=day(5))

    # Both are reachable by day 5. FIFO decides, so the urgent claim waits.
    assert plan.fill == ("A",)


def test_ties_broken_deterministically_by_claim_id():
    first = make_claim("A", ordered_offset=2, cancel_offset=60)
    second = make_claim("B", ordered_offset=2, cancel_offset=60)
    queue = build(second, first)

    assert queue.plan("1500001", 1, day(10)).fill == ("A",)


# -- the deadline as a filter ----------------------------------------------


def test_unreachable_claim_is_skipped_without_consuming_a_unit():
    unreachable = make_claim("A", ordered_offset=0, cancel_offset=3)
    reachable = make_claim("B", ordered_offset=1, cancel_offset=60)
    queue = build(unreachable, reachable)

    plan = queue.plan("1500001", units=1, arrival=day(10))

    assert plan.fill == ("B",)
    assert [(s.claim_id, s.reason) for s in plan.skip] == [
        ("A", SkipReason.DEADLINE_UNREACHABLE)
    ]
    assert plan.units_unused == 0, "being passed over must not waste the unit"


def test_skip_is_recorded_on_the_claim_but_leaves_it_open():
    unreachable = make_claim("A", ordered_offset=0, cancel_offset=3)
    queue = build(unreachable)

    plan = queue.plan("1500001", units=1, arrival=day(10))
    queue.commit(plan, day(10))

    assert unreachable.is_open
    assert unreachable.times_skipped == 1


def test_arrival_may_vary_by_claim():
    collector = make_claim("A", ordered_offset=0, cancel_offset=1, prefers_pickup=True)
    deliveree = make_claim("B", ordered_offset=0, cancel_offset=1)
    queue = build(collector, deliveree)

    # Available today: pickup is same-day, delivery takes two. Only the member
    # collecting at the warehouse can be reached before tomorrow's deadline.
    plan = queue.plan(
        "1500001",
        units=2,
        arrival=lambda c: day(0) if c.prefers_pickup else day(2),
    )

    assert plan.fill == ("A",)
    assert [s.claim_id for s in plan.skip] == ["B"]


# -- the anti-gaming property ----------------------------------------------


@pytest.mark.parametrize("arrival_offset", range(1, 25, 3))
@pytest.mark.parametrize("loose,tight", [(60, 30), (30, 10), (45, 7), (20, 19)])
def test_tightening_a_deadline_never_gains_a_unit(arrival_offset, loose, tight):
    """The property that makes self-reported urgency safe to trust.

    A member sits behind two earlier claims. Whatever arrival date a receipt
    implies, shortening their own cancel-by date can never turn a miss into a
    hit — so overstating urgency is strictly self-harming, and needs no
    verification, no policing, and no abuse team.
    """
    arrival = day(arrival_offset)

    def outcome(deadline_offset: int) -> bool:
        gamer = make_claim("C-gamer", ordered_offset=5, cancel_offset=deadline_offset)
        queue = build(
            make_claim("A", ordered_offset=1, cancel_offset=90),
            make_claim("B", ordered_offset=3, cancel_offset=90),
            gamer,
        )
        return "C-gamer" in queue.plan("1500001", units=2, arrival=arrival).fill

    if outcome(tight):
        assert outcome(loose), (
            "a tighter deadline produced a fill that a looser one did not — "
            "the deadline is acting as a priority, not a filter"
        )


def test_tightening_a_deadline_cannot_displace_the_claim_behind_it():
    """Dropping out may help the next member. It must never hurt them."""

    def filled(front_deadline: int) -> tuple[str, ...]:
        queue = build(
            make_claim("A", ordered_offset=0, cancel_offset=front_deadline),
            make_claim("B", ordered_offset=1, cancel_offset=90),
        )
        return queue.plan("1500001", units=1, arrival=day(20)).fill

    assert filled(90) == ("A",)
    # A tightens out of reach; the unit passes to B rather than being lost.
    assert filled(5) == ("B",)


def test_extending_a_deadline_does_not_change_position():
    ahead = make_claim("A", ordered_offset=0, cancel_offset=20)
    behind = make_claim("B", ordered_offset=1, cancel_offset=20)
    queue = build(ahead, behind)

    behind.extend(day(120), day(2))

    assert queue.plan("1500001", 1, day(10)).fill == ("A",)


# -- capacity and bookkeeping ---------------------------------------------


def test_units_beyond_the_queue_are_reported_unused():
    queue = build(make_claim("A", ordered_offset=0, cancel_offset=60))

    plan = queue.plan("1500001", units=5, arrival=day(10))

    assert plan.fill == ("A",)
    assert plan.units_unused == 4


def test_zero_units_fills_nothing_and_skips_nothing():
    queue = build(make_claim("A", ordered_offset=0, cancel_offset=2))

    plan = queue.plan("1500001", units=0, arrival=day(30))

    assert plan.fill == ()
    assert plan.skip == ()


def test_negative_units_rejected():
    queue = build(make_claim("A", ordered_offset=0, cancel_offset=60))
    with pytest.raises(ValueError):
        queue.plan("1500001", units=-1, arrival=day(1))


def test_commit_fills_claims_and_closes_them():
    claim = make_claim("A", ordered_offset=0, cancel_offset=60)
    queue = build(claim)

    filled = queue.commit(queue.plan("1500001", 1, day(10)), day(10))

    assert [c.claim_id for c in filled] == ["A"]
    assert not claim.is_open
    assert claim.filled_on == day(10)


def test_filled_claims_leave_the_open_set():
    queue = build(
        make_claim("A", ordered_offset=0, cancel_offset=60),
        make_claim("B", ordered_offset=1, cancel_offset=60),
    )
    queue.commit(queue.plan("1500001", 1, day(5)), day(5))

    assert [c.claim_id for c in queue.open_for("1500001")] == ["B"]
    assert queue.open_count("1500001") == 1


def test_duplicate_claim_id_rejected():
    queue = build(make_claim("A", ordered_offset=0, cancel_offset=60))
    with pytest.raises(ValueError, match="duplicate"):
        queue.add(make_claim("A", ordered_offset=1, cancel_offset=60))


def test_position_is_one_indexed_and_reflects_order_time():
    queue = build(
        make_claim("B", ordered_offset=4, cancel_offset=60),
        make_claim("A", ordered_offset=1, cancel_offset=60),
        make_claim("C", ordered_offset=9, cancel_offset=60),
    )

    assert queue.position_of("A") == 1
    assert queue.position_of("B") == 2
    assert queue.position_of("C") == 3


def test_position_of_a_closed_claim_is_an_error():
    claim = make_claim("A", ordered_offset=0, cancel_offset=60)
    queue = build(claim)
    claim.expire(day(61))

    with pytest.raises(ValueError, match="not queued"):
        queue.position_of("A")


# -- expiry ----------------------------------------------------------------


def test_expiry_closes_only_lapsed_claims():
    lapsed = make_claim("A", ordered_offset=0, cancel_offset=10)
    live = make_claim("B", ordered_offset=0, cancel_offset=40)
    queue = build(lapsed, live)

    closed = queue.expire_lapsed(day(11))

    assert [c.claim_id for c in closed] == ["A"]
    assert live.is_open


def test_expiry_is_idempotent():
    queue = build(make_claim("A", ordered_offset=0, cancel_offset=10))

    assert len(queue.expire_lapsed(day(20))) == 1
    assert queue.expire_lapsed(day(21)) == []


def test_claim_survives_its_deadline_day_itself():
    queue = build(make_claim("A", ordered_offset=0, cancel_offset=10))

    assert queue.expire_lapsed(day(10)) == []
    assert queue.expire_lapsed(day(11)) != []


def test_nudge_fires_once_inside_the_lead_window():
    claim = make_claim("A", ordered_offset=0, cancel_offset=10)
    queue = build(claim)

    assert queue.due_for_nudge(day(6)) == []
    assert [c.claim_id for c in queue.due_for_nudge(day(8))] == ["A"]

    claim.mark_nudged()
    assert queue.due_for_nudge(day(9)) == []


def test_purge_drops_closed_claims_from_the_sku_index():
    claim = make_claim("A", ordered_offset=0, cancel_offset=10)
    queue = build(claim, make_claim("B", ordered_offset=1, cancel_offset=60))
    claim.expire(day(11))

    assert queue.purge_closed() == 1
    assert queue.open_count("1500001") == 1


# -- demand signal ---------------------------------------------------------


def test_demand_signal_groups_by_sku_and_area():
    queue = build(
        make_claim("A", ordered_offset=0, cancel_offset=10, zip_code="85719"),
        make_claim("B", ordered_offset=0, cancel_offset=12, zip_code="85719"),
        make_claim("C", ordered_offset=0, cancel_offset=10, zip_code="85704"),
    )

    clusters = demand_signal(queue, day(1), horizon_days=21, zip_precision=5)

    assert [(c.zip_code, c.open_claims) for c in clusters] == [
        ("85719", 2),
        ("85704", 1),
    ]


def test_forfeit_value_counts_only_claims_expiring_inside_the_horizon():
    queue = build(
        make_claim("A", ordered_offset=0, cancel_offset=5, price="20.00"),
        make_claim("B", ordered_offset=0, cancel_offset=120, price="99.00"),
    )

    cluster = demand_signal(queue, day(1), horizon_days=21, zip_precision=5)[0]

    assert cluster.open_claims == 2
    assert cluster.expiring_soon == 1
    assert cluster.forfeit_value == money("20.00")


def test_demand_signal_ignores_closed_claims():
    filled = make_claim("A", ordered_offset=0, cancel_offset=10)
    queue = build(filled, make_claim("B", ordered_offset=0, cancel_offset=10))
    filled.fill(day(2))

    clusters = demand_signal(queue, day(3), zip_precision=5)

    assert sum(c.open_claims for c in clusters) == 1


def test_worst_clusters_sort_first():
    claims = [
        make_claim(f"X{i}", ordered_offset=0, cancel_offset=6, zip_code="85704")
        for i in range(3)
    ]
    claims.append(
        make_claim("Y0", ordered_offset=0, cancel_offset=6, zip_code="85719")
    )
    queue = build(*claims)

    clusters = demand_signal(queue, day(1), zip_precision=5)

    assert clusters[0].zip_code == "85704"
