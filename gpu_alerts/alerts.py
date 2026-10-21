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

Every retailer whose eligible offer was at or under target holds the alert until
it answers completely (every page read) with nothing at or under target. A scan
counts toward re-arming only when no retailer holds the alert. If a holder is
blocked for several runs in a row, the other retailers' higher prices are not
evidence that its deal is gone, so the alert stays disarmed and the deal is not
pinged again when the retailer returns. That covers a deal the ping named, one
shown next to it, and one that moved to another retailer without a ping. The
hold has a limit: after RELEASE_AFTER scans in a row above target without a
complete answer from a holder (a day at the default hourly checks), the alert
re-arms anyway, so a retailer that stays blocked or half-read cannot mute new
crossings elsewhere for good. Its deal can then ping once more when it returns.
"""

from collections.abc import Collection
from dataclasses import dataclass, replace

FURTHER_DROP_PCT = 3          # percent
FURTHER_DROP_CENTS = 2500
REARM_AFTER = 2               # consecutive scans above target before re-arming
RELEASE_AFTER = 24            # consecutive scans above target a holder may leave unanswered

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
    # retailers seen at or under target and not since seen above it in a complete answer
    holding_retailers: frozenset[str] = frozenset()
    held_streak: int = 0          # consecutive scans above target a holder did not answer completely


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
    under_target: Collection[str] | None = None,
) -> Decision:
    """One scan's outcome for one alert.

    best_price_cents and best_retailer describe the cheapest eligible offer, or
    None when there is none. answered is the set of retailers that answered the
    scan completely (status ok or empty, nothing left unread). When answered is
    None, a missing price means "no retailer answered", as before. under_target
    is the set of retailers with an eligible offer at or under the target,
    partial reads included; when None it is best_retailer alone.
    """
    price = best_price_cents
    complete = frozenset(answered or ())
    if price is None or price > state.target_cents:
        if answered is not None and not answered:
            return Decision(False, UNCHANGED, state)
        if price is None and answered is None:
            return Decision(False, UNCHANGED, state)
        if state.armed and state.notified_price_cents is None:
            return Decision(False, UNCHANGED, state)
        # A holder that answered completely, above target or with nothing eligible, lets go.
        holding = state.holding_retailers - complete
        if holding:
            held = state.held_streak + 1
            if held < RELEASE_AFTER:
                # A holder did not answer completely: not evidence its deal is gone.
                return Decision(False, UNCHANGED, replace(state, holding_retailers=holding, held_streak=held))
            # Every other answer was above target for RELEASE_AFTER scans: stop waiting.
            return Decision(False, REARMED, AlertState(state.target_cents))
        streak = state.above_streak + 1
        if streak < REARM_AFTER:
            return Decision(False, UNCHANGED, replace(
                state, above_streak=streak, holding_retailers=holding, held_streak=0))
        return Decision(False, REARMED, AlertState(state.target_cents))
    if under_target is None:
        under_target = {best_retailer} if best_retailer else ()
    holding = (state.holding_retailers - complete) | frozenset(under_target)
    state = replace(state, above_streak=0, holding_retailers=holding, held_streak=0)
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
