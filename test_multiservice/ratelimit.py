"""限流模块 — Python 实现。

基于令牌桶算法的限流器。
"""

import time


class RateLimiter:
    """令牌桶限流器。

    每个用户每秒最多 10 个请求。
    """

    def __init__(self, capacity: int = 10, refill_rate: float = 10.0):
        self.capacity = capacity
        self.refill_rate = refill_rate
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, user_id: str) -> bool:
        """检查是否允许请求。"""
        now = time.time()
        
        if user_id not in self._buckets:
            self._buckets[user_id] = (float(self.capacity), now)
        
        tokens, last_refill = self._buckets[user_id]
        
        # 补充令牌
        elapsed = now - last_refill
        tokens = min(self.capacity, tokens + elapsed * self.refill_rate)
        
        if tokens >= 1.0:
            tokens -= 1.0
            self._buckets[user_id] = (tokens, now)
            return True
        
        self._buckets[user_id] = (tokens, now)
        return False

    def reset(self, user_id: str | None = None) -> None:
        """重置限流桶。"""
        if user_id:
            self._buckets.pop(user_id, None)
        else:
            self._buckets.clear()
