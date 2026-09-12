"""Small helpers for replacing private text files atomically.

The callers in this project write credentials and account configuration.  A
temporary file is therefore created beside the destination, restricted before
it is populated, flushed and synced, and only then replaced into place.  The
helper deliberately avoids changing permissions on an already existing parent
directory.
"""

from __future__ import annotations

import os
import tempfile


def _set_private_mode(fd: int, temporary_path: str, mode: int) -> None:
    """Apply *mode* using the descriptor where possible.

    ``fchmod`` is unavailable on some Windows/Python combinations.  Falling
    back to the temporary path keeps the helper portable while still ensuring
    the temporary file is restricted before any content is written.
    """

    fchmod = getattr(os, "fchmod", None)
    if fchmod is not None:
        try:
            fchmod(fd, mode)
            return
        except (AttributeError, NotImplementedError, OSError):
            pass
    os.chmod(temporary_path, mode)


def _sync_directory(directory: str) -> None:
    """Best-effort sync of a replaced directory entry where supported."""

    directory_flag = getattr(os, "O_DIRECTORY", None)
    if directory_flag is None:
        return
    try:
        directory_fd = os.open(directory, os.O_RDONLY | directory_flag)
    except (AttributeError, NotImplementedError, OSError):
        return
    try:
        try:
            os.fsync(directory_fd)
        except (AttributeError, NotImplementedError, OSError):
            # Directory fsync is unsupported on some platforms.  The file
            # itself has already been flushed and the replace is atomic.
            pass
    finally:
        try:
            os.close(directory_fd)
        except OSError:
            pass


def atomic_write_text(
    path: str | os.PathLike[str],
    content: str,
    *,
    mode: int = 0o600,
    prefix: str = ".atomic-write-",
) -> None:
    """Atomically write UTF-8 text to *path* with private permissions.

    Parent directories are created with mode ``0700`` when absent.  Existing
    directories are left unchanged.  Any failure before ``os.replace`` leaves
    an existing destination untouched and removes the temporary file.
    """

    destination = os.path.abspath(os.fspath(path))
    directory = os.path.dirname(destination) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)

    fd, temporary_path = tempfile.mkstemp(prefix=prefix, dir=directory, text=True)
    # Ownership of ``fd`` moves to the stream on ``os.fdopen``; the exception
    # path must therefore track it separately from the still-open descriptor.
    descriptor: int | None = fd
    try:
        _set_private_mode(fd, temporary_path, mode)
        handle = os.fdopen(fd, "w", encoding="utf-8")
        # fdopen now owns and will close the descriptor.  Do not retain the
        # integer for the exception path: the OS may reuse it immediately.
        descriptor = None
        with handle:
            handle.write(content)
            handle.flush()
            fsync = getattr(os, "fsync", None)
            if fsync is not None:
                fsync(handle.fileno())
        os.replace(temporary_path, destination)
        try:
            os.chmod(destination, mode)
        except OSError:
            # The temporary file was restricted before the replace.  A few
            # platforms do not support all chmod bits on the destination.
            pass
        _sync_directory(directory)
    except Exception:
        # Only close a descriptor still owned by this function.  Once fdopen
        # succeeds, ``descriptor`` is None and the stream owns cleanup.
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


__all__ = ["atomic_write_text"]
