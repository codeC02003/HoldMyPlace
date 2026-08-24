"""Build the demo's data by running the real domain logic.

The whole point of generating this rather than hand-writing it: every branch the
demo can show is evaluated by the same code the simulation runs, so a change to
eligibility, feasibility, routing, or the copy shows up in the screens without
anyone remembering to update them.

Four variants are exported, one per rung of the resolution ladder, because the
paths that do *not* end in a claim are most of the design:

  nearby     stock 14 km away — the item is pulled over, no refund, no wait
  core       nothing in range — a claim, end to end
  one_time   an opportunistic buy — refunded, substitutes shown
  seasonal   a closed season with no alternatives — a bare refund

For the claim variant, every deadline preset the offer permits is evaluated
all the way through to a fulfillment decision, including the presets that turn
out to be infeasible. Those are the interesting ones: they show the system
refusing a date at the moment it is set rather than failing it weeks later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from ..domain import economics as econ
from ..domain.catalog import Channel, Lifecycle, Sku
from ..domain.claims import (
    PRESET_DAYS,
    Claim,
    DeadlinePreset,
    Membership,
    resolve_deadline,
)
from ..domain.money import fmt, money
from ..domain.offers import (
    Feasibility,
    assess_deadline,
    build_offer,
    deadline_warning,
    resolve_line,
)
from ..domain.queue import ClaimQueue
from ..domain.sourcing import LADDER, Rung, SourcingPolicy, StockPoint
from ..domain.restock import RestockPolicy, estimate_return, split_receipt
from ..domain.routing import BatchParams, CostParams, Mode, choose_mode
from ..sim.generate import WorldConfig, build_world
from ..sim.run import simulate

TODAY = date(2026, 10, 31)
"""Fixed 'now' for the demo. Nothing reads the wall clock.

