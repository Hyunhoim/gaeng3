"""Independent, locally sealed safety-blind evaluation package."""

from .sealing import SealError, open_record, seal_record

__all__ = ["SealError", "open_record", "seal_record"]
