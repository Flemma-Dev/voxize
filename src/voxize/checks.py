"""Startup dependency checks and API key retrieval."""

import logging
import sys

import gi

logger = logging.getLogger(__name__)

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
    logger.debug("check_all: running startup checks")
    errors: list[str] = []

    # GTK4
    try:
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk  # noqa: F401

        logger.debug("check_all: GTK4 available")
    except (ImportError, ValueError) as e:
        logger.debug("check_all: GTK4 not available: %s", e)
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
                logger.debug("check_all: %s API key not found in keyring", label)
                errors.append(
                    f"{label} API key not found in keyring "
                    f"(set with: secret-tool store --label='{label} API Key' service {service} key api)"
                )
            else:
                logger.debug("check_all: %s API key found", label)
        except Exception as e:
            logger.debug("check_all: %s API key lookup failed: %s", label, e)
            errors.append(f"Failed to look up {label} API key: {e}")

    logger.debug("check_all: complete, errors=%d", len(errors))
    return errors


def get_api_key(service: str) -> str:
    """Retrieve an API key from GNOME Keyring via libsecret.

    Raises RuntimeError if the key is not found.
    """
    logger.debug("get_api_key: service=%s", service)
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
    logger.debug("get_api_key: retrieved successfully")
    return password


def exit_on_failure() -> None:
    """Run checks and exit with clear error messages if any fail."""
    logger.debug("exit_on_failure: running checks")
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
