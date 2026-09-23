"""Shared running-size budget for one send/draft call's attachments.

Both attachment sources (URL downloads and pre-fetched base64) draw from the
same ceiling, and both need to abort at the moment it is crossed rather than
after everything is already resident in memory. A single budget object,
created by the caller and threaded through both paths, is what makes that
possible.

No lock: asyncio never preempts inside synchronous code, so the read-modify-
write in :meth:`SizeBudget.take` is atomic with respect to sibling coroutines.
"""

from typing import Optional

from ._constants import MAX_TOTAL_ATTACHMENT_SIZE


class AttachmentBudgetExceeded(ValueError):
    """Raised when attachments would exceed the combined size ceiling."""


class SizeBudget:
    """Running total of attachment bytes, capped at *limit*."""

    def __init__(self, limit: Optional[int] = None) -> None:
        # Resolved at call time rather than bound as a default argument, so the
        # ceiling stays overridable (tests use a few bytes instead of 50MB).
        self.limit = MAX_TOTAL_ATTACHMENT_SIZE if limit is None else limit
        self.used = 0

    def take(self, nbytes: int) -> None:
        """Charge *nbytes* to the budget, raising once the limit is crossed."""
        self.used += nbytes
        if self.used > self.limit:
            raise AttachmentBudgetExceeded(
                f"Combined attachment size exceeds {self.limit // (1024 * 1024)}MB limit"
            )

    def mark(self) -> int:
        """Snapshot the current total, for :meth:`rewind`."""
        return self.used

    def rewind(self, mark: int) -> None:
        """Undo charges made since *mark*.

        Used when a download is retried: its first attempt's partial bytes
        were charged but never became an attachment, so leaving them on the
        total would reject a payload that fits.
        """
        self.used = mark
