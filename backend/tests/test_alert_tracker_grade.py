"""Tests for the Alert Tracker's S/A/B/C grade - direct request: grade every
call by the quality of the wallets converging, real price momentum, the
floor's actual dollar value, and the type of project being minted - not the
general NFT Scope score (which answers a different question, "is this
collection legitimate", via signals like supply sanity that have nothing to
do with any of the above).

Four axes, 100 points total: Wallet Quality (45, the heaviest - this IS the
Alert Tracker, WHO is buying is the primary signal), Price Momentum (20),
Floor Value (20, priced in USD so every chain grades on the same real
scale), Project Quality (15, the TYPE of project - verified/category/
socials, independent of who's currently buying it).

Deliberately reuses data already computed elsewhere rather than deriving
anything new: track_records (a wallet's REAL resolved win rate, already
gated behind _NFT_SCOPE_SMART_WALLET_MIN_SAMPLE/_MIN_WIN_RATE),
score["floor_multiple"]/score["reasons"] (the same real, proven
appreciation-since-first-tracked and rapid-activity signals every other
post already trusts), and c["floorUsd"]/c["verified"]/c["category"] (the
same OpenSea collection data every embed already has in hand).
"""
import main


def _score(**overrides):
    data = {"tier": "red", "floor_multiple": None, "reasons": []}
    data.update(overrides)
    return data


def _collection(**overrides):
    # No floorUsd/verified/category/socials by default - a bare-minimum
    # collection dict, same shape a real caller could genuinely hand in
    # when OpenSea doesn't have that data yet.
    data = {"name": "Test Collection", "floorUsd": None, "verified": False, "category": None}
    data.update(overrides)
    return data


def test_grade_floor_is_c_for_a_single_unproven_wallet_with_nothing_else_firing():
    grade, points, reasons = main._alert_tracker_grade({"0xa"}, {}, _score(), _collection())
    assert grade == "C"
    assert points == main._ALERT_TRACKER_GRADE_POINTS_PER_WALLET  # 12 - just the one-wallet count, nothing else fired
    assert reasons == ["1 tracked wallet(s) converging"]


# ── Wallet Quality (max 45) ────────────────────────────────────────────────

def test_grade_wallet_count_points_scale_then_cap_at_three_wallets():
    p = [main._alert_tracker_grade({f"0x{i}" for i in range(n)}, {}, _score(), _collection())[1] for n in (1, 2, 3, 4)]
    per = main._ALERT_TRACKER_GRADE_POINTS_PER_WALLET
    assert p == [per, per * 2, per * 3, per * 3]  # a 4th converging wallet adds nothing further
    assert p[2] == main._ALERT_TRACKER_GRADE_MAX_WALLET_COUNT_POINTS


def test_grade_rewards_one_proven_wallet_then_a_further_bonus_for_a_second():
    tr1 = {"0xa": {"win_rate": 0.8, "sample": 10}}
    _, points_one, reasons_one = main._alert_tracker_grade({"0xa"}, tr1, _score(), _collection())
    tr2 = {"0xa": {"win_rate": 0.8, "sample": 10}, "0xb": {"win_rate": 0.75, "sample": 6}}
    _, points_two, reasons_two = main._alert_tracker_grade({"0xa", "0xb"}, tr2, _score(), _collection())

    per = main._ALERT_TRACKER_GRADE_POINTS_PER_WALLET
    assert points_one == per + main._ALERT_TRACKER_GRADE_ONE_PROVEN_WALLET_POINTS
    assert "1 with a proven track record" in reasons_one[0]
    assert points_two == per * 2 + main._ALERT_TRACKER_GRADE_ONE_PROVEN_WALLET_POINTS + main._ALERT_TRACKER_GRADE_TWO_PROVEN_WALLETS_BONUS
    assert "2 with a proven track record" in reasons_two[0]


def test_grade_only_counts_track_records_for_wallets_actually_converging():
    # A wallet with a proven history that ISN'T part of this convergence
    # must never inflate this call's grade.
    tr = {"0xa": {"win_rate": 0.9, "sample": 20}, "0xUNRELATED": {"win_rate": 0.99, "sample": 50}}
    _, points, reasons = main._alert_tracker_grade({"0xa"}, tr, _score(), _collection())
    assert points == main._ALERT_TRACKER_GRADE_POINTS_PER_WALLET + main._ALERT_TRACKER_GRADE_ONE_PROVEN_WALLET_POINTS
    assert "1 with a proven track record" in reasons[0]


