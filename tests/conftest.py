"""Shared builders. Everything takes explicit dates, no wall-clock reads."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from holdmyplace.domain.catalog import BOTH_CHANNELS, Channel, Lifecycle, Sku
from holdmyplace.domain.claims import Claim, Membership
from holdmyplace.domain.money import money

TODAY = date(2026, 9, 1)


def day(offset: int) -> date:
    """A date relative to the fixed reference day."""
    return TODAY + timedelta(days=offset)


@pytest.fixture
def today() -> date:
    return TODAY


def make_sku(
    sku_id: str = "1500001",
    *,
    lifecycle: Lifecycle = Lifecycle.CORE,
    price: str = "18.99",
    cadence: int | None = 14,
    season_end: date | None = None,
    channels: frozenset[Channel] = BOTH_CHANNELS,
) -> Sku:
    return Sku(
        sku_id=sku_id,
        name="Test item, 1 ct",
        unit_price=money(price),
        lifecycle=lifecycle,
        restock_cadence_days=cadence,
        season_end=season_end,
        channels=channels,
    )


def make_claim(
    claim_id: str,
    *,
    ordered_offset: int = 0,
    cancel_offset: int = 30,
    created_offset: int | None = None,
    sku_id: str = "1500001",
    zip_code: str = "85719",
    price: str = "18.99",
    membership: Membership = Membership.BASE,
    prefers_pickup: bool = False,
) -> Claim:
    """A claim positioned by day-offsets from the reference day.

    `ordered_offset` is the FIFO key; `created_offset` defaults to it so most
    tests only have to think about one of the two.
    """
    created = day(ordered_offset if created_offset is None else created_offset)
    return Claim(
        claim_id=claim_id,
        member_id=f"M-{claim_id}",
        sku_id=sku_id,
        zip_code=zip_code,
        locked_price=money(price),
        ordered_at=day(ordered_offset),
        created_at=created,
        cancel_by=day(cancel_offset),
        membership=membership,
        prefers_pickup=prefers_pickup,
    )


@pytest.fixture
def sku() -> Sku:
    return make_sku()
