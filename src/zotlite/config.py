import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "zotlite" / "settings.toml"
DEFAULT_CACHE_DIR   = Path("/tmp/zotlite_cache")


class ZotliteError(Exception):
    """A user-facing error: printed without a traceback."""


@dataclass
class Config:
    library_path : str
    cache_dir    : Path = DEFAULT_CACHE_DIR

    @classmethod
    def load(cls, path=None):
        path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH

        if not path.exists():
            raise ZotliteError(f"Configuration file not found at {path}")

        with open(path, "rb") as f:
            config = tomllib.load(f)

        library_path = config.get('zotero', {}).get('library_path', '').strip()

        if not library_path:
            raise ZotliteError("Missing library_path in settings.")

        return cls(library_path=library_path)
