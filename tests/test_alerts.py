import pytest

from gpu_alerts.alerts import (
    CROSSED, FURTHER_DROP, RELEASE_AFTER, REARMED, UNCHANGED, AlertState, decide, is_further_drop,
)

T = 150_000  # $1,500.00 target


@pytest.mark.parametrize(
    "state, price, notify, kind, new_state",
    [
        # armed, price above target: nothing to do
        (AlertState(T), 160_000, False, UNCHANGED, AlertState(T)),
        # armed, price at target counts as crossed
        (AlertState(T), T, True, CROSSED, AlertState(T, False, T)),
        (AlertState(T), 149_000, True, CROSSED, AlertState(T, False, 149_000)),
        # no price and no answered set given: read as "no retailer answered", never a change
        (AlertState(T), None, False, UNCHANGED, AlertState(T)),
        (AlertState(T, False, 149_000), None, False, UNCHANGED, AlertState(T, False, 149_000)),
        # disarmed, same or slightly lower price: silent
        (AlertState(T, False, 149_000), 149_000, False, UNCHANGED, AlertState(T, False, 149_000)),
        (AlertState(T, False, 149_000), 147_000, False, UNCHANGED, AlertState(T, False, 149_000)),
        # disarmed, price rose but still under target: silent, keep the notified price
        (AlertState(T, False, 140_000), 145_000, False, UNCHANGED, AlertState(T, False, 140_000)),
        # disarmed, drop of $25 or more: notify again
        (AlertState(T, False, 149_000), 146_500, True, FURTHER_DROP, AlertState(T, False, 146_500)),
        # disarmed, drop of 3 percent on a cheap card (under $25): notify again
        (AlertState(30_000, False, 30_000), 29_100, True, FURTHER_DROP, AlertState(30_000, False, 29_100)),
        (AlertState(30_000, False, 30_000), 29_200, False, UNCHANGED, AlertState(30_000, False, 30_000)),
        # price back above target: re-arm silently
        (AlertState(T, False, 149_000), 150_001, False, UNCHANGED, AlertState(T, False, 149_000, 1)),
        (AlertState(T, False, 149_000, 1), 150_001, False, REARMED, AlertState(T)),
        (AlertState(T, False, 149_000, 1), 149_000, False, UNCHANGED, AlertState(T, False, 149_000)),
        # disarmed without a remembered price: treat as a fresh crossing
        (AlertState(T, False, None), 149_000, True, CROSSED, AlertState(T, False, 149_000)),
    ],
)
def test_decide_table(state, price, notify, kind, new_state):
    d = decide(state, price)
    assert (d.notify, d.kind, d.new_state) == (notify, kind, new_state)


def _run(target, prices, state=None):
    state = state or AlertState(target)
    kinds = []
    for p in prices:
        d = decide(state, p)
        kinds.append(d.kind if d.notify else None)
        state = d.new_state
    return kinds, state


def test_steady_price_under_target_notifies_once():
    kinds, _ = _run(T, [140_000] * 24)
    assert kinds == [CROSSED] + [None] * 23


def test_flapping_prices_notify_once_per_crossing():
    # One scan above target (e.g. the cheap retailer was blocked) must not re-arm.
    prices = [151_000, 149_000, 151_000, 149_000, 149_000, 151_000, None, 149_000]
    kinds, state = _run(T, prices)
    assert kinds == [None, CROSSED, None, None, None, None, None, None]
    assert state == AlertState(T, False, 149_000)


def test_two_scans_above_target_rearm():
    prices = [149_000, 151_000, 151_000, 149_500]
    kinds, state = _run(T, prices)
    assert kinds == [CROSSED, None, None, CROSSED]
    assert state == AlertState(T, False, 149_500)


def test_failed_scans_do_not_rearm():
    # A scan with no price between two under-target prices must not cause a second ping.
    kinds, _ = _run(T, [149_000, None, None, 149_000])
    assert kinds == [CROSSED, None, None, None]


def test_staircase_of_drops():
    kinds, state = _run(T, [149_000, 148_000, 146_400, 146_000, 140_000, 141_000, 139_000])
    assert kinds == [CROSSED, None, FURTHER_DROP, None, FURTHER_DROP, None, None]
    assert state.notified_price_cents == 140_000


def test_is_further_drop_thresholds():
    assert is_further_drop(200_000, 197_500)        # exactly $25
    assert not is_further_drop(200_000, 197_501)
    assert is_further_drop(10_000, 9_700)            # exactly 3 percent
    assert not is_further_drop(10_000, 9_701)
    assert not is_further_drop(10_000, 10_500)       # a rise is never a drop


