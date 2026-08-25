"""Synthetic world generation.

The single most important modelling choice in this file is `OOS_PROPENSITY`. The
strongest objection to the whole design is that the items most likely to sell
out skew toward the ones that never come back. A low-SKU assortment carries a
meaningful share of one-time opportunistic buys and closeouts, and those are
exactly the items that empty a shelf and stay empty. Weighting out-of-stock
incidence against lifecycle is what lets the simulation reproduce that
objection instead of quietly assuming it away.

Everything is driven by a seeded Random and an explicit start date, so a given
config always produces the same world.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from ..domain.catalog import BOTH_CHANNELS, Channel, Lifecycle, Sku
from ..domain.claims import DeadlinePreset, Membership
from ..domain.money import money
from ..domain.sourcing import StockPoint

#: Relative likelihood that a given lifecycle is the one that ran out. Not
#: probabilities but weights, normalized against the assortment mix at runtime.
#: Opportunistic and discontinued items are over-represented on purpose.
OOS_PROPENSITY: dict[Lifecycle, float] = {
    Lifecycle.CORE: 1.0,
    Lifecycle.TEMPORARILY_UNAVAILABLE: 4.5,
    Lifecycle.SEASONAL: 2.0,
    Lifecycle.OPPORTUNISTIC: 5.5,
    Lifecycle.DISCONTINUED: 6.0,
}

#: Share of the assortment in each lifecycle.
DEFAULT_MIX: dict[Lifecycle, float] = {
    Lifecycle.CORE: 0.55,
    Lifecycle.TEMPORARILY_UNAVAILABLE: 0.08,
    Lifecycle.SEASONAL: 0.15,
    Lifecycle.OPPORTUNISTIC: 0.15,
    Lifecycle.DISCONTINUED: 0.07,
}

#: Chance that another warehouse in transfer range has units to spare.
#:
#: This is where the ladder earns its keep, and the pattern is not uniform.
#: Core items are stocked everywhere, so a local gap is often just a local gap.
#: A supply disruption is network-wide, so nearby stock is unlikely. Clearance
#: and one-time buys sell through unevenly, which means cross-warehouse sourcing
#: is sometimes the right answer for exactly the items a queue must refuse.
NEARBY_STOCK_CHANCE: dict[Lifecycle, float] = {
    Lifecycle.CORE: 0.55,
    Lifecycle.TEMPORARILY_UNAVAILABLE: 0.15,
    Lifecycle.SEASONAL: 0.35,
    Lifecycle.OPPORTUNISTIC: 0.12,
    Lifecycle.DISCONTINUED: 0.18,
}

#: Chance the other channel has stock, for items sold in both.
OTHER_CHANNEL_CHANCE = 0.22

#: Share of the assortment sold only online, and only in warehouses. The two
#: pools overlap heavily but are not the same pool.
ONLINE_ONLY_SHARE = 0.10
WAREHOUSE_ONLY_SHARE = 0.14

#: Nearby warehouses, as distances in km from the member's delivery area.
_NEIGHBOURS = (("Tucson East", 14.0), ("Tucson NW", 26.0), ("Marana", 47.0), ("Phoenix S", 165.0))


#: How members answer the deadline question when offered presets.
DEFAULT_DEADLINE_MIX: dict[DeadlinePreset, float] = {
    DeadlinePreset.TWO_WEEKS: 0.22,
    DeadlinePreset.ONE_MONTH: 0.38,
    DeadlinePreset.THREE_MONTHS: 0.24,
    DeadlinePreset.UNTIL_CANCELLED: 0.16,
}

_CATEGORIES = (
    ("Organic almond butter", "27 oz", 12, 18),
    ("Paper towels", "12 rolls", 20, 26),
    ("Olive oil", "2 L", 16, 24),
    ("Rotisserie chicken", "each", 5, 7),
    ("Cashews", "2.5 lb", 15, 22),
    ("Laundry detergent", "170 oz", 18, 25),
    ("Ground coffee", "3 lb", 14, 22),
    ("Bath tissue", "30 rolls", 20, 28),
    ("Frozen berries", "4 lb", 10, 15),
    ("Greek yogurt", "48 oz", 6, 10),
    ("Trail mix", "4 lb", 12, 17),
    ("Dish soap", "90 oz", 9, 14),
    ("Patio umbrella", "11 ft", 90, 160),
    ("Down comforter", "queen", 60, 120),
    ("Cordless drill kit", "20V", 80, 150),
    ("Air purifier", "large room", 110, 190),
    ("Wool runner", "2x8", 45, 90),
    ("Cast iron skillet", '12"', 25, 45),
    ("Hiking socks", "4 pack", 15, 22),
    ("Standing desk", "60 in", 200, 380),
)

_ZIPS = (
    "85705", "85712", "85715", "85718", "85719", "85730", "85741", "85745",
    "85748", "85750", "85704", "85710", "85716", "85721", "85742",
)


@dataclass(frozen=True, slots=True)
class WorldConfig:
    seed: int = 7
    days: int = 90
    start: date = date(2026, 9, 1)
    n_skus: int = 400
    oos_events_per_day: int = 9
    claim_optin_rate: float = 0.62
    executive_share: float = 0.43
    pickup_pref_rate: float = 0.24
    proceed_past_warning_rate: float = 0.35
    """Share of members who queue anyway after being told the date is tight."""
    extend_on_warning_rate: float = 0.40
    """Share who take the later date the warning suggests instead."""
    n_members: int = 2600
    online_share: float = 0.80
    substitutes_max: int = 5
    receipt_units_low: int = 4
    receipt_units_high: int = 40
    lifecycle_mix: dict[Lifecycle, float] = field(default_factory=lambda: dict(DEFAULT_MIX))
    deadline_mix: dict[DeadlinePreset, float] = field(
        default_factory=lambda: dict(DEFAULT_DEADLINE_MIX)
    )

    @property
    def end(self) -> date:
        return self.start + timedelta(days=self.days - 1)


@dataclass(frozen=True, slots=True)
class OosEvent:
    """A paid delivery line that could not be picked."""

    day: date
    member_id: str
    sku_id: str
    zip_code: str
    line_total: Decimal
    ordered_at: date
    membership: Membership
    prefers_pickup: bool
    channel: Channel
    nearby: tuple[StockPoint, ...]
    """On-hand at other warehouses when this line failed to pick."""
    other_channel_has_stock: bool
    substitutes: int


@dataclass(frozen=True, slots=True)
class Receipt:
    """Units arriving at the warehouse."""

    day: date
    sku_id: str
    units: int


@dataclass(slots=True)
class World:
    config: WorldConfig
    catalog: dict[str, Sku]
    events: list[OosEvent]
    receipts: list[Receipt]
    last_received: dict[str, date]
    """Receipt history as of the day before the run starts."""

    def receipts_by_day(self) -> dict[date, list[Receipt]]:
        grouped: dict[date, list[Receipt]] = {}
        for receipt in self.receipts:
            grouped.setdefault(receipt.day, []).append(receipt)
        return grouped

    def events_by_day(self) -> dict[date, list[OosEvent]]:
        grouped: dict[date, list[OosEvent]] = {}
        for event in self.events:
            grouped.setdefault(event.day, []).append(event)
        return grouped

    def restocks_within(self, sku_id: str, after: date, days: int) -> bool:
        """Ground truth for gate one: did this SKU actually come back in time?

        Available only because this is a simulation. The real version of this
        number is what the 90-day shadow run exists to measure.
        """
        limit = after + timedelta(days=days)
        return any(
            r.sku_id == sku_id and after < r.day <= limit for r in self.receipts
        )


def _weighted(rng: random.Random, weights: dict) -> object:
    keys = list(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def _cadence_for(rng: random.Random, lifecycle: Lifecycle) -> int | None:
    if lifecycle is Lifecycle.CORE:
        return rng.choice((7, 10, 14, 21))
    if lifecycle is Lifecycle.TEMPORARILY_UNAVAILABLE:
        return rng.choice((21, 30, 45, 60))
    if lifecycle is Lifecycle.SEASONAL:
        return rng.choice((14, 21, 28))
    return None


def build_catalog(cfg: WorldConfig, rng: random.Random) -> dict[str, Sku]:
    catalog: dict[str, Sku] = {}
    for index in range(cfg.n_skus):
        lifecycle: Lifecycle = _weighted(rng, cfg.lifecycle_mix)  # type: ignore[assignment]
        name, size, low, high = rng.choice(_CATEGORIES)
        cadence = _cadence_for(rng, lifecycle)

        season_end = None
        if lifecycle is Lifecycle.SEASONAL:
            # Some seasons close inside the run window, some outside it. The
            # ones that close inside are what exercise the SEASON_CLOSED path.
            season_end = cfg.start + timedelta(days=rng.randint(20, 200))

        roll = rng.random()
        if roll < ONLINE_ONLY_SHARE:
            channels = frozenset({Channel.ONLINE})
        elif roll < ONLINE_ONLY_SHARE + WAREHOUSE_ONLY_SHARE:
            channels = frozenset({Channel.WAREHOUSE})
        else:
            channels = BOTH_CHANNELS

        sku_id = f"{1_500_000 + index * 37:07d}"
        catalog[sku_id] = Sku(
            sku_id=sku_id,
            name=f"{name}, {size}",
            unit_price=money(rng.uniform(low, high)),
            lifecycle=lifecycle,
            restock_cadence_days=cadence,
            season_end=season_end,
            channels=channels,
        )
    return catalog


def build_receipts(
    cfg: WorldConfig, catalog: dict[str, Sku], rng: random.Random
) -> tuple[list[Receipt], dict[str, date]]:
    """Schedule replenishment, plus the history the estimator reads."""
    receipts: list[Receipt] = []
    last_received: dict[str, date] = {}

    for sku in catalog.values():
        if sku.restock_cadence_days is None:
            continue

        # Where in its cycle this SKU sits when the run begins.
        phase = rng.randrange(sku.restock_cadence_days)
        last_received[sku.sku_id] = cfg.start - timedelta(days=phase)

        day = cfg.start + timedelta(days=sku.restock_cadence_days - phase)
        while day <= cfg.end:
            if sku.season_end is not None and day >= sku.season_end:
                break
            # Cadence slips. Late receipts exercise the overdue-estimate path.
            slip = rng.choices((0, 1, 3, 7), weights=(60, 20, 13, 7), k=1)[0]
            arrival = day + timedelta(days=slip)
            if arrival > cfg.end:
                break
            receipts.append(
                Receipt(
                    day=arrival,
                    sku_id=sku.sku_id,
                    units=rng.randint(cfg.receipt_units_low, cfg.receipt_units_high),
                )
            )
            day = arrival + timedelta(days=sku.restock_cadence_days)

    receipts.sort(key=lambda r: (r.day, r.sku_id))
    return receipts, last_received


def build_events(
    cfg: WorldConfig, catalog: dict[str, Sku], rng: random.Random
) -> list[OosEvent]:
    """Generate out-of-stock lines, weighted toward items that never return.

    Two weights multiply here. Lifecycle propensity is the honest reproduction
    of the design's strongest objection. Popularity is drawn per SKU from a
    long-tailed distribution, because retail demand is concentrated: a small
    number of items generate most out-of-stock events, which is also what makes
    a per-area demand signal worth aggregating at all. Spreading events evenly
    across the assortment would make the queue look thinner than it is.
    """
    sku_ids = list(catalog)
    weights = [
        OOS_PROPENSITY[catalog[s].lifecycle] * rng.paretovariate(1.4)
        for s in sku_ids
    ]

    events: list[OosEvent] = []
    for offset in range(cfg.days):
        day = cfg.start + timedelta(days=offset)
        count = max(0, int(rng.gauss(cfg.oos_events_per_day, 2.2)))
        for _ in range(count):
            sku_id = rng.choices(sku_ids, weights=weights, k=1)[0]
            tier = (
                Membership.EXECUTIVE
                if rng.random() < cfg.executive_share
                else Membership.BASE
            )
            sku = catalog[sku_id]
            channel = (
                Channel.ONLINE
                if rng.random() < cfg.online_share
                else Channel.WAREHOUSE
            )
            if channel not in sku.channels:
                # A member cannot order through a channel that never carried it.
                channel = next(iter(sku.channels))

            nearby: tuple[StockPoint, ...] = ()
            if rng.random() < NEARBY_STOCK_CHANCE[sku.lifecycle]:
                name, distance = rng.choice(_NEIGHBOURS)
                nearby = (StockPoint(name, distance, rng.randint(1, 30)),)

            other = (
                len(sku.channels) > 1 and rng.random() < OTHER_CHANNEL_CHANCE
            )

            events.append(
                OosEvent(
                    day=day,
                    member_id=f"M{rng.randrange(cfg.n_members):06d}",
                    sku_id=sku_id,
                    zip_code=rng.choice(_ZIPS),
                    line_total=sku.unit_price,
                    # The line was paid for a day or two before the pick failed.
                    ordered_at=day - timedelta(days=rng.choice((0, 1, 1, 2))),
                    membership=tier,
                    prefers_pickup=rng.random() < cfg.pickup_pref_rate,
                    channel=channel,
                    nearby=nearby,
                    other_channel_has_stock=other,
                    substitutes=rng.randint(0, cfg.substitutes_max),
                )
            )
    return events


def build_world(cfg: WorldConfig | None = None) -> World:
    cfg = cfg or WorldConfig()
    rng = random.Random(cfg.seed)
    catalog = build_catalog(cfg, rng)
    receipts, last_received = build_receipts(cfg, catalog, rng)
    events = build_events(cfg, catalog, rng)
    return World(
        config=cfg,
        catalog=catalog,
        events=events,
        receipts=receipts,
        last_received=last_received,
    )
