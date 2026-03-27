"""Startup dependency checks and API key retrieval."""

import shutil
import subprocess
import sys


def check_all() -> list[str]:
    """Run all startup checks, returning a list of error messages (empty = all good)."""
    errors: list[str] = []

    # GTK4
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk  # noqa: F401
    except (ImportError, ValueError) as e:
        errors.append(f"GTK4 not available: {e}")

    # wl-copy
    if not shutil.which("wl-copy"):
        errors.append("wl-copy not found on PATH (install wl-clipboard)")

    # secret-tool
    if not shutil.which("secret-tool"):
        errors.append("secret-tool not found on PATH (install libsecret)")

    # API keys via secret-tool
    if shutil.which("secret-tool"):
        for service, label in [("openai", "OpenAI")]:
            try:
                result = subprocess.run(
                    ["secret-tool", "lookup", "service", service, "key", "api"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if not result.stdout.strip():
                    errors.append(
                        f"{label} API key not found in keyring "
                        f"(set with: secret-tool store --label='{label} API Key' service {service} key api)"
                    )
            except subprocess.TimeoutExpired:
                errors.append(f"secret-tool timed out looking up {label} API key")
            except OSError as e:
                errors.append(f"Failed to run secret-tool for {label}: {e}")

    return errors


def get_api_key(service: str) -> str:
    """Retrieve an API key from GNOME Keyring via secret-tool.

    Raises RuntimeError if the key is not found.
    """
    result = subprocess.run(
        ["secret-tool", "lookup", "service", service, "key", "api"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    key = result.stdout.strip()
    if not key:
        raise RuntimeError(
            f"API key for '{service}' not found in keyring"
        )
    return key


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
