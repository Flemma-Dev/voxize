"""Auto-generate a meeting title from a transcript via GPT-5.4 Nano.

Reads the session's ``transcript.txt``, sends it to the Responses API
with a detailed system prompt, and returns a short descriptive title.
Runs synchronously in a background thread — the caller is responsible
for thread dispatch and ``GLib.idle_add`` bridging.
"""

from __future__ import annotations

import logging
import os
import time

from voxize.checks import get_api_key

logger = logging.getLogger(__name__)

_MODEL = "gpt-5.4-nano"

_SYSTEM_PROMPT = (
    "# Role\n"
    "You are a meeting title generator. You receive the timestamped, "
    "speaker-labelled transcript of a meeting and produce a single short "
    "title that captures what the meeting was about.\n"
    "\n"
    "# Goal\n"
    "The title will be shown in a compact session list alongside the meeting "
    "date. It must let the user instantly recall which meeting this was, even "
    "weeks later. Think of it like a calendar entry subject line.\n"
    "\n"
    "# Output format\n"
    "Return ONLY the title string. No quotes. No preamble. No explanation. "
    "No punctuation at the end (no trailing period). No markdown formatting. "
    "Just the bare title text.\n"
    "\n"
    "# Constraints\n"
    "- Length: 3 to 10 words. Aim for 5-7.\n"
    "- Do not start with generic words like 'Meeting about', 'Discussion on', "
    "'Talk about', 'Conversation regarding'. Jump straight to the topic.\n"
    "- Do not include participant names or speaker labels.\n"
    "- Do not include the date or time — those are already shown in the UI.\n"
    "- If the meeting covers multiple topics, pick the dominant one. If no "
    "single topic dominates, pick the one discussed earliest and longest.\n"
    "- Use sentence case (capitalize only the first word and proper nouns).\n"
    "- Always produce a title, even for short or informal conversations. "
    "If the topic is unclear, describe the *tone* or *activity* instead "
    "(e.g. 'Quick debugging chat', 'Informal check-in'). Only as an "
    "absolute last resort — if the transcript is completely unintelligible "
    "or under 3 turns — return exactly: Untitled meeting\n"
    "\n"
    "# Transcript format\n"
    "The transcript uses this format per turn:\n"
    "  HH:MM:SS,mmm --> HH:MM:SS,mmm [speaker_N]\n"
    "  Spoken text for that turn.\n"
    "\n"
    "Timestamps and speaker labels are metadata — use them to understand who "
    "said what and how long each topic was discussed, but do not include them "
    "in your output.\n"
    "\n"
    "# Examples\n"
    "\n"
    "Good titles:\n"
    "- Sprint planning for auth migration\n"
    "- Debugging the PipeWire capture pipeline\n"
    "- Q3 hiring priorities and interview process\n"
    "- Customer onboarding flow redesign\n"
    "- Release blockers for v2.4\n"
    "\n"
    "Bad titles (do NOT produce these):\n"
    "- Meeting about sprint planning (generic prefix)\n"
    "- John and Sarah discuss auth (includes names)\n"
    "- 22 May 2026 standup (includes date)\n"
    "- Auth. (too short, trailing period)\n"
    "- A productive discussion about various engineering topics (vague)\n"
    "\n"
    "# Reminders\n"
    "1. Output ONLY the title — nothing before, nothing after.\n"
    "2. Sentence case. No trailing punctuation.\n"
    "3. 3-10 words. No generic prefixes. No names. No dates."
)


def generate_title(
    session_dir: str,
    meeting_date: str,
) -> str:
    """Generate a title for a meeting session. Runs synchronously (call from a thread).

    ``meeting_date`` is a human-readable date string (e.g. "22 May 2026 11:46")
    included as context for the model.

    Returns the generated title, or raises on failure.
    """
    transcript_path = os.path.join(session_dir, "transcript.txt")
    with open(transcript_path) as f:
        transcript = f.read()

    if not transcript.strip():
        return "Untitled meeting"

    api_key = get_api_key("openai")

    from openai import OpenAI

    client = OpenAI(api_key=api_key)

    user_message = (
        f"Meeting date: {meeting_date}\n" f"\n" f"Transcript:\n" f"{transcript}"
    )

    logger.debug(
        "generate_title: calling %s, transcript_len=%d",
        _MODEL,
        len(transcript),
    )
    t0 = time.monotonic()

    response = client.responses.create(
        model=_MODEL,
        reasoning={"effort": "low"},
        input=[
            {"role": "developer", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        store=False,
    )

    title = response.output_text.strip().rstrip(".")
    elapsed = time.monotonic() - t0

    logger.debug(
        "generate_title: done in %.1fs, title=%r",
        elapsed,
        title,
    )

    return title or "Untitled meeting"
