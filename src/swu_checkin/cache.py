"""Small, private Token cache used by the SWU login facade.

The cache intentionally owns only persistence concerns.  Token validation stays
in :mod:`swu_checkin.api.school`: callers must validate a candidate before calling
``save_cached_token``.  File writes are atomic and use restrictive permissions
because the cache contains bearer credentials.
"""

import json
import os
import threading

from .atomic_io import atomic_write_text

_token_cache_lock = threading.Lock()


def load_cached_token(username, cache_path):
    """Return the cached token for *username*, or ``None`` when unavailable."""
    with _token_cache_lock:
        try:
            with open(cache_path, encoding="utf-8") as handle:
                cache = json.load(handle)
            if not isinstance(cache, dict):
                return None
            token = cache.get(username)
            return token if isinstance(token, str) and token else None
        except (OSError, ValueError, TypeError):
            return None


def save_cached_token(username, token, cache_path):
    """Merge *token* into *cache_path* with an atomic private write."""
    if not isinstance(token, str) or not token:
        raise ValueError("不能缓存空 Token")
    with _token_cache_lock:
        absolute_path = os.path.abspath(cache_path)

        cache = {}
        try:
            with open(absolute_path, encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                cache = loaded
        except (OSError, ValueError, TypeError):
            pass
        cache[str(username)] = token
        atomic_write_text(
            absolute_path,
            json.dumps(cache, ensure_ascii=False, indent=2) + "\n",
            mode=0o600,
            prefix=".token-cache-",
        )


# Internal aliases make the module pleasant to use from older integrations
# that imported the implementation names directly.
_load_cached_token = load_cached_token
_save_cached_token = save_cached_token


__all__ = ["load_cached_token", "save_cached_token"]
