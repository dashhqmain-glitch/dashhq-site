"""Tests for the Alert Tracker's S/A/B/C grade - direct request: grade every
call by (1) the quality of the wallets converging and (2) real floor/price
action, not the general NFT Scope score (which answers a different question -
"is this collection legitimate" - via signals like socials/description/
verified badge that have nothing to do with wallet quality or price action).

Deliberately reuses data already computed elsewhere rather than deriving
anything new: track_records (a wallet's REAL resolved win rate, already
gated behind _NFT_SCOPE_SMART_WALLET_MIN_SAMPLE/_MIN_WIN_RATE) and
score["floor_multiple"]/score["reasons"] (the same real, proven
appreciation-since-first-tracked and rapid-activity signals every other post
already trusts).
"""
import main


def _score(**overrides):
    data = {"tier": "red", "floor_multiple": None, "reasons": []}
    data.update(overrides)
    return data


def test_grade_floor_is_c_for_a_single_unproven_wallet_with_no_price_action():
    grade, points, reasons = main._alert_tracker_grade({"0xa"}, {}, _score())
    assert grade == "C"
    assert points == main._ALERT_TRACKER_GRADE_POINTS_PER_WALLET  # 15 - just the one-wallet count, nothing else fired
    assert reasons == ["1 tracked wallet(s) converging"]


def test_grade_wallet_count_points_scale_then_cap_at_three_wallets():
    g1, p1, _ = main._alert_tracker_grade({"0xa"}, {}, _score())
    g2, p2, _ = main._alert_tracker_grade({"0xa", "0xb"}, {}, _score())
    g3, p3, _ = main._alert_tracker_grade({"0xa", "0xb", "0xc"}, {}, _score())
    g4, p4, _ = main._alert_tracker_grade({"0xa", "0xb", "0xc", "0xd"}, {}, _score())
    assert p1 == 15 and p2 == 30 and p3 == 45
    assert p4 == 45  # a 4th converging wallet is real and still shown elsewhere, just doesn't add further grade points
    assert p3 == main._ALERT_TRACKER_GRADE_MAX_WALLET_COUNT_POINTS


def test_grade_rewards_one_proven_wallet_then_a_further_bonus_for_a_second():
    no_proof, _, _ = main._alert_tracker_grade({"0xa"}, {}, _score())
    tr = {"0xa": {"win_rate": 0.8, "sample": 10}}
    one_proven, points_one, reasons_one = main._alert_tracker_grade({"0xa"}, tr, _score())
    tr2 = {"0xa": {"win_rate": 0.8, "sample": 10}, "0xb": {"win_rate": 0.75, "sample": 6}}
    two_proven, points_two, reasons_two = main._alert_tracker_grade({"0xa", "0xb"}, tr2, _score())

    assert points_one == 15 + main._ALERT_TRACKER_GRADE_ONE_PROVEN_WALLET_POINTS  # 15 + 10 = 25
    assert "1 with a proven track record" in reasons_one[0]
    # 2 wallets (30) + one-proven bonus (10) + two-proven bonus (5) = 45
    assert points_two == 30 + main._ALERT_TRACKER_GRADE_ONE_PROVEN_WALLET_POINTS + main._ALERT_TRACKER_GRADE_TWO_PROVEN_WALLETS_BONUS
    assert "2 with a proven track record" in reasons_two[0]


def test_grade_only_counts_track_records_for_wallets_actually_converging():
    # A wallet with a proven history that ISN'T part of this convergence
    # must never inflate this call's grade.
    tr = {"0xa": {"win_rate": 0.9, "sample": 20}, "0xUNRELATED": {"win_rate": 0.99, "sample": 50}}
    _, points, reasons = main._alert_tracker_grade({"0xa"}, tr, _score())
    assert points == 15 + main._ALERT_TRACKER_GRADE_ONE_PROVEN_WALLET_POINTS
    assert "1 with a proven track record" in reasons[0]


def test_grade_floor_multiple_tiers_are_mutually_exclusive_and_scale_with_real_appreciation():
    _, p_none, r_none = main._alert_tracker_grade({"0xa"}, {}, _score(floor_multiple=1.1))
    _, p_low, r_low = main._alert_tracker_grade({"0xa"}, {}, _score(floor_multiple=1.2))
    _, p_mid, r_mid = main._alert_tracker_grade({"0xa"}, {}, _score(floor_multiple=1.5))
    _, p_high, r_high = main._alert_tracker_grade({"0xa"}, {}, _score(floor_multiple=2.0))
    base = main._ALERT_TRACKER_GRADE_POINTS_PER_WALLET
    assert p_none == base  # under 1.2x - no bonus at all, not even a reason line
    assert len(r_none) == 1
    assert p_low == base + main._ALERT_TRACKER_GRADE_FLOOR_1_2X_POINTS
    assert p_mid == base + main._ALERT_TRACKER_GRADE_FLOOR_1_5X_POINTS
    assert p_high == base + main._ALERT_TRACKER_GRADE_FLOOR_2X_POINTS
    assert any("1.2x" in r for r in r_low)
    assert any("1.5x" in r for r in r_mid)
    assert any("2.0x" in r for r in r_high)


