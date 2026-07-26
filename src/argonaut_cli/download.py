"""Streaming download shared by the package installer and the NLLB model:
chunked copy to a file with progress reporting, cooperative cancellation
and partial-file cleanup."""

import os

from argonaut.translation import CancelledError

CHUNK_SIZE = 1024 * 256


def download_to(response, target, on_progress=None, is_cancelled=None,
                done=0, total=0):
    """Streams an open `response` into the file at `target`, reporting
    (done_bytes, total_bytes) after each chunk, and closes the response.
    `done` and `total` let a multi-file download report one continuous
    byte count; by default they cover this file alone. Cancelling or
    failing removes the partial file. Returns the updated `done`."""
    on_progress = on_progress or (lambda done, total: None)
    is_cancelled = is_cancelled or (lambda: False)
    if not total:
        total = done + int(response.headers.get("Content-Length") or 0)
    try:
        with open(target, "wb") as out:
            while True:
                if is_cancelled():
                    raise CancelledError()
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                on_progress(done, total)
    except BaseException:
        if os.path.exists(target):
            os.remove(target)
        raise
    finally:
        response.close()
    return done
