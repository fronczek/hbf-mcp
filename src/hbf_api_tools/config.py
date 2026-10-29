"""Private credential loading and deterministic credential-directory discovery."""
from __future__ import annotations

import os
import stat
from pathlib import Path


class ConfigurationError(RuntimeError):
    pass


def credentials_dir() -> Path:
    explicit = os.environ.get("HBF_CREDENTIALS_DIR")
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_dir():
            raise ConfigurationError("HBF_CREDENTIALS_DIR is not a directory")
        if Path(explicit).expanduser().is_symlink():
            raise ConfigurationError("Credential directory symlink is forbidden")
        if path.stat().st_mode & 0o077:
            raise ConfigurationError("Credentials directory must be private (mode 0700)")
        if path.stat().st_uid != os.geteuid():
            raise ConfigurationError("Credentials directory is not owned by the invoking user")
        return path

    # Never discover credentials inside repositories or another agent's worktree.
    # The Unix account's private home is the only implicit source.
    fallback = Path.home() / ".credentials"
    if fallback.is_symlink() or not fallback.is_dir():
        raise ConfigurationError("Private ~/.credentials directory not found")
    if fallback.stat().st_mode & 0o077:
        raise ConfigurationError("Credentials directory must be private (mode 0700)")
    if fallback.stat().st_uid != os.geteuid():
        raise ConfigurationError("Credentials directory is not owned by the invoking user")
    return fallback


def load_scalar_yaml(name: str) -> dict[str, str]:
    """Read the deliberately small flat-scalar credential YAML format."""
    if Path(name).name != name or not name.endswith(".yml"):
        raise ConfigurationError("Invalid credential filename")
    path = credentials_dir() / name
    try:
        info = path.lstat()
    except OSError:
        raise ConfigurationError(f"Credential file {name} is unavailable") from None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise ConfigurationError(f"Credential file {name} must be private (mode 0600)")
    values: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if not key or key in values:
                raise ConfigurationError(f"Credential file {name} has an invalid or repeated key")
            values[key] = value
    except (OSError, UnicodeError):
        raise ConfigurationError(f"Credential file {name} cannot be read") from None
    return values