def test_grade_wallet_points_never_exceed_their_own_axis_cap():
    tr = {f"0x{i}": {"win_rate": 0.9, "sample": 20} for i in range(5)}
    _, points, _ = main._alert_tracker_grade({f"0x{i}" for i in range(5)}, tr, _score(), _collection())
    base = main._ALERT_TRACKER_GRADE_MAX_WALLET_COUNT_POINTS + main._ALERT_TRACKER_GRADE_ONE_PROVEN_WALLET_POINTS + main._ALERT_TRACKER_GRADE_TWO_PROVEN_WALLETS_BONUS
    assert base == main._ALERT_TRACKER_GRADE_MAX_WALLET_POINTS  # the constants themselves must sum exactly to the axis cap
    assert points == main._ALERT_TRACKER_GRADE_MAX_WALLET_POINTS  # nothing else fired in this score/collection


# ── Price Momentum (max 20) ────────────────────────────────────────────────

def test_grade_floor_multiple_tiers_are_mutually_exclusive_and_scale_with_real_appreciation():
    base = main._ALERT_TRACKER_GRADE_POINTS_PER_WALLET
    _, p_none, r_none = main._alert_tracker_grade({"0xa"}, {}, _score(floor_multiple=1.1), _collection())
    _, p_low, r_low = main._alert_tracker_grade({"0xa"}, {}, _score(floor_multiple=1.2), _collection())
    _, p_mid, r_mid = main._alert_tracker_grade({"0xa"}, {}, _score(floor_multiple=1.5), _collection())
    _, p_high, r_high = main._alert_tracker_grade({"0xa"}, {}, _score(floor_multiple=2.0), _collection())
    assert p_none == base and len(r_none) == 1  # under 1.2x - no bonus, no reason line
    assert p_low == base + main._ALERT_TRACKER_GRADE_FLOOR_1_2X_POINTS
    assert p_mid == base + main._ALERT_TRACKER_GRADE_FLOOR_1_5X_POINTS
    assert p_high == base + main._ALERT_TRACKER_GRADE_FLOOR_2X_POINTS
    assert any("1.2x" in r for r in r_low)
    assert any("1.5x" in r for r in r_mid)
    assert any("2.0x" in r for r in r_high)


def test_grade_treats_a_missing_or_zero_floor_multiple_as_no_momentum():
    _, p_none, _ = main._alert_tracker_grade({"0xa"}, {}, _score(floor_multiple=None), _collection())
    _, p_zero, _ = main._alert_tracker_grade({"0xa"}, {}, _score(floor_multiple=0), _collection())
    assert p_none == p_zero == main._ALERT_TRACKER_GRADE_POINTS_PER_WALLET


def test_grade_momentum_reasons_only_award_the_single_strongest_signal_present():
    base = main._ALERT_TRACKER_GRADE_POINTS_PER_WALLET
    _, p_sharp, r_sharp = main._alert_tracker_grade({"0xa"}, {}, _score(reasons=["🚀 rapid", "💥 surge", "🔥 sharp"]), _collection())
    _, p_surge, r_surge = main._alert_tracker_grade({"0xa"}, {}, _score(reasons=["🚀 rapid", "💥 surge"]), _collection())
    _, p_rapid, r_rapid = main._alert_tracker_grade({"0xa"}, {}, _score(reasons=["🚀 rapid"]), _collection())
    assert p_sharp == base + main._ALERT_TRACKER_GRADE_SHARP_MOMENTUM_POINTS
    assert p_surge == base + main._ALERT_TRACKER_GRADE_SURGE_MOMENTUM_POINTS
    assert p_rapid == base + main._ALERT_TRACKER_GRADE_RAPID_MOMENTUM_POINTS
    assert "🔥" in r_sharp[-1] and "💥" in r_surge[-1] and "🚀" in r_rapid[-1]


def test_grade_ignores_a_momentum_emoji_only_when_it_is_not_actually_present():
    _, points, reasons = main._alert_tracker_grade({"0xa"}, {}, _score(reasons=["Some unrelated static reason"]), _collection())
    assert points == main._ALERT_TRACKER_GRADE_POINTS_PER_WALLET
    assert len(reasons) == 1


def test_grade_handles_a_score_with_no_reasons_key_at_all():
    _, points, _ = main._alert_tracker_grade({"0xa"}, {}, {"floor_multiple": None}, _collection())
    assert points == main._ALERT_TRACKER_GRADE_POINTS_PER_WALLET


def test_grade_momentum_axis_caps_even_if_floor_and_burst_would_stack_past_it():
    per_axis = main._ALERT_TRACKER_GRADE_FLOOR_2X_POINTS + main._ALERT_TRACKER_GRADE_SHARP_MOMENTUM_POINTS
    assert per_axis == main._ALERT_TRACKER_GRADE_MAX_MOMENTUM_POINTS  # the constants sum exactly to the axis cap by design
    _, points, _ = main._alert_tracker_grade({"0xa"}, {}, _score(floor_multiple=2.0, reasons=["🔥"]), _collection())
    assert points == main._ALERT_TRACKER_GRADE_POINTS_PER_WALLET + main._ALERT_TRACKER_GRADE_MAX_MOMENTUM_POINTS


