"""Recovery script generation for batch transcription."""

from __future__ import annotations

import logging
import os
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from voxize.prompt import PromptSource

logger = logging.getLogger(__name__)

_MODEL = "gpt-transcribe"


def write_recover_script(
    session_dir: str, prompts: list[PromptSource] | None = None
) -> None:
    """Write a recover.sh script for batch transcription to session_dir.

    The prompt is baked in so a recovery run reproduces what production
    would have sent — without it, vocabulary guidance is silently absent
    and the recovered transcript can differ from the real one.
    """
    prompt = "\n".join(s.content for s in (prompts or [])).strip()
    prompt_flag = f"  -F prompt={shlex.quote(prompt)} \\\n" if prompt else ""
    script = f"""\
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
OPENAI_API_KEY="${{OPENAI_API_KEY:-$(secret-tool lookup service openai key api)}}"
curl -s https://api.openai.com/v1/audio/transcriptions \\
  -H "Authorization: Bearer $OPENAI_API_KEY" \\
  -F model={_MODEL} \\
{prompt_flag}  -F file=@audio.wav \\
  -F response_format=text | tee recovered.txt
echo
read -r -p "Press Enter to close..."
"""
    path = os.path.join(session_dir, "recover.sh")
    with open(path, "w") as f:
        f.write(script)
    os.chmod(path, 0o755)
    logger.debug("write_recover_script: path=%s prompt_len=%d", path, len(prompt))