def test_grade_treats_a_missing_or_zero_floor_multiple_as_no_price_action():
    _, p_none, _ = main._alert_tracker_grade({"0xa"}, {}, _score(floor_multiple=None))
    _, p_zero, _ = main._alert_tracker_grade({"0xa"}, {}, _score(floor_multiple=0))
    assert p_none == p_zero == main._ALERT_TRACKER_GRADE_POINTS_PER_WALLET


def test_grade_momentum_reasons_only_award_the_single_strongest_signal_present():
    # A cycle can only ever have ONE of these actually fire per _nft_scope_score's
    # own if/elif chain, but the grade must not double-count even if it did.
    _, p_sharp, r_sharp = main._alert_tracker_grade({"0xa"}, {}, _score(reasons=["🚀 rapid", "💥 surge", "🔥 sharp"]))
    _, p_surge, r_surge = main._alert_tracker_grade({"0xa"}, {}, _score(reasons=["🚀 rapid", "💥 surge"]))
    _, p_rapid, r_rapid = main._alert_tracker_grade({"0xa"}, {}, _score(reasons=["🚀 rapid"]))
    base = main._ALERT_TRACKER_GRADE_POINTS_PER_WALLET
    assert p_sharp == base + main._ALERT_TRACKER_GRADE_SHARP_MOMENTUM_POINTS
    assert p_surge == base + main._ALERT_TRACKER_GRADE_SURGE_MOMENTUM_POINTS
    assert p_rapid == base + main._ALERT_TRACKER_GRADE_RAPID_MOMENTUM_POINTS
    assert sum(1 for r in r_sharp if "momentum" in r.lower() or "surging" in r.lower() or "accelerating" in r.lower()) == 1
    assert "🔥" in r_sharp[-1]
    assert "💥" in r_surge[-1]
    assert "🚀" in r_rapid[-1]


def test_grade_ignores_a_momentum_emoji_only_when_it_is_not_actually_present():
    _, points, reasons = main._alert_tracker_grade({"0xa"}, {}, _score(reasons=["Some unrelated static reason"]))
    assert points == main._ALERT_TRACKER_GRADE_POINTS_PER_WALLET
    assert len(reasons) == 1


def test_grade_handles_a_score_with_no_reasons_key_at_all():
    _, points, _ = main._alert_tracker_grade({"0xa"}, {}, {"floor_multiple": None})
    assert points == main._ALERT_TRACKER_GRADE_POINTS_PER_WALLET


# ── Letter grade thresholds ───────────────────────────────────────────────

def test_grade_letter_boundaries_are_correct_and_inclusive_at_each_threshold():
    assert main._alert_tracker_grade_letter_for_points(main._ALERT_TRACKER_GRADE_S_THRESHOLD) == "S"
    assert main._alert_tracker_grade_letter_for_points(main._ALERT_TRACKER_GRADE_S_THRESHOLD - 1) == "A"
    assert main._alert_tracker_grade_letter_for_points(main._ALERT_TRACKER_GRADE_A_THRESHOLD) == "A"
    assert main._alert_tracker_grade_letter_for_points(main._ALERT_TRACKER_GRADE_A_THRESHOLD - 1) == "B"
    assert main._alert_tracker_grade_letter_for_points(main._ALERT_TRACKER_GRADE_B_THRESHOLD) == "B"
    assert main._alert_tracker_grade_letter_for_points(main._ALERT_TRACKER_GRADE_B_THRESHOLD - 1) == "C"
    assert main._alert_tracker_grade_letter_for_points(0) == "C"


def test_grade_reaches_s_tier_with_maximum_wallet_quality_and_price_action():
    tr = {"0xa": {"win_rate": 0.9, "sample": 20}, "0xb": {"win_rate": 0.8, "sample": 10}}
    grade, points, reasons = main._alert_tracker_grade(
        {"0xa", "0xb", "0xc"}, tr, _score(floor_multiple=2.5, reasons=["🔥 sharp burst"]),
    )
    assert points == main._ALERT_TRACKER_GRADE_MAX_WALLET_POINTS + main._ALERT_TRACKER_GRADE_MAX_ACTION_POINTS == 100
    assert grade == "S"
    assert len(reasons) == 3  # wallet line, floor line, momentum line - all three axes fired


def test_grade_points_never_exceed_one_hundred_even_if_every_axis_maxes_out():
    tr = {f"0x{i}": {"win_rate": 0.9, "sample": 20} for i in range(5)}
    _, points, _ = main._alert_tracker_grade({f"0x{i}" for i in range(5)}, tr, _score(floor_multiple=10.0, reasons=["🔥"]))
    assert points == 100


def test_grade_emoji_map_covers_every_letter():
    for letter in ("S", "A", "B", "C"):
        assert letter in main._ALERT_TRACKER_GRADE_EMOJI
        assert main._ALERT_TRACKER_GRADE_EMOJI[letter]  # non-empty
