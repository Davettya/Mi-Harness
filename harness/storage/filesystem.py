"""Filesystem spelling for trusted internal storage, not workspace authorization."""

import os
from pathlib import Path


def storage_path(path: str | Path) -> Path:
    """Keep logical storage keys unchanged while supporting Windows long paths.

    Prefix the absolute root before appending object keys so mkdir, replace,
    open, stat, traversal and FileResponse all use the same namespace. This does
    not change the system long-path policy or the tool gateway's path rules.
    """
    # Resolve first: Store Python can redirect LOCALAPPDATA to LocalCache.
    # Prefixing the un-resolved spelling would bypass that redirection and split
    # the database and its objects across different physical directories.
    absolute = str(Path(path).resolve())
    if os.name == "nt" and not absolute.startswith("\\\\?\\"):
        absolute = "\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\") else "\\\\?\\" + absolute
    return Path(absolute)
