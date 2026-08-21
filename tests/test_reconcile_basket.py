"""Guard against republishing a basket total that priced the wrong trains.

Regression test for 2026-08-20, when £54 (08:23 out + 20:30 back) reached
Sophie's message as her "cheapest unbooked" — her real total was £70.80.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from daily_run import reconcile_basket  # noqa: E402

OUT = {"dep": "07:36", "arr": "09:34", "price": 43.8}
BACK = {"dep": "18:30", "arr": "20:27", "price": 27.0}
# 43.8 + 27.0 = 70.8


def test_reconciling_basket_is_kept():
    entry = {
        "splitsave": {"available": True, "total": 70.8, "savings_vs_direct": 0},
        "twox_advance_premium": 13.7,
    }
    ss, premium, note = reconcile_basket(entry, OUT, BACK)
    assert ss["total"] == 70.8
    assert premium == 13.7
    assert note is None


def test_wrong_trains_basket_is_discarded():
    """The actual 2026-08-20 failure: cheapest rows priced instead."""
    entry = {
        "splitsave": {"available": True, "total": 54.0, "savings_vs_direct": 16.8},
        "twox_advance_premium": 0.0,
    }
    ss, premium, note = reconcile_basket(entry, OUT, BACK)
    assert ss["total"] is None, "phantom total must not survive"
    assert ss["available"] is None
    assert premium is None, "premium from a wrong-trains basket is meaningless"
    assert note and "54.00" in note and "70.80" in note


def test_premium_dropped_when_basket_total_absent():
    """Can't reconcile means can't trust — the premium goes too."""
    entry = {
        "splitsave": {"available": None, "total": None, "savings_vs_direct": None},
        "twox_advance_premium": 9.9,
    }
    ss, premium, note = reconcile_basket(entry, OUT, BACK)
    assert premium is None
    assert note and "unverifiable" in note


def test_clean_miss_is_silent():
    """No basket and no premium is the normal non-blocking skip — no noise."""
    entry = {}
    ss, premium, note = reconcile_basket(entry, OUT, BACK)
    assert ss["total"] is None
    assert premium is None
    assert note is None


def test_float_noise_does_not_trigger_a_false_discard():
    entry = {
        "splitsave": {"available": True, "total": 70.80000000001},
        "twox_advance_premium": 5.0,
    }
    ss, premium, note = reconcile_basket(entry, OUT, BACK)
    assert note is None
    assert premium == 5.0


def main() -> int:
    test_reconciling_basket_is_kept()
    test_wrong_trains_basket_is_discarded()
    test_premium_dropped_when_basket_total_absent()
    test_clean_miss_is_silent()
    test_float_noise_does_not_trigger_a_false_discard()
    print("ALL BASKET-RECONCILIATION TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
