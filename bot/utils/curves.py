from abc import ABC, abstractmethod

class LevelCurve(ABC):
    @abstractmethod
    def cumulative_xp_for_level(self, level: int, base_xp: float, multiplier: float) -> int:
        """Returns the cumulative XP required to reach the start of `level`."""
        pass

    def level_for_xp(self, xp: int, base_xp: float, multiplier: float, max_level: int) -> int:
        """
        Uses binary search to find the highest level L such that cumulative_xp_for_level(L) <= xp.
        Guarantees O(log(max_level)) lookup time.
        """
        if xp <= 0:
            return 1
        
        low = 1
        high = max_level
        ans = 1
        
        while low <= high:
            mid = (low + high) // 2
            required = self.cumulative_xp_for_level(mid, base_xp, multiplier)
            if required <= xp:
                ans = mid
                low = mid + 1
            else:
                high = mid - 1
                
        return ans

class LinearCurve(LevelCurve):
    def cumulative_xp_for_level(self, level: int, base_xp: float, multiplier: float) -> int:
        if level <= 1:
            return 0
        # XP required for level L = base_xp * L * multiplier
        # Cumulative Sum_{i=1}^{L-1} (base_xp * i * multiplier) = base_xp * (L * (L - 1)) / 2 * multiplier
        return int(base_xp * (level * (level - 1)) / 2 * multiplier)

class QuadraticCurve(LevelCurve):
    def cumulative_xp_for_level(self, level: int, base_xp: float, multiplier: float) -> int:
        if level <= 1:
            return 0
        # XP required for level L = base_xp * L^2 * multiplier
        # Cumulative Sum_{i=1}^{L-1} (base_xp * i^2 * multiplier) = base_xp * (L - 1) * L * (2*L - 1) / 6 * multiplier
        return int(base_xp * ((level - 1) * level * (2 * level - 1)) / 6 * multiplier)

class ExponentialCurve(LevelCurve):
    def __init__(self, growth_rate: float = 1.5):
        self.growth_rate = growth_rate

    def cumulative_xp_for_level(self, level: int, base_xp: float, multiplier: float) -> int:
        if level <= 1:
            return 0
        # XP required for level L = base_xp * (growth_rate^L) * multiplier
        # Cumulative = base_xp * (growth_rate^(L-1) - 1) / (growth_rate - 1) * multiplier
        numerator = (self.growth_rate ** (level - 1)) - 1
        denominator = self.growth_rate - 1
        return int(base_xp * (numerator / denominator) * multiplier)

# Curve registry
CURVES: dict[str, LevelCurve] = {
    "linear": LinearCurve(),
    "quadratic": QuadraticCurve(),
    "exponential": ExponentialCurve(),
}

def get_curve(name: str) -> LevelCurve:
    """Retrieves a level curve instance by name. Case-insensitive."""
    curve = CURVES.get(name.lower())
    if not curve:
        raise ValueError(f"Unknown level curve type: {name}. Available: {list(CURVES.keys())}")
    return curve
