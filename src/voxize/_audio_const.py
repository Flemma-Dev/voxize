"""Audio format constants shared by audio, batch, and app modules.

Separated from audio.py so that batch.py can reference the WAV layout
without pulling in sounddevice (and its ~2s numpy transitive import).
"""

SAMPLE_RATE = 24_000
CHANNELS = 1
DTYPE = "int16"
BLOCK_SIZE = 960  # 40ms at 24kHz — 1920 bytes per block
WAV_HEADER_SIZE = 44