Set two months into the generated world so that some seasonal windows have
already closed — the season-closed refusal is one of the paths worth showing,
and it does not exist on day one.
"""

COSTS = CostParams()
BATCHING = BatchParams()
POLICY = RestockPolicy()
SOURCING = SourcingPolicy()
ASSUMPTIONS = econ.Assumptions()

PRESET_LABELS = {
    DeadlinePreset.TWO_WEEKS: "2 weeks",
    DeadlinePreset.ONE_MONTH: "1 month",
    DeadlinePreset.THREE_MONTHS: "3 months",
    DeadlinePreset.UNTIL_CANCELLED: "Until I cancel",
    DeadlinePreset.EXACT_DATE: "Pick a date",
}

MODE_LABELS = {
    Mode.PICKUP_HOLD: "Held at the warehouse",
    Mode.BATCHED_ROUTE: "Delivered on a nearby route",
    Mode.PAID_EXPRESS: "Express delivery",
    Mode.EXECUTIVE_FREE: "Express delivery, free",
}

MODE_MEMBER_COPY = {
    Mode.PICKUP_HOLD: "Ready at the front desk. We'll hold it five days.",
    Mode.BATCHED_ROUTE: "Going out with other deliveries in your area.",
    Mode.PAID_EXPRESS: "On a van already heading your way.",
    Mode.EXECUTIVE_FREE: "On a van already heading your way — free, as an Executive member.",
}

RUNG_LABELS = {
    Rung.OTHER_WAREHOUSE: "Pulled from another warehouse",
    Rung.OTHER_CHANNEL: "Filled from the other channel",
    Rung.CLAIM_QUEUE: "Place held in the queue",
    Rung.SUBSTITUTE: "Refunded, substitutes shown",
    Rung.REFUND_ONLY: "Refunded, nothing else to do",
}

#: Sourcing context per demo variant. Each is chosen to land the ladder on a
#: different rung, so the walk is visible rather than described.
LADDER_SPECS: dict[str, dict] = {
    "nearby": {
        "nearby": (StockPoint("Tucson East", 14.0, 9),),
        "other_channel_has_stock": False,
        "substitutes": 3,
    },
    "core": {
        "nearby": (StockPoint("Phoenix S", 165.0, 22),),
        "other_channel_has_stock": False,
        "substitutes": 3,
    },
    "one_time": {
        "nearby": (StockPoint("Tucson NW", 26.0, 1),),
        "other_channel_has_stock": False,
        "substitutes": 4,
    },
    "seasonal": {
        "nearby": (),
        "other_channel_has_stock": False,
        "substitutes": 0,
    },
}

FEASIBILITY_LABELS = {
    Feasibility.LIKELY: "Comfortable",
    Feasibility.UNLIKELY: "Tight",
    Feasibility.IMPOSSIBLE: "Won't make it",
}


def _d(value: date) -> str:
    return f"{value:%b %-d}"


def _iso(value: date) -> str:
    return value.isoformat()


# ---------------------------------------------------------------------------
# Picking real items out of a generated catalog
# ---------------------------------------------------------------------------


def _pick_skus(catalog: dict[str, Sku]) -> dict[str, Sku]:
    """Find one SKU per variant, so names and prices are not invented here."""

    def first(predicate) -> Sku:
        for sku in catalog.values():
            if predicate(sku):
                return sku
        raise LookupError("generated catalog lacks a SKU for a demo variant")

    return {
        # A three-week cadence is what makes the demo instructive: the shortest
        # preset becomes unreachable, so the screens show a date being refused
        # when it is set rather than every option looking equally fine.
        "core": first(
            lambda s: s.lifecycle is Lifecycle.CORE
            and s.restock_cadence_days == 21
            and money("12.00") < s.unit_price < money("40.00")
        ),
        "one_time": first(lambda s: s.lifecycle is Lifecycle.OPPORTUNISTIC),
        "seasonal": first(
            lambda s: s.lifecycle is Lifecycle.SEASONAL
            and s.season_end is not None
            and s.season_end <= TODAY
        ),
    }


def _cart(catalog: dict[str, Sku], out_of_stock: Sku) -> list[dict[str, Any]]:
    """A plausible bulk order with one line that could not be picked."""
    others = [
        s
        for s in catalog.values()
        if s.sku_id != out_of_stock.sku_id and s.unit_price < money("30.00")
    ][:3]

    lines = [
        {
            "sku_id": sku.sku_id,
            "name": sku.name,
            "price": fmt(sku.unit_price),
            "out_of_stock": False,
        }
        for sku in others
    ]
    lines.insert(
        2,
        {
            "sku_id": out_of_stock.sku_id,
            "name": out_of_stock.name,
            "price": fmt(out_of_stock.unit_price),
            "out_of_stock": True,
        },
    )
    total = sum(
        (s.unit_price for s in others), start=out_of_stock.unit_price
    )
    return lines, fmt(money(total))


# ---------------------------------------------------------------------------
# A queue with real history in it
# ---------------------------------------------------------------------------

#: Order-date and deadline offsets, in days relative to TODAY, for the members
#: already in line on the demo SKU.
#:
#: Sized to what would genuinely still be open. On a SKU replenished every three
#: weeks, anyone who filed before the last receipt has already been served — so
#: the queue holds recent filings, plus a few long-waiting claims that keep
#: being passed over because their own deadlines are unreachable. Those are the
#: ones that make the filter visible: they have waited longest and still get
#: skipped, without costing anyone behind them a unit.
_WAITING = [
    # Chronically skipped: filed weeks ago, deadlines no receipt can reach.
    (-26, 2),
    (-22, 3),
    (-17, 1),
    # Filed since the last receipt four days ago.
    (-4, 34),
    (-4, 61),
    (-3, 29),
    (-3, 47),
    (-2, 55),
    (-1, 38),
]

DEMO_MEMBER = "you"


def _build_queue(sku: Sku, member_claim: Claim | None) -> ClaimQueue:
    queue = ClaimQueue()
    for index, (ordered, cancel) in enumerate(_WAITING):
        queue.add(
            Claim(
                claim_id=f"C{index:03d}",
                member_id=f"M{index:03d}",
                sku_id=sku.sku_id,
                zip_code="85719" if index % 3 else "85704",
                locked_price=sku.unit_price,
                ordered_at=TODAY + timedelta(days=ordered),
                created_at=TODAY + timedelta(days=ordered + 1),
                cancel_by=TODAY + timedelta(days=cancel),
                membership=Membership.EXECUTIVE if index % 5 == 0 else Membership.BASE,
                prefers_pickup=index % 7 == 0,
            )
        )
    if member_claim is not None:
        queue.add(member_claim)
    return queue


# ---------------------------------------------------------------------------
# Evaluating one deadline choice all the way through
# ---------------------------------------------------------------------------


def _evaluate_choice(
    sku: Sku,
    offer,
    preset: DeadlinePreset,
    *,
    prefers_pickup: bool,
    membership: Membership,
    exact: date | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """What happens if the member picks `preset` — computed, not asserted."""
    cancel_by = resolve_deadline(
        preset, TODAY, exact=exact, ceiling=offer.latest_cancel_by
    )
    verdict = assess_deadline(offer, cancel_by)
    warning = deadline_warning(offer, cancel_by, verdict)

    claim = Claim(
        claim_id="C-you",
        member_id=DEMO_MEMBER,
        sku_id=sku.sku_id,
        zip_code="85719",
        locked_price=sku.unit_price,
        # The line was paid two days before the pick failed.
        ordered_at=TODAY - timedelta(days=2),
        created_at=TODAY,
        cancel_by=cancel_by,
        membership=membership,
        prefers_pickup=prefers_pickup,
    )

    queue = _build_queue(sku, claim)
    position = queue.position_of("C-you")
    waiting = queue.open_count(sku.sku_id)

    estimate = offer.estimate
    assert estimate is not None
    available_on = estimate.eta

    # What the next receipt would actually do. Units come from the reservation
    # policy, not from a number chosen to make the demo look good.
    receipt_units = 24
    split = split_receipt(receipt_units, waiting, POLICY)
    plan = queue.plan(
        sku.sku_id,
        split.to_queue,
        lambda c: available_on
        if c.prefers_pickup
        else available_on + timedelta(days=BATCHING.express_days),
    )
    served = "C-you" in plan.fill
    skipped_ahead = sum(
        1
        for s in plan.skip
        if queue.get(s.claim_id).fifo_key < claim.fifo_key
    )

    outcome: dict[str, Any] | None = None
    if served:
        decision = choose_mode(
            claim, available_on, costs=COSTS, batching=BATCHING
        )
        price = claim.price_on(decision.arrival, sku.unit_price)
        contribution = econ.evaluate(
            decision,
            price=price,
            tier=membership,
            a=ASSUMPTIONS,
        )
        outcome = {
            "mode": decision.mode.value,
            "mode_label": MODE_LABELS[decision.mode],
            "member_copy": MODE_MEMBER_COPY[decision.mode],
            "arrival": _d(decision.arrival),
            "arrival_iso": _iso(decision.arrival),
            "days_to_arrival": (decision.arrival - TODAY).days,
            "fee": fmt(decision.fee_charged),
            "charged": decision.fee_charged > 0,
            "price_paid": fmt(price),
            "price_locked": price == claim.locked_price,
            "cost_to_costco": fmt(decision.cost_estimate),
            "contribution": fmt(contribution.total),
            "contribution_merch": fmt(contribution.total_excl_renewal),
        }

    return {
        "preset": preset.value,
        "label": label or PRESET_LABELS[preset],
        "cancel_by": _d(cancel_by),
        "cancel_by_iso": _iso(cancel_by),
        "days": (cancel_by - TODAY).days,
        "capped": offer.latest_cancel_by is not None
        and cancel_by == offer.latest_cancel_by
        and PRESET_DAYS.get(preset, 0) > (cancel_by - TODAY).days,
        "feasibility": verdict.value,
        "feasibility_label": FEASIBILITY_LABELS[verdict],
        "warning": warning,
        "position": position,
        "queue_length": waiting,
        "skipped_ahead": skipped_ahead,
        "effective_position": position - skipped_ahead,
        "served": served,
        "price_lock_until": _d(claim.lock_expires_on()),
        "nudge_on": _d(cancel_by - timedelta(days=3)),
        "receipt": {
            "units": receipt_units,
            "to_queue": split.to_queue,
            "to_floor": split.to_floor,
            "share": f"{POLICY.queue_share:.0%}",
        },
        "outcome": outcome,
    }


def _queue_table(sku: Sku, member_cancel_offset: int) -> list[dict[str, Any]]:
    """The allocation the operator would see, with reasons attached."""
    claim = Claim(
        claim_id="C-you",
        member_id=DEMO_MEMBER,
        sku_id=sku.sku_id,
        zip_code="85719",
        locked_price=sku.unit_price,
        ordered_at=TODAY - timedelta(days=2),
        created_at=TODAY,
        cancel_by=TODAY + timedelta(days=member_cancel_offset),
        membership=Membership.BASE,
    )
    queue = _build_queue(sku, claim)
    estimate = estimate_return(sku, TODAY, TODAY - timedelta(days=4))
    assert estimate is not None
    available_on = estimate.eta

    split = split_receipt(24, queue.open_count(sku.sku_id), POLICY)
    plan = queue.plan(
        sku.sku_id,
        split.to_queue,
        lambda c: available_on
        if c.prefers_pickup
        else available_on + timedelta(days=BATCHING.express_days),
    )
    skipped = {s.claim_id for s in plan.skip}

    rows: list[dict[str, Any]] = []
    for position, entry in enumerate(queue.open_for(sku.sku_id), start=1):
        if entry.claim_id in skipped:
            status, note = "skipped", "deadline unreachable"
        elif entry.claim_id in plan.fill:
            status, note = "served", "allocated a unit"
        else:
            status, note = "waiting", "no units left this receipt"
        rows.append(
            {
                "position": position,
                "claim_id": entry.claim_id,
                "is_you": entry.member_id == DEMO_MEMBER,
                "ordered_at": _d(entry.ordered_at),
                "cancel_by": _d(entry.cancel_by),
                "slack": entry.slack_days(available_on),
                "status": status,
                "note": note,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _variant(
    key: str,
    sku: Sku,
    catalog: dict[str, Sku],
    *,
    last_received: date | None,
) -> dict[str, Any]:
    spec = LADDER_SPECS[key]
    line = resolve_line(
        sku,
        TODAY,
        line_total=sku.unit_price,
        channel=Channel.ONLINE,
        last_received=last_received,
        nearby=spec["nearby"],
        other_channel_has_stock=spec["other_channel_has_stock"],
        substitutes=spec["substitutes"],
        policy=SOURCING,
    )
    offer = line.offer
    lines, cart_total = _cart(catalog, sku)

    variant: dict[str, Any] = {
        "id": key,
        "ladder": {
            "rung": line.rung.value,
            "rung_label": RUNG_LABELS[line.rung],
            "member_copy": line.resolution.member_copy,
            "headline": line.headline,
            "secondary": line.secondary,
            "refunded": line.refunded,
            "immediate": line.resolution.immediate,
            "gets_the_item": line.resolution.gets_the_item,
            "source": (
                {
                    "warehouse": line.resolution.source.warehouse,
                    "distance_km": line.resolution.source.distance_km,
                    "on_hand": line.resolution.source.on_hand,
                }
                if line.resolution.source
                else None
            ),
            "substitutes": line.resolution.substitutes,
            "passed_over": [
                {
                    "rung": rej.rung.value,
                    "rung_label": RUNG_LABELS[rej.rung],
                    "reason": rej.reason,
                }
                for rej in line.resolution.passed_over
            ],
            "order": [r.value for r in LADDER],
            "policy": {
                "max_transfer_km": SOURCING.max_transfer_km,
                "min_on_hand_to_pull": SOURCING.min_on_hand_to_pull,
            },
        },
        "sku": {
            "id": sku.sku_id,
            "name": sku.name,
            "price": fmt(sku.unit_price),
            "lifecycle": sku.lifecycle.value,
            "cadence": sku.restock_cadence_days,
            "season_end": _d(sku.season_end) if sku.season_end else None,
        },
        "cart": lines,
        "cart_total": cart_total,
        "offer": {
            "claimable": offer.claimable,
            "headline": offer.headline,
            "secondary": offer.secondary,
            "refund": fmt(offer.refund_amount),
            "denial": offer.denial.value if offer.denial else None,
            "prompt": offer.prompt,
            "latest_cancel_by": _d(offer.latest_cancel_by)
            if offer.latest_cancel_by
            else None,
        },
        "choices": [],
        "queue": [],
    }

    if offer.estimate is not None:
        variant["offer"]["estimate"] = {
            "eta": _d(offer.estimate.eta),
            "worst_case": _d(offer.estimate.worst_case),
            "band_days": offer.estimate.band_days,
            "confidence": round(offer.estimate.confidence, 3),
            "copy": offer.estimate.member_copy(),
            "overdue": offer.estimate.overdue,
        }

    if line.rung is not Rung.CLAIM_QUEUE:
        return variant

    # Every preset the offer permits, plus the shortest one it withheld. The
    # withheld option is worth showing: a member typing that date in by hand is
    # exactly who the refusal copy exists for.
    permitted = [
        p for p in offer.presets(TODAY) if p is not DeadlinePreset.EXACT_DATE
    ]
    withheld = [p for p in PRESET_DAYS if p not in permitted]
    for preset in withheld[:1] + permitted:
        variant["choices"].append(
            _evaluate_choice(
                sku,
                offer,
                preset,
                prefers_pickup=False,
                membership=Membership.EXECUTIVE,
            )
        )

    # A hand-typed date landing inside the estimate band, so the demo shows the
    # middle verdict too — tight rather than impossible.
    inside_band: date | None = None
    if offer.estimate is not None and offer.estimate.band_days > 2:
        inside_band = offer.estimate.eta + timedelta(
            days=max(1, offer.estimate.band_days // 2)
        )
        variant["choices"].append(
            _evaluate_choice(
                sku,
                offer,
                DeadlinePreset.EXACT_DATE,
                exact=inside_band,
                label=f"{inside_band:%b %-d} (typed in)",
                prefers_pickup=False,
                membership=Membership.EXECUTIVE,
            )
        )
    variant["choices"].sort(key=lambda c: c["days"])

    # The same tight date as a base member and as someone collecting in person.
    # It has to be the tight one: with plenty of slack every tier routes to the
    # same cheap batched delivery, so the gate would be invisible.
    if inside_band is not None:
        for key, tier, pickup in (
            ("base_member_choice", Membership.BASE, False),
            ("pickup_choice", Membership.BASE, True),
        ):
            variant[key] = _evaluate_choice(
                sku,
                offer,
                DeadlinePreset.EXACT_DATE,
                exact=inside_band,
                label=f"{inside_band:%b %-d}",
                prefers_pickup=pickup,
                membership=tier,
            )

    variant["queue"] = _queue_table(sku, member_cancel_offset=45)
    return variant


def build_scenario() -> dict[str, Any]:
    """Everything the demo needs, computed from the domain modules."""
    world = build_world(WorldConfig(seed=7, days=90, n_skus=400))
    skus = _pick_skus(world.catalog)

    # A real run, for the at-scale figures shown alongside the member screens.
    results = simulate(WorldConfig(seed=7, days=90, n_skus=400))

    variants = [
        _variant(
            "nearby",
            skus["core"],
            world.catalog,
            last_received=TODAY - timedelta(days=4),
        ),
        _variant(
            "core",
            skus["core"],
            world.catalog,
            last_received=TODAY - timedelta(days=4),
        ),
        _variant("one_time", skus["one_time"], world.catalog, last_received=None),
        _variant(
            "seasonal",
            skus["seasonal"],
            world.catalog,
            last_received=TODAY - timedelta(days=10),
        ),
    ]

    subsidy = econ.check_subsidy(ASSUMPTIONS, stop_cost=COSTS.adjacent_stop)

    return {
        "today": _d(TODAY),
        "today_iso": _iso(TODAY),
        "variants": variants,
        "policy": {
            "queue_share": f"{POLICY.queue_share:.0%}",
            "min_cluster": BATCHING.min_cluster,
            "break_even_stops": COSTS.break_even_stops(),
            "express_fee": fmt(COSTS.express_fee),
            "adjacent_stop": fmt(COSTS.adjacent_stop),
            "dense_stop": fmt(COSTS.cost_per_stop(BATCHING.min_cluster)),
        },
        "at_scale": {
            "days": results.world.config.days,
            "oos_events": results.oos_events,
            "coverage": f"{results.coverage_rate:.0%}",
            "addressable": f"{results.addressable_rate:.0%}",
            "promises_kept": f"{results.promise_keeping_rate:.0%}",
            "claims_filled": results.claims_filled,
            "claims_created": results.claims_created,
            "median_days": results.median_fill_days,
            "cost_per_fulfillment": fmt(results.cost_per_fulfillment),
            "units_to_queue": results.units_to_queue,
            "units_to_floor": results.units_to_floor,
            "verdict": results.gate_verdict,
            "sourced": f"{results.sourced_rate:.0%}",
            "got_the_item": f"{results.got_the_item_rate:.0%}",
            "rungs": {
                rung.value: results.rungs.get(rung, 0) for rung in LADDER
            },
        },
        "economics": {
            "basket": fmt(subsidy.basket),
            "incremental": fmt(subsidy.incremental_revenue),
            "margin": fmt(subsidy.gross_margin),
            "stop_cost": fmt(subsidy.stop_cost),
            "merchandise_only": fmt(subsidy.merchandise_only),
            "with_renewal": fmt(subsidy.with_renewal),
            "breakeven_lift": str(
                econ.breakeven_lift(ASSUMPTIONS, stop_cost=COSTS.adjacent_stop)
            ),
            "assumed_lift": str(ASSUMPTIONS.renewal_lift_pp),
        },
    }


def to_json(indent: int | None = None) -> str:
    return json.dumps(build_scenario(), indent=indent, default=str)


if __name__ == "__main__":  # pragma: no cover
    print(to_json(indent=2))
