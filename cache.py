"""Small, private Token cache used by the SWU login facade.

The cache intentionally owns only persistence concerns.  Token validation stays
in :mod:`school_api`: callers must validate a candidate before calling
``save_cached_token``.  File writes are atomic and use restrictive permissions
because the cache contains bearer credentials.
"""

import json
import os
import tempfile
import threading


_token_cache_lock = threading.Lock()


def load_cached_token(username, cache_path):
    """Return the cached token for *username*, or ``None`` when unavailable."""
    with _token_cache_lock:
        try:
            with open(cache_path, "r", encoding="utf-8") as handle:
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
        directory = os.path.dirname(absolute_path) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass

        cache = {}
        try:
            with open(absolute_path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                cache = loaded
        except (OSError, ValueError, TypeError):
            pass
        cache[str(username)] = token

        fd, temporary_path = tempfile.mkstemp(prefix=".token-cache-", dir=directory, text=True)
        try:
            fchmod = getattr(os, "fchmod", None)
            if fchmod is not None:
                try:
                    fchmod(fd, 0o600)
                except (AttributeError, NotImplementedError, OSError):
                    os.chmod(temporary_path, 0o600)
            else:
                os.chmod(temporary_path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(cache, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                if hasattr(os, "fsync"):
                    os.fsync(handle.fileno())
            os.replace(temporary_path, absolute_path)
            try:
                os.chmod(absolute_path, 0o600)
            except OSError:
                pass

            # Persist the directory entry where the platform supports it.  If
            # the operation is unavailable, the atomic replace still protects
            # readers from partial JSON.
            try:
                directory_fd = os.open(directory, os.O_DIRECTORY)
            except (AttributeError, OSError):
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
            raise


# Internal aliases make the module pleasant to use from older integrations
# that imported the implementation names directly.
_load_cached_token = load_cached_token
_save_cached_token = save_cached_token


__all__ = ["load_cached_token", "save_cached_token"]
