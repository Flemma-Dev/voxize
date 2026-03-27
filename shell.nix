{
  pkgs ? import <nixpkgs> { },
}:

pkgs.mkShell {
  name = "voxize-dev-shell";

  packages = with pkgs; [
    # UI
    gtk4
    gobject-introspection

    # Audio
    portaudio

    # Build
    pkg-config

    # Runtime tools
    wl-clipboard # wl-copy
    libsecret    # secret-tool

    # Dev/testing tools
    dotool # key/mouse simulation
  ];

  NIX_LD_LIBRARY_PATH = with pkgs; lib.makeLibraryPath [
    gtk4
    gobject-introspection
    portaudio
  ];

  shellHook = ''
    export LD_LIBRARY_PATH="$NIX_LD_LIBRARY_PATH"
  '';
}
