import pytest
from bot.utils.curves import LinearCurve, QuadraticCurve, ExponentialCurve

def test_linear_curve():
    curve = LinearCurve()
    # level 1: 0 XP
    # level 2: base * 1 * multiplier = 100 * 1 * 1 = 100 XP
    # level 3: base * (3 * 2)/2 * multiplier = 300 XP
    assert curve.cumulative_xp_for_level(1, 100, 1.0) == 0
    assert curve.cumulative_xp_for_level(2, 100, 1.0) == 100
    assert curve.cumulative_xp_for_level(3, 100, 1.0) == 300
    
    assert curve.level_for_xp(0, 100, 1.0, 100) == 1
    assert curve.level_for_xp(99, 100, 1.0, 100) == 1
    assert curve.level_for_xp(100, 100, 1.0, 100) == 2
    assert curve.level_for_xp(299, 100, 1.0, 100) == 2
    assert curve.level_for_xp(300, 100, 1.0, 100) == 3
    assert curve.level_for_xp(1000, 100, 1.0, 100) == 5 # 100 * 5 * 4 / 2 = 1000 XP

def test_quadratic_curve():
    curve = QuadraticCurve()
    # level 1: 0 XP
    # level 2: 1 * 2 * 3 / 6 * 100 = 100 XP
    # level 3: 2 * 3 * 5 / 6 * 100 = 500 XP
    # level 4: 3 * 4 * 7 / 6 * 100 = 1400 XP
    assert curve.cumulative_xp_for_level(1, 100, 1.0) == 0
    assert curve.cumulative_xp_for_level(2, 100, 1.0) == 100
    assert curve.cumulative_xp_for_level(3, 100, 1.0) == 500
    assert curve.cumulative_xp_for_level(4, 100, 1.0) == 1400
    
    assert curve.level_for_xp(0, 100, 1.0, 100) == 1
    assert curve.level_for_xp(99, 100, 1.0, 100) == 1
    assert curve.level_for_xp(100, 100, 1.0, 100) == 2
    assert curve.level_for_xp(499, 100, 1.0, 100) == 2
    assert curve.level_for_xp(500, 100, 1.0, 100) == 3
    assert curve.level_for_xp(1399, 100, 1.0, 100) == 3
    assert curve.level_for_xp(1400, 100, 1.0, 100) == 4

def test_exponential_curve():
    curve = ExponentialCurve(growth_rate=2.0)
    # level 1: 0 XP
    # level 2: base * (2^(2-1) - 1)/(2-1) * mult = 100 * 1 / 1 = 100 XP
    # level 3: base * (2^(3-1) - 1)/(2-1) * mult = 100 * 3 / 1 = 300 XP
    # level 4: base * (2^(4-1) - 1)/(2-1) * mult = 100 * 7 / 1 = 700 XP
    assert curve.cumulative_xp_for_level(1, 100, 1.0) == 0
    assert curve.cumulative_xp_for_level(2, 100, 1.0) == 100
    assert curve.cumulative_xp_for_level(3, 100, 1.0) == 300
    assert curve.cumulative_xp_for_level(4, 100, 1.0) == 700
    
    assert curve.level_for_xp(0, 100, 1.0, 100) == 1
    assert curve.level_for_xp(99, 100, 1.0, 100) == 1
    assert curve.level_for_xp(100, 100, 1.0, 100) == 2
    assert curve.level_for_xp(299, 100, 1.0, 100) == 2
    assert curve.level_for_xp(300, 100, 1.0, 100) == 3
    assert curve.level_for_xp(699, 100, 1.0, 100) == 3
    assert curve.level_for_xp(700, 100, 1.0, 100) == 4
