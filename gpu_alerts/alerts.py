"""When to notify: a pure state machine per alert, so it can be tested as a table.

    armed, price <= target      notify "crossed", disarm, remember the price and retailer
    disarmed, price <= target   notify "further_drop" only when the price fell at least
                                3 percent or $25 below the last notified price
    price > target              re-arm silently, but only after REARM_AFTER scans in a row
                                above target, so one blocked retailer (whose low price
                                drops out of a single scan) cannot cause a repeat ping
    nothing eligible            counts like a scan above target when retailers answered
                                (sold out everywhere), so a restock under target pings again
    no retailer answered        no change (a failed scan is not a price rise)

A scan counts toward re-arming only when the retailer that produced the notified
price answered in it. If that retailer is blocked for several runs in a row, the
other retailers' higher prices are not evidence that the deal is gone, so the
alert stays disarmed and the deal is not pinged again when the retailer returns.
"""

from collections.abc import Collection
from dataclasses import dataclass, replace

FURTHER_DROP_PCT = 3          # percent
FURTHER_DROP_CENTS = 2500
REARM_AFTER = 2               # consecutive scans above target before re-arming

CROSSED = "crossed"
FURTHER_DROP = "further_drop"
REARMED = "rearmed"
UNCHANGED = "unchanged"


@dataclass(frozen=True)
class AlertState:
    target_cents: int
    armed: bool = True
    notified_price_cents: int | None = None
    above_streak: int = 0         # consecutive scans above target while disarmed
    notified_retailer: str | None = None


@dataclass(frozen=True)
class Decision:
    notify: bool
    kind: str
    new_state: AlertState


def is_further_drop(previous_cents: int, price_cents: int) -> bool:
    drop = previous_cents - price_cents
    if drop <= 0:
        return False
    return drop >= FURTHER_DROP_CENTS or drop * 100 >= previous_cents * FURTHER_DROP_PCT


def decide(
    state: AlertState,
    best_price_cents: int | None,
    best_retailer: str | None = None,
    answered: Collection[str] | None = None,
) -> Decision:
    """One scan's outcome for one alert.

    best_price_cents and best_retailer describe the cheapest eligible offer, or
    None when there is none. answered is the set of retailers that answered the
    scan (status ok or empty). When answered is None, a missing price means "no
    retailer answered", as before.
    """
    if answered is not None and not answered:
        return Decision(False, UNCHANGED, state)
    if best_price_cents is None and answered is None:
        return Decision(False, UNCHANGED, state)
    price = best_price_cents
    if price is None or price > state.target_cents:
        if state.armed and state.notified_price_cents is None:
            return Decision(False, UNCHANGED, state)
        if (answered is not None and state.notified_retailer is not None
                and state.notified_retailer not in answered):
            # The retailer holding the deal did not answer: not evidence the deal is gone.
            return Decision(False, UNCHANGED, state)
        streak = state.above_streak + 1
        if streak < REARM_AFTER:
            return Decision(False, UNCHANGED, replace(state, above_streak=streak))
        return Decision(
            False, REARMED,
            replace(state, armed=True, notified_price_cents=None, above_streak=0, notified_retailer=None),
        )
    state = replace(state, above_streak=0)
    if state.armed:
        return Decision(True, CROSSED, replace(
            state, armed=False, notified_price_cents=price, notified_retailer=best_retailer))
    previous = state.notified_price_cents
    if previous is None:
        # Disarmed without a remembered price (e.g. imported state): treat as a fresh crossing.
        return Decision(True, CROSSED, replace(
            state, armed=False, notified_price_cents=price, notified_retailer=best_retailer))
    if is_further_drop(previous, price):
        return Decision(True, FURTHER_DROP, replace(
            state, notified_price_cents=price, notified_retailer=best_retailer))
    return Decision(False, UNCHANGED, state)
