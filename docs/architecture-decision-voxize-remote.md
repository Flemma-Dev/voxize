# Architecture Decision: Voxize Remote, a Phone as the Microphone

**Date**: 2026-11-24
**Status**: Decided, implemented in 0.4.1 (built first inside the author's NixOS configuration, nix-meridian, then moved here)
**Referenced from**: `README.md` ("Voxize Remote")

## Context

The author reaches Claude Code sessions from an Android phone: Termux, `ssh` over Tailscale, a tmux session list on the machine. Typing prompts on a phone keyboard was the bottleneck the desktop overlay had already removed at the desk. The phone has a microphone and the machine has Voxize; what was missing was a way to dictate on one into the other, without a second transcription stack, without anything reachable from outside the author's devices, and without a recording anyone but the author can start.

## Decision

**The phone is only a microphone.** An Android app (`android/`, package `dev.flemma.voxize.remote`) records 24 kHz mono 16-bit PCM and streams it to a server on the machine (`voxize-remote serve`, `voxize.remote.server`). Everything else is Voxize as it is: one backend per dictation, `python -m voxize.backend --audio-fd FD`, reading the phone's audio from a pipe instead of the microphone (`PipeAudioCapture`), with live preview, batch transcription and cleanup unchanged. The result goes into the tmux pane the phone's Termux is showing, as a bracketed paste and never with Enter; a pane that never asked for bracketed paste gets the lines joined with spaces.

**Only a tap on the phone starts a recording.** The server has no command that starts one. The app obeys the server's `capture` only within 10 seconds of a tap on itself, so even a server that proves itself cannot open the microphone on its own.

**Pairing, then proof on every connection.** Pairing is once per phone, in a 120 s window `voxize-remote pair` opens. The app commits to a nonce, receives a 256-bit secret, reveals the nonce, and shows a six-digit code derived from both; the author types it on the machine, which never prints it. The server keeps one GNOME Keyring item per phone, tied to the Tailscale node it paired from. Every connection runs HMAC challenges both ways, the server first: the app obeys nothing before the server has proved it holds the secret, and the secret works only from its own Tailscale node.

**Tailscale only.** The server refuses peers `tailscale whois` cannot vouch for, peers of another Tailscale user, the machine's own address and loopback. The app dials Tailscale addresses only, binds its socket to the VPN network Android routes it through, and refuses to connect without one: an app left out of Tailscale's split tunneling would otherwise reach a 100.x address over Wi-Fi or mobile data, where anyone could answer (some carriers hand out 100.64/10 themselves).

**On demand, both ends.** The server is meant to be socket-activated by systemd (sockets named `clients` and `control`) and to exit five minutes after its last phone leaves. The app is armed by `ssh` itself (`am start …/.ArmActivity` as an ssh `LocalCommand`), shows its floating button only while Termux is in front with a session open (Usage access tells it which app is in front) and while it records, and disarms after 15 minutes awake with no session or no server.

**The backend stays in charge of its own safety.** It runs in `/` with Python's safe path (`-P`), so code planted in a pane's directory is never imported; the pane's directory reaches Voxize only as data, for `WHISPER.txt`.

**Distribution: a signed APK per release.** The app is built with Nix and no Gradle (`nix/voxize-remote-apk.nix`: aapt2, javac, d8, zipalign), and `scripts/release-apk.sh` signs it with a release key kept in `android/signing/` (gitignored, no backup) for the release's `voxize-remote-VERSION.apk` and `.sha256`. Losing the key costs one reinstall: `voxize-remote setup-client` replaces a client signed with another key (or newer than the release) without asking, because the app keeps nothing but its pairing and setup redoes the pairing anyway. Client and server ship in one release: `tests/test_remote_client_agreement.py` compiles the app's `Pairing.java` and holds it against `pairing.py`.

## Consequences

- The server and the app are one version. An app from another release may not speak the server's protocol; `setup-client` installs the release's own app.
- A machine needs Tailscale, tmux, the GNOME Keyring and an OpenAI key as the desktop does; nothing listens on other networks once the port is kept to Tailscale's interface.
- Dictations from the phone are normal Voxize sessions under `~/.local/state/voxize/`; costs and logs are where the desktop's are.
- The app is not on any store. Updates arrive by running `setup-client` again with a release's APK, over USB or Wireless debugging.

## Rejected alternatives

- **A web app in the phone's browser.** No microphone in the background, no floating button over Termux, and a page on the machine to serve and protect.
- **Transcribing on the phone.** A second transcription stack, a second API key on the phone, and none of Voxize's vocabulary or cleanup.
- **Per-machine signing keys** (the first version signed at install time with a key made on each machine). It needed a JDK and apksigner on every machine and gave nothing a release key does not: whoever can install over adb can replace the app either way.
- **Pairing by trusting the Tailscale identity alone.** A node of the same user is not proof of this app; a copied secret is not proof of this node. Both checks stay.
