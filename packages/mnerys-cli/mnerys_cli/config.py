"""Profile and credential storage for the mnerys CLI.

Profiles are plain JSON in the platform user configuration directory; the
credential (API token) lives in a separate file with restrictive
permissions. Tokens are never echoed, logged or written into shell
history by the CLI itself.
"""

import json
import os
import sys
from pathlib import Path

from mnerys_cli.errors import ConfigError

CONFIG_DIR_NAME = "mnerys"
PROFILES_FILE = "profiles.json"
CREDENTIALS_DIR = "credentials"


def config_dir() -> Path:
    """Platform-appropriate user configuration directory."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "mnerys"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "mnerys"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "mnerys"


def _mkdir_private(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass  # Windows does not honor POSIX modes; permissions are ACL-based.


def profile_paths() -> tuple[Path, Path]:
    root = config_dir()
    _mkdir_private(root)
    creds = root / CREDENTIALS_DIR
    _mkdir_private(creds)
    return root / PROFILES_FILE, creds


def load_profiles(root: Path | None = None) -> dict:
    profiles_file, _creds = _paths(root)
    if not profiles_file.is_file():
        return {"active": None, "profiles": {}}
    try:
        data = json.loads(profiles_file.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ConfigError("The mnerys configuration file is not readable JSON") from exc
    if not isinstance(data, dict):
        raise ConfigError("The mnerys configuration file is malformed")
    data.setdefault("active", None)
    data.setdefault("profiles", {})
    return data


def _paths(root: Path | None) -> tuple[Path, Path]:
    if root is not None:
        return root / PROFILES_FILE, root / CREDENTIALS_DIR
    return profile_paths()


def active_profile_name(profiles: dict, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    return profiles.get("active")


def resolve_endpoint(profiles: dict, profile: str | None) -> tuple[str, str | None]:
    """Return (endpoint, token) for the selected profile with env overrides."""
    endpoint = os.environ.get("MNERYS_ENDPOINT")
    token = os.environ.get("MNERYS_TOKEN")
    if endpoint:
        return endpoint, token
    name = active_profile_name(profiles, profile)
    if not name:
        raise ConfigError(
            "No profile selected. Run 'mnerys connect' first, "
            "or set MNERYS_ENDPOINT/MNERYS_TOKEN",
        )
    entry = (profiles.get("profiles") or {}).get(name)
    if not entry:
        raise ConfigError(f"Profile {name!r} is not configured")
    endpoint = entry.get("endpoint")
    if not endpoint:
        raise ConfigError(f"Profile {name!r} has no endpoint")
    stored = _read_credential(name)
    return endpoint, token or stored


def _read_credential(profile: str) -> str | None:
    _, creds = _paths(None)
    path = creds / _credential_name(profile)
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _credential_name(profile: str) -> str:
    return profile.replace("/", "_").replace("\\", "_") + ".token"


def save_profile(
    name: str,
    endpoint: str,
    token: str | None,
    root: Path | None = None,
) -> None:
    if not name or not endpoint:
        raise ConfigError("connect requires --profile and --endpoint")
    profiles_file, creds = _paths(root)
    _mkdir_private(profiles_file.parent)
    _mkdir_private(creds)
    profiles = load_profiles(root)
    profiles.setdefault("profiles", {})
    profiles["profiles"][name] = {"endpoint": endpoint.rstrip("/")}
    profiles["active"] = name
    profiles_file.write_text(
        json.dumps(profiles, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    try:
        profiles_file.chmod(0o600)
    except OSError:
        pass
    if token:
        token_path = creds / _credential_name(name)
        token_path.write_text(token.strip(), encoding="utf-8")
        try:
            token_path.chmod(0o600)
        except OSError:
            pass


def describe_root() -> Path:
    return config_dir()