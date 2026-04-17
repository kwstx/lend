import time
from typing import Dict, Tuple
from fastapi import Request, HTTPException, status

class RateLimiter:
    """
    Production-grade rate limiting would use Redis.
    This implementation uses an in-memory sliding window for demonstration.
    """
    def __init__(self, requests_per_minute: int = 60):
        self.requests_per_minute = requests_per_minute
        # item -> (count, reset_time)
        self.buckets: Dict[str, Tuple[int, float]] = {}

    async def check(self, identifier: str):
        now = time.time()
        if identifier in self.buckets:
            count, reset_time = self.buckets[identifier]
            if now < reset_time:
                if count >= self.requests_per_minute:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail="Rate limit exceeded. Try again later."
                    )
                self.buckets[identifier] = (count + 1, reset_time)
            else:
                self.buckets[identifier] = (1, now + 60)
        else:
            self.buckets[identifier] = (1, now + 60)

# Global instances for different tiers
standard_limiter = RateLimiter(requests_per_minute=100)
admin_limiter = RateLimiter(requests_per_minute=200)
webhook_limiter = RateLimiter(requests_per_minute=500)

async def rate_limit_tenant(request: Request):
    # In a real app, extract tenant ID after auth
    # For now, we use IP or a header if auth hasn't happened yet
    # But usually, this runs after auth in the dependency chain
    tenant_id = request.headers.get("X-Customer-Id", "anonymous")
    await standard_limiter.check(f"tenant_{tenant_id}")

async def rate_limit_admin(request: Request):
    # Use admin email/ID from auth
    await admin_limiter.check("admin_global") # Simplified
