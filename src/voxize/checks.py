"""Startup dependency checks and API key retrieval."""

import sys

import gi

gi.require_version("Secret", "1")

# gi.require_version() above is executable code; the subsequent import triggers E402.
from gi.repository import Secret  # noqa: E402

# Match the schema used by secret-tool (attribute-only matching, no schema name filter).
# Users still store keys via: secret-tool store --label='OpenAI API Key' service openai key api
_GENERIC_SCHEMA = Secret.Schema.new(
    "org.freedesktop.Secret.Generic",
    Secret.SchemaFlags.DONT_MATCH_NAME,
    {
        "service": Secret.SchemaAttributeType.STRING,
        "key": Secret.SchemaAttributeType.STRING,
    },
)


def check_all() -> list[str]:
    """Run all startup checks, returning a list of error messages (empty = all good)."""
    errors: list[str] = []

    # GTK4
    try:
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk  # noqa: F401
    except (ImportError, ValueError) as e:
        errors.append(f"GTK4 not available: {e}")

    # API keys via libsecret
    for service, label in [("openai", "OpenAI")]:
        try:
            password = Secret.password_lookup_sync(
                _GENERIC_SCHEMA,
                {"service": service, "key": "api"},
                None,
            )
            if not password:
                errors.append(
                    f"{label} API key not found in keyring "
                    f"(set with: secret-tool store --label='{label} API Key' service {service} key api)"
                )
        except Exception as e:
            errors.append(f"Failed to look up {label} API key: {e}")

    return errors


def get_api_key(service: str) -> str:
    """Retrieve an API key from GNOME Keyring via libsecret.

    Raises RuntimeError if the key is not found.
    """
    password = Secret.password_lookup_sync(
        _GENERIC_SCHEMA,
        {"service": service, "key": "api"},
        None,
    )
    if not password:
        raise RuntimeError(f"API key for '{service}' not found in keyring")
    if not password.startswith("sk-"):
        raise RuntimeError(
            f"API key for '{service}' has unexpected format (expected 'sk-' prefix)"
        )
    return password


def exit_on_failure() -> None:
    """Run checks and exit with clear error messages if any fail."""
    errors = check_all()
    if errors:
        print("Voxize startup checks failed:\n", file=sys.stderr)
        for err in errors:
            print(f"  • {err}", file=sys.stderr)
        print(
            "\nEnsure you are running inside the nix dev shell (nix develop).",
            file=sys.stderr,
        )
        sys.exit(1)
