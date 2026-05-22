"""Voxize meeting recorder — captures mic + system audio to a stereo WAV.

Separate from the dictation pipeline: no live transcription, no API
calls, no cleanup. The deliverable is a crash-safe stereo WAV (L=mic,
R=system) suitable for offline transcription + diarization with tools
like WhisperX.
"""