# With the retailer of the best offer and the set of retailers that answered completely.

BB, NE, AZ = "bestbuy", "newegg", "amazon"
BOTH = frozenset({BB, NE})
ALL = frozenset({BB, NE, AZ})


def held(*retailers):
    return frozenset(retailers)


@pytest.mark.parametrize(
    "state, price, retailer, answered, notify, kind, new_state",
    [
        # crossing remembers the retailer
        (AlertState(T), 140_000, BB, BOTH, True, CROSSED, AlertState(T, False, 140_000, 0, BB, held(BB))),
        # no retailer answered: never a change
        (AlertState(T, False, 140_000, 1, BB, held(BB)), None, None, frozenset(), False, UNCHANGED,
         AlertState(T, False, 140_000, 1, BB, held(BB))),
        # only partial reads answered, one of them under target: the offer is real
        (AlertState(T), 140_000, BB, frozenset(), True, CROSSED, AlertState(T, False, 140_000, 0, BB, held(BB))),
        # answered but nothing eligible (sold out everywhere): counts like above target
        (AlertState(T, False, 140_000, 0, BB, held(BB)), None, None, BOTH, False, UNCHANGED,
         AlertState(T, False, 140_000, 1, BB)),
        (AlertState(T, False, 140_000, 1, BB, held(BB)), None, None, BOTH, False, REARMED, AlertState(T)),
        # armed with nothing notified and nothing eligible: nothing to do
        (AlertState(T), None, None, BOTH, False, UNCHANGED, AlertState(T)),
        # the notified retailer did not answer: a higher price elsewhere is no change, but
        # the scan counts toward letting go of the retailer
        (AlertState(T, False, 140_000, 1, BB, held(BB)), 160_000, NE, frozenset({NE}), False, UNCHANGED,
         AlertState(T, False, 140_000, 1, BB, held(BB), 1)),
        (AlertState(T, False, 140_000, 0, BB, held(BB)), None, None, frozenset({NE}), False, UNCHANGED,
         AlertState(T, False, 140_000, 0, BB, held(BB), 1)),
        # ...but it answered with a higher price: counts
        (AlertState(T, False, 140_000, 1, BB, held(BB)), 160_000, NE, BOTH, False, REARMED, AlertState(T)),
        # under target from another retailer while the notified one is blocked: resets the
        # streak, and both now hold the alert
        (AlertState(T, False, 140_000, 1, BB, held(BB)), 145_000, NE, frozenset({NE}), False, UNCHANGED,
         AlertState(T, False, 140_000, 0, BB, held(BB, NE))),
        # a further drop moves the notified retailer
        (AlertState(T, False, 140_000, 0, BB, held(BB)), 130_000, NE, BOTH, True, FURTHER_DROP,
         AlertState(T, False, 130_000, 0, NE, held(NE))),
        # another retailer under target at ping time did not answer while the notified one
        # answered above target: no change, and only the notified one leaves the set
        (AlertState(T, False, 140_000, 1, BB, held(BB, AZ)), 160_000, BB, BOTH, False, UNCHANGED,
         AlertState(T, False, 140_000, 1, BB, held(AZ), 1)),
        # a holder still without a complete answer keeps the alert disarmed...
        (AlertState(T, False, 140_000, 0, BB, held(AZ), 5), 160_000, BB, BOTH, False, UNCHANGED,
         AlertState(T, False, 140_000, 0, BB, held(AZ), 6)),
        # ...until RELEASE_AFTER such scans in a row: then the alert re-arms anyway
        (AlertState(T, False, 140_000, 0, BB, held(AZ), RELEASE_AFTER - 1), 160_000, BB, BOTH, False, REARMED,
         AlertState(T)),
        # a scan under target starts the count again
        (AlertState(T, False, 140_000, 0, BB, held(AZ), 5), 145_000, NE, BOTH, False, UNCHANGED,
         AlertState(T, False, 140_000, 0, BB, held(AZ, NE))),
        # a scan no retailer answered completely neither counts nor starts it again
        (AlertState(T, False, 140_000, 0, BB, held(AZ), 5), 160_000, BB, frozenset(), False, UNCHANGED,
         AlertState(T, False, 140_000, 0, BB, held(AZ), 5)),
        # every retailer in the set answered above target: counts
        (AlertState(T, False, 140_000, 1, BB, held(BB, AZ)), 160_000, BB, ALL, False, REARMED, AlertState(T)),
        # state without a notified retailer (written before it was stored): counts as before
        (AlertState(T, False, 140_000, 1), 160_000, NE, frozenset({NE}), False, REARMED, AlertState(T)),
    ],
)
def test_decide_with_retailers_table(state, price, retailer, answered, notify, kind, new_state):
    d = decide(state, price, retailer, answered)
    assert (d.notify, d.kind, d.new_state) == (notify, kind, new_state)


