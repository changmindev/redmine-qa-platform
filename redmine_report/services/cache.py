"""TTL + 최대 크기 제한이 있는 아주 작은 인메모리 캐시.

원본 코드의 전역 dict 캐시는 프로세스가 살아있는 동안 무한히 커졌고
데이터가 갱신돼도 만료되지 않았다. 여기서는 TTL과 LRU 상한을 둔다.
스레드 안전(Flask 개발 서버는 스레드로 요청을 처리한다).
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any


class TTLCache:
    def __init__(self, ttl_seconds: int = 600, max_entries: int = 64):
        self.ttl = max(0, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Any, default: Any = None) -> Any:
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return default
            expires_at, value = hit
            if expires_at < now:
                self._data.pop(key, None)
                return default
            self._data.move_to_end(key)
            return value

    def set(self, key: Any, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + self.ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def get_or_set(self, key: Any, factory: Callable[[], Any]) -> Any:
        """캐시에 없으면 factory() 결과를 저장 후 반환.

        factory 호출은 락 밖에서 수행한다(느린 HTTP 호출로 다른 요청을 막지 않기 위해).
        같은 키를 동시에 요청하면 두 번 계산될 수 있으나, 결과는 동일하므로 문제되지 않는다.
        """
        sentinel = object()
        cached = self.get(key, sentinel)
        if cached is not sentinel:
            return cached
        value = factory()
        self.set(key, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