# ── Floor Value (max 20) - real dollar stakes, priced in USD ──────────────

def test_grade_floor_value_tiers_scale_with_real_dollar_stakes():
    base = main._ALERT_TRACKER_GRADE_POINTS_PER_WALLET
    _, p_elite, r_elite = main._alert_tracker_grade({"0xa"}, {}, _score(), _collection(floorUsd=5000))
    _, p_high, r_high = main._alert_tracker_grade({"0xa"}, {}, _score(), _collection(floorUsd=500))
    _, p_mid, r_mid = main._alert_tracker_grade({"0xa"}, {}, _score(), _collection(floorUsd=50))
    _, p_low, r_low = main._alert_tracker_grade({"0xa"}, {}, _score(), _collection(floorUsd=5))
    assert p_elite == base + main._ALERT_TRACKER_GRADE_FLOOR_ELITE_POINTS
    assert p_high == base + main._ALERT_TRACKER_GRADE_FLOOR_HIGH_POINTS
    assert p_mid == base + main._ALERT_TRACKER_GRADE_FLOOR_MID_POINTS
    assert p_low == base + main._ALERT_TRACKER_GRADE_FLOOR_LOW_POINTS
    assert "Elite floor" in r_elite[-1] and "5,000" in r_elite[-1]
    assert "High-value floor" in r_high[-1]
    assert "Solid floor" in r_mid[-1]
    assert "Modest floor" in r_low[-1]


def test_grade_floor_value_is_zero_below_the_low_tier_or_when_unknown():
    base = main._ALERT_TRACKER_GRADE_POINTS_PER_WALLET
    # Confirmed live: "Aura", a real Alert Tracker call with 28 converging
    # wallets, had a $0.91 floor - real wallet interest, but a
    # fundamentally lower-stakes situation than the same wallet count on a
    # premium floor. This must add nothing here, not even a reason line.
    _, p_cheap, r_cheap = main._alert_tracker_grade({"0xa"}, {}, _score(), _collection(floorUsd=0.91))
    _, p_unknown, r_unknown = main._alert_tracker_grade({"0xa"}, {}, _score(), _collection(floorUsd=None))
    assert p_cheap == base and len(r_cheap) == 1
    assert p_unknown == base and len(r_unknown) == 1


def test_grade_floor_value_boundary_is_inclusive_at_each_threshold():
    base = main._ALERT_TRACKER_GRADE_POINTS_PER_WALLET
    _, p_at, _ = main._alert_tracker_grade({"0xa"}, {}, _score(), _collection(floorUsd=main._ALERT_TRACKER_GRADE_FLOOR_MID_USD))
    _, p_below, _ = main._alert_tracker_grade({"0xa"}, {}, _score(), _collection(floorUsd=main._ALERT_TRACKER_GRADE_FLOOR_MID_USD - 0.01))
    assert p_at == base + main._ALERT_TRACKER_GRADE_FLOOR_MID_POINTS
    assert p_below == base + main._ALERT_TRACKER_GRADE_FLOOR_LOW_POINTS


# ── Project Quality (max 15) - the TYPE of project ─────────────────────────

def test_grade_project_quality_rewards_verified_category_and_socials_independently():
    base = main._ALERT_TRACKER_GRADE_POINTS_PER_WALLET
    _, p_verified, r_verified = main._alert_tracker_grade({"0xa"}, {}, _score(), _collection(verified=True))
    _, p_category, r_category = main._alert_tracker_grade({"0xa"}, {}, _score(), _collection(category="Art"))
    _, p_socials, r_socials = main._alert_tracker_grade({"0xa"}, {}, _score(), _collection(twitter="proj"))
    assert p_verified == base + main._ALERT_TRACKER_GRADE_VERIFIED_POINTS
    assert p_category == base + main._ALERT_TRACKER_GRADE_CATEGORY_POINTS
    assert p_socials == base + main._ALERT_TRACKER_GRADE_SOCIALS_POINTS
    assert "OpenSea-verified" in r_verified[-1]
    assert "category: Art" in r_category[-1]
    assert "public presence linked" in r_socials[-1]


def test_grade_project_quality_signals_stack_and_socials_check_all_three_fields():
    base = main._ALERT_TRACKER_GRADE_POINTS_PER_WALLET
    for social_field in ("twitter", "discord", "website"):
        _, points, reasons = main._alert_tracker_grade({"0xa"}, {}, _score(), _collection(**{social_field: "x"}))
        assert points == base + main._ALERT_TRACKER_GRADE_SOCIALS_POINTS

    _, points_all, reasons_all = main._alert_tracker_grade(
        {"0xa"}, {}, _score(), _collection(verified=True, category="Gaming", website="https://x.com"),
    )
    expected = main._ALERT_TRACKER_GRADE_VERIFIED_POINTS + main._ALERT_TRACKER_GRADE_CATEGORY_POINTS + main._ALERT_TRACKER_GRADE_SOCIALS_POINTS
    assert expected == main._ALERT_TRACKER_GRADE_MAX_QUALITY_POINTS  # the constants sum exactly to the axis cap
    assert points_all == base + main._ALERT_TRACKER_GRADE_MAX_QUALITY_POINTS
    assert len(reasons_all) == 2  # wallet line + one combined "Project quality: ..." line, not three separate ones