def test_every_retailer_under_target_holds_the_alert():
    d = decide(AlertState(T), 140_000, BB, ALL, under_target={BB, AZ})
    assert d.kind == CROSSED and d.new_state.holding_retailers == held(BB, AZ)
    # A partial read under target joins the set; one above target keeps its old place in it.
    state = AlertState(T, False, 140_000, 0, BB, held(BB, AZ))
    d = decide(state, 145_000, NE, frozenset({BB}), under_target={NE})
    assert d.kind == UNCHANGED and d.new_state.holding_retailers == held(AZ, NE)


def _run_scans(target, scans):
    """scans: (price, retailer, answered) or (price, retailer, answered, under_target) per run."""
    state, kinds = AlertState(target), []
    for price, retailer, answered, *under in scans:
        d = decide(state, price, retailer, answered, *under)
        kinds.append(d.kind if d.notify else None)
        state = d.new_state
    return kinds, state


def test_cheapest_retailer_blocked_for_several_runs_does_not_repeat_the_ping():
    # Best Buy has the deal at $1,400; it is blocked for three runs while Newegg shows $1,600.
    scans = [(140_000, BB, BOTH)] + [(160_000, NE, frozenset({NE}))] * 3 + [(140_000, BB, BOTH)]
    kinds, state = _run_scans(T, scans)
    assert kinds == [CROSSED, None, None, None, None]
    assert state == AlertState(T, False, 140_000, 0, BB, held(BB))


def test_second_retailer_under_target_blocked_does_not_repeat_its_offer():
    # Best Buy $1,400 and Amazon $1,450 both qualify; Best Buy's sale ends while Amazon
    # is blocked for two runs, then Amazon answers with the same $1,450.
    scans = [(140_000, BB, ALL, {BB, AZ})] + [(160_000, BB, BOTH, set())] * 2 + [(145_000, AZ, ALL, {AZ})]
    kinds, state = _run_scans(T, scans)
    assert kinds == [CROSSED, None, None, None]
    assert state.holding_retailers == held(AZ) and not state.armed


def test_deal_that_moved_without_a_ping_holds_the_alert():
    # The deal moves to Amazon at $1,480 (not a further drop, so no ping), then Amazon is blocked.
    scans = [(140_000, BB, ALL, {BB}), (148_000, AZ, ALL, {AZ})]
    scans += [(160_000, BB, BOTH, set())] * 2 + [(148_000, AZ, ALL, {AZ})]
    kinds, _ = _run_scans(T, scans)
    assert kinds == [CROSSED, None, None, None, None]


def test_rearms_once_every_holder_was_seen_above_target():
    scans = [(140_000, BB, ALL, {BB, AZ}), (160_000, BB, BOTH, set()), (160_000, NE, frozenset({AZ, NE}), set())]
    scans += [(160_000, BB, ALL, set()), (140_000, BB, ALL, {BB})]
    kinds, _ = _run_scans(T, scans)
    # Amazon leaves the set in the third scan, which is the first to count; the fourth re-arms.
    assert kinds == [CROSSED, None, None, None, CROSSED]


def test_holder_that_never_answers_completely_lets_go_in_the_end():
    # Best Buy $1,400 pings with Amazon $1,480 also under target. Amazon's page 2 then keeps
    # failing, so it never answers completely, while Best Buy and Newegg show $1,600.
    scans = [(140_000, BB, ALL, {BB, AZ})] + [(160_000, BB, BOTH, set())] * RELEASE_AFTER
    # Newegg crosses at $1,450, not a further drop from $1,400; the alert has re-armed by then.
    scans += [(145_000, NE, BOTH, {NE})]
    kinds, state = _run_scans(T, scans)
    assert kinds == [CROSSED] + [None] * RELEASE_AFTER + [CROSSED]
    assert state == AlertState(T, False, 145_000, 0, NE, held(NE))


def test_sold_out_everywhere_then_restock_pings_again():
    scans = [(140_000, BB, BOTH)] + [(None, None, BOTH)] * 10 + [(140_000, BB, BOTH)]
    kinds, _ = _run_scans(T, scans)
    assert kinds == [CROSSED] + [None] * 10 + [CROSSED]


def test_failed_scans_with_retailers_do_not_rearm():
    scans = [(140_000, BB, BOTH), (None, None, frozenset()), (None, None, frozenset()), (140_000, BB, BOTH)]
    kinds, _ = _run_scans(T, scans)
    assert kinds == [CROSSED, None, None, None]
