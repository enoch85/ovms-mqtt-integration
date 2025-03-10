"""Rate limiter utility for OVMS integration."""
import time
from typing import List


class CommandRateLimiter:
    """Rate limiter for OVMS commands.

    Prevents sending too many commands in a short period to avoid overwhelming the OVMS module.
    """

    def __init__(self, max_calls: int = 5, period: float = 60.0):
        """Initialize the rate limiter.

        Args:
            max_calls: Maximum number of calls allowed per period
            period: Time period in seconds
        """
        self.calls: List[float] = []
        self.max_calls = max_calls
        self.period = period

    def can_call(self) -> bool:
        """Check if a call can be made without exceeding the rate limit.

        Returns:
            True if call is allowed, False if limit would be exceeded
        """
        now = time.time()

        # Remove calls older than the period
        self.calls = [t for t in self.calls if now - t < self.period]

        # Check if we've hit the limit
        if len(self.calls) >= self.max_calls:
            return False

        # Add this call
        self.calls.append(now)
        return True

    def calls_remaining(self) -> int:
        """Get the number of calls remaining in the current period.

        Returns:
            Number of calls remaining
        """
        now = time.time()

        # Remove calls older than the period
        self.calls = [t for t in self.calls if now - t < self.period]

        return max(0, self.max_calls - len(self.calls))

    def time_to_next_call(self) -> float:
        """Get the time in seconds until the next call is allowed.

        Returns:
            Seconds until next call is allowed, or 0 if calls are available now
        """
        now = time.time()

        # If we have calls available, return 0
        if len(self.calls) < self.max_calls:
            return 0.0

        # Find the oldest call and calculate when it will expire
        oldest_call = min(self.calls)
        return max(0.0, (oldest_call + self.period) - now)
