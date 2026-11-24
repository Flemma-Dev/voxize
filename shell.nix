{
  pkgs ? import <nixpkgs> { },
}:

let
  runtimeDeps = with pkgs; [
    gtk4
    libadwaita
    gobject-introspection
    portaudio
    libsecret # gi.repository.Secret (keyring access)

    # The dictation window uses the system UI font throughout. IBM Plex
    # Mono is still named by the meeting recorder (.rename-original).
    ibm-plex
  ];

  devDeps = with pkgs; [
    python313 # match Nix package; PyGObject 3.56 + Python 3.14 breaks constructors
    pkg-config # build-time
    wl-clipboard # manual testing (wl-copy)
    dotool # key/mouse simulation
    librsvg # rsvg-convert: scripts/tui_screenshots.py --hero (SVG -> PNG)
    # Voxize Remote's tests: a private tmux server, a pty for an attached client (script), socket activation
    # (systemd-socket-activate), and the client's Java against the server's Python (javac)
    tmux
    util-linux
    systemd
    jdk17_headless
    android-tools # adb: voxize-remote setup-client by hand
  ];
in

pkgs.mkShell {
  name = "voxize-dev-shell";

  packages = runtimeDeps ++ devDeps;

  NIX_LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath runtimeDeps;

  shellHook = ''
    export LD_LIBRARY_PATH="$NIX_LD_LIBRARY_PATH"
  '';
}