def test_grade_omits_the_project_quality_line_when_nothing_is_on_file():
    _, points, reasons = main._alert_tracker_grade({"0xa"}, {}, _score(), _collection())
    assert points == main._ALERT_TRACKER_GRADE_POINTS_PER_WALLET
    assert len(reasons) == 1
    assert not any("quality" in r.lower() for r in reasons)


# ── Letter grade thresholds ─────────────────────────────────────────────────

def test_grade_letter_boundaries_are_correct_and_inclusive_at_each_threshold():
    assert main._alert_tracker_grade_letter_for_points(main._ALERT_TRACKER_GRADE_S_THRESHOLD) == "S"
    assert main._alert_tracker_grade_letter_for_points(main._ALERT_TRACKER_GRADE_S_THRESHOLD - 1) == "A"
    assert main._alert_tracker_grade_letter_for_points(main._ALERT_TRACKER_GRADE_A_THRESHOLD) == "A"
    assert main._alert_tracker_grade_letter_for_points(main._ALERT_TRACKER_GRADE_A_THRESHOLD - 1) == "B"
    assert main._alert_tracker_grade_letter_for_points(main._ALERT_TRACKER_GRADE_B_THRESHOLD) == "B"
    assert main._alert_tracker_grade_letter_for_points(main._ALERT_TRACKER_GRADE_B_THRESHOLD - 1) == "C"
    assert main._alert_tracker_grade_letter_for_points(0) == "C"


def test_grade_reaches_s_tier_only_with_strong_signal_across_every_axis():
    tr = {"0xa": {"win_rate": 0.9, "sample": 20}, "0xb": {"win_rate": 0.8, "sample": 10}}
    grade, points, reasons = main._alert_tracker_grade(
        {"0xa", "0xb", "0xc"}, tr, _score(floor_multiple=2.5, reasons=["🔥 sharp burst"]),
        _collection(floorUsd=6000, verified=True, category="Art", twitter="proj"),
    )
    assert points == 100  # every axis at its own cap: 45 + 20 + 20 + 15
    assert grade == "S"
    # 5 lines: wallet, floor-multiple, momentum-emoji (momentum's two sub-
    # signals each write their own line), floor value, project quality.
    assert len(reasons) == 5


def test_grade_a_real_28_wallet_cheap_floor_call_does_not_reach_s_or_a_on_wallets_alone():
    # Confirmed live: "Aura" had 28 converging wallets (capped at the
    # 3-wallet ceiling either way) but a $0.91 floor and, via the manual
    # preview path, no momentum data. Wallet count alone must not be
    # enough to reach the top tiers - the whole point of adding the other
    # three axes.
    grade, points, _ = main._alert_tracker_grade({f"0x{i}" for i in range(28)}, {}, _score(), _collection(floorUsd=0.91))
    assert points == main._ALERT_TRACKER_GRADE_MAX_WALLET_COUNT_POINTS == 36
    assert grade == "B"


def test_grade_points_never_exceed_one_hundred_even_if_every_axis_maxes_out():
    tr = {f"0x{i}": {"win_rate": 0.9, "sample": 20} for i in range(10)}
    _, points, _ = main._alert_tracker_grade(
        {f"0x{i}" for i in range(10)}, tr, _score(floor_multiple=100.0, reasons=["🔥"]),
        _collection(floorUsd=10_000_000, verified=True, category="Art", twitter="x", discord="x", website="x"),
    )
    assert points == 100


def test_grade_emoji_map_covers_every_letter():
    for letter in ("S", "A", "B", "C"):
        assert letter in main._ALERT_TRACKER_GRADE_EMOJI
        assert main._ALERT_TRACKER_GRADE_EMOJI[letter]  # non-empty


def test_grade_axis_caps_sum_to_exactly_one_hundred():
    # If these constants ever drift, the S-tier ceiling silently moves too -
    # pin the invariant directly so a future edit can't do that by accident.
    total = (
        main._ALERT_TRACKER_GRADE_MAX_WALLET_POINTS + main._ALERT_TRACKER_GRADE_MAX_MOMENTUM_POINTS
        + main._ALERT_TRACKER_GRADE_MAX_FLOOR_POINTS + main._ALERT_TRACKER_GRADE_MAX_QUALITY_POINTS
    )
    assert total == 100
