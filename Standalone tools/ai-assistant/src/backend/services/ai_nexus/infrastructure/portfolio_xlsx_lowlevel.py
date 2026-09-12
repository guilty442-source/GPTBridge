"""Compatibility interface for the documents subpackage."""

from .documents import portfolio_xlsx_lowlevel as _implementation

globals().update(
    {
        name: value
        for name, value in vars(_implementation).items()
        if not name.startswith("__")
    }
)

__all__ = getattr(
    _implementation,
    "__all__",
    tuple(name for name in vars(_implementation) if not name.startswith("_")),
)
