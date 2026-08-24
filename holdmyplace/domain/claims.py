"""The claim itself: its deadline, its price lock, its lifecycle.

Two rules in here carry most of the design's weight.

The deadline is a *cancel-by* date, never a deliver-by date. The member is
declaring the point past which they no longer want the item; the company is not
promising to arrive before it. Every piece of member-facing copy in this module
reflects that, because getting it backwards converts an optional convenience
into an implied commitment.

The price lock is capped independently of the claim length. A member may hold a
claim for six months; they may not hold a six-month locked price on a
freight-sensitive item. Past the lock the claim survives at the current price,
with explicit re-consent required if the price has moved materially.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum

from .money import money, pct

#: Longest a claim may run when the member picks "until I cancel". A claim with
#: no bound at all is a support liability and an unbounded consented delay.
MAX_CLAIM_DAYS = 180

#: How long the price paid at the original order is honoured.
PRICE_LOCK_DAYS = 30

#: Price movement past the lock that requires the member to re-consent rather
#: than being silently charged the new price.
RECONSENT_THRESHOLD = pct("0.05")

#: Days before expiry to send the single extend-or-release nudge.
NUDGE_LEAD_DAYS = 3


class Membership(str, Enum):
    """Membership tier. Governs access to subsidized fulfillment modes."""

    BASE = "base"
    EXECUTIVE = "executive"


class DeadlinePreset(str, Enum):
    """What the member actually picks.

    Presets rather than a bare date picker. A raw calendar produces unusable
    data — people choose today, or arbitrarily a year out — while a short list
    of intervals is faster to answer and aggregates cleanly.
    """

    TWO_WEEKS = "two_weeks"
    ONE_MONTH = "one_month"
    THREE_MONTHS = "three_months"
    UNTIL_CANCELLED = "until_cancelled"
    EXACT_DATE = "exact_date"


PRESET_DAYS: dict[DeadlinePreset, int] = {
    DeadlinePreset.TWO_WEEKS: 14,
    DeadlinePreset.ONE_MONTH: 30,
    DeadlinePreset.THREE_MONTHS: 90,
    DeadlinePreset.UNTIL_CANCELLED: MAX_CLAIM_DAYS,
}

#: The label attached to the control. Kept here, next to the logic, so the
#: framing cannot drift away from the mechanic it describes.
DEADLINE_PROMPT = "Cancel my claim if it hasn't arrived by"


def resolve_deadline(
    preset: DeadlinePreset,
    created_at: date,
    *,
    exact: date | None = None,
    ceiling: date | None = None,
) -> date:
    """Turn a member's pick into a concrete cancel-by date.

    `ceiling` is an eligibility-imposed cap — a seasonal item's season end —
    and always wins over the member's choice, since a claim past it can never
    be filled.
    """
    if preset is DeadlinePreset.EXACT_DATE:
        if exact is None:
            raise ValueError("EXACT_DATE requires an explicit date")
        if exact <= created_at:
            raise ValueError("cancel-by date must be in the future")
        chosen = min(exact, created_at + timedelta(days=MAX_CLAIM_DAYS))
    else:
        chosen = created_at + timedelta(days=PRESET_DAYS[preset])

    if ceiling is not None:
        chosen = min(chosen, ceiling)
    return chosen


class ClaimStatus(str, Enum):
    OPEN = "open"
    FILLED = "filled"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


TERMINAL = frozenset({ClaimStatus.FILLED, ClaimStatus.EXPIRED, ClaimStatus.CANCELLED})


@dataclass(slots=True)
class Claim:
    """A member's held place in line for one out-of-stock unit.

    `ordered_at` is the timestamp of the original order, not of the claim. That
    is the FIFO key: a member's position reflects when they tried to buy the
    item, so nothing about filing or amending a claim can improve it.
    """

    claim_id: str
    member_id: str
    sku_id: str
    zip_code: str
    locked_price: Decimal
    ordered_at: date
    created_at: date
    cancel_by: date
    membership: Membership = Membership.BASE
    prefers_pickup: bool = False
    status: ClaimStatus = ClaimStatus.OPEN
    filled_on: date | None = None
    closed_on: date | None = None
    times_skipped: int = 0
    extensions: int = 0
    _nudged: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        self.locked_price = money(self.locked_price)
        if self.cancel_by <= self.created_at:
            raise ValueError(f"{self.claim_id}: cancel-by must follow creation")
        if self.created_at < self.ordered_at:
            raise ValueError(f"{self.claim_id}: claim precedes its own order")

    # -- ordering ---------------------------------------------------------

    @property
    def fifo_key(self) -> tuple[date, str]:
        """Sort key for allocation. Claim id breaks ties deterministically."""
        return (self.ordered_at, self.claim_id)

    @property
    def is_open(self) -> bool:
        return self.status is ClaimStatus.OPEN

    # -- deadline ---------------------------------------------------------

    def slack_days(self, as_of: date) -> int:
        """Days left before the member's own cancel-by date."""
        return (self.cancel_by - as_of).days

    def has_lapsed(self, as_of: date) -> bool:
        return as_of > self.cancel_by

    def deliverable_by(self, arrival: date) -> bool:
        """Whether an arrival on `arrival` still satisfies this claim.

        Used as a *filter* during allocation, never as a sort key. A claim that
        cannot be met by an incoming receipt is passed over; it does not
        consume a unit, and it does not move ahead of anyone.
        """
        return arrival <= self.cancel_by

    def due_for_nudge(self, as_of: date) -> bool:
        """One message before expiry: extend, or let it go."""
        return (
            self.is_open
            and not self._nudged
            and 0 <= self.slack_days(as_of) <= NUDGE_LEAD_DAYS
        )

    def mark_nudged(self) -> None:
        self._nudged = True

    def extend(self, new_cancel_by: date, as_of: date) -> None:
        """Push the deadline out at the member's request.

        Extending cannot improve position — `ordered_at` is untouched — so this
        is safe to allow freely.
        """
        if not self.is_open:
            raise ValueError(f"{self.claim_id}: cannot extend a {self.status} claim")
        if new_cancel_by <= self.cancel_by:
            raise ValueError("an extension must move the deadline later")
        ceiling = self.created_at + timedelta(days=MAX_CLAIM_DAYS)
        self.cancel_by = min(new_cancel_by, ceiling)
        self.extensions += 1
        self._nudged = False
        del as_of  # recorded by the caller's event log, not needed here

    # -- price ------------------------------------------------------------

    def lock_expires_on(self) -> date:
        return self.created_at + timedelta(days=PRICE_LOCK_DAYS)

    def price_on(self, as_of: date, current_price: Decimal) -> Decimal:
        """What the member pays if the item is fulfilled on `as_of`.

        Inside the lock window the original price holds even if the shelf price
        rose. Outside it, the member pays the current price — and if that is a
        material increase the caller must obtain re-consent first.
        """
        if as_of <= self.lock_expires_on():
            return self.locked_price
        return money(current_price)

    def requires_reconsent(self, as_of: date, current_price: Decimal) -> bool:
        if as_of <= self.lock_expires_on():
            return False
        if self.locked_price == 0:
            return False
        movement = (money(current_price) - self.locked_price) / self.locked_price
        return movement > RECONSENT_THRESHOLD

    # -- transitions ------------------------------------------------------

    def fill(self, as_of: date) -> None:
        self._require_open()
        self.status = ClaimStatus.FILLED
        self.filled_on = as_of
        self.closed_on = as_of

    def expire(self, as_of: date) -> None:
        self._require_open()
        self.status = ClaimStatus.EXPIRED
        self.closed_on = as_of

    def cancel(self, as_of: date) -> None:
        self._require_open()
        self.status = ClaimStatus.CANCELLED
        self.closed_on = as_of

    def skip(self) -> None:
        """Record that a receipt passed this claim over. Not a state change."""
        self._require_open()
        self.times_skipped += 1

    def _require_open(self) -> None:
        if not self.is_open:
            raise ValueError(f"{self.claim_id}: already {self.status.value}")
