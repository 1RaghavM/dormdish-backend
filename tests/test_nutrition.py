from app import calculate_bmr_tdee


def test_calculate_bmr_tdee_matches_calculator_net_example():
    metrics = calculate_bmr_tdee(
        age=18,
        height_feet=5,
        height_inches=8,
        weight_lbs=175,
        gender="male",
        activity_level="moderately_active",
    )

    assert metrics["bmr"] == 1788
    assert metrics["tdee"] == 2772

    activity_breakdown = metrics["activity_breakdown"]
    assert activity_breakdown["sedentary"]["calories"] == 2146
    assert activity_breakdown["lightly_active"]["calories"] == 2459
    assert activity_breakdown["moderately_active"]["calories"] == 2772
    assert activity_breakdown["very_active"]["calories"] == 3085
    assert activity_breakdown["extremely_active"]["calories"] == 3398


def test_calculate_bmr_tdee_handles_prefer_not_to_say_gender():
    metrics = calculate_bmr_tdee(
        age=25,
        height_feet=6,
        height_inches=0,
        weight_lbs=200,
        gender="prefer_not_to_say",
        activity_level="sedentary",
    )

    # Ensure the calculation completes and returns the expected keys
    assert isinstance(metrics["bmr"], int)
    assert isinstance(metrics["tdee"], int)
    assert "activity_breakdown" in metrics and metrics["activity_breakdown"]


def test_bmr_increases_with_weight():
    lighter = calculate_bmr_tdee(
        age=30,
        height_feet=5,
        height_inches=10,
        weight_lbs=160,
        gender="female",
        activity_level="moderately_active",
    )

    heavier = calculate_bmr_tdee(
        age=30,
        height_feet=5,
        height_inches=10,
        weight_lbs=185,
        gender="female",
        activity_level="moderately_active",
    )

    assert heavier["bmr"] > lighter["bmr"]
    assert heavier["tdee"] > lighter["tdee"]
    assert (
        heavier["activity_breakdown"]["moderately_active"]["calories"]
        > lighter["activity_breakdown"]["moderately_active"]["calories"]
    )


def test_bmr_increases_with_height():
    shorter = calculate_bmr_tdee(
        age=22,
        height_feet=5,
        height_inches=6,
        weight_lbs=180,
        gender="male",
        activity_level="lightly_active",
    )

    taller = calculate_bmr_tdee(
        age=22,
        height_feet=6,
        height_inches=2,
        weight_lbs=180,
        gender="male",
        activity_level="lightly_active",
    )

    assert taller["bmr"] > shorter["bmr"]
    assert taller["tdee"] > shorter["tdee"]
    assert (
        taller["activity_breakdown"]["lightly_active"]["calories"]
        > shorter["activity_breakdown"]["lightly_active"]["calories"]
    )


def test_activity_level_scaling_matches_multiplier():
    metrics = calculate_bmr_tdee(
        age=28,
        height_feet=5,
        height_inches=9,
        weight_lbs=170,
        gender="male",
        activity_level="very_active",
    )

    bmr = metrics["bmr"]

    for key, breakdown in metrics["activity_breakdown"].items():
        multiplier = breakdown["multiplier"]
        expected = int(round(metrics["bmr_precise"] * multiplier))
        assert breakdown["calories"] == expected, f"Mismatch for {key}"

    assert metrics["activity_breakdown"]["super_active"]["calories"] > metrics["activity_breakdown"]["extremely_active"]["calories"]
