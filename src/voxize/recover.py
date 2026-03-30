"""Recovery script generation for batch transcription."""

import logging
import os

logger = logging.getLogger(__name__)

_MODEL = "gpt-4o-transcribe"


def write_recover_script(session_dir: str) -> None:
    """Write a recover.sh script for batch transcription to session_dir."""
    script = f"""\
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
OPENAI_API_KEY="${{OPENAI_API_KEY:-$(secret-tool lookup service openai key api)}}"
curl -s https://api.openai.com/v1/audio/transcriptions \\
  -H "Authorization: Bearer $OPENAI_API_KEY" \\
  -F model={_MODEL} \\
  -F file=@audio.wav \\
  -F response_format=text | tee recovered.txt
echo
read -r -p "Press Enter to close..."
"""
    path = os.path.join(session_dir, "recover.sh")
    with open(path, "w") as f:
        f.write(script)
    os.chmod(path, 0o755)
    logger.debug("write_recover_script: path=%s", path)
