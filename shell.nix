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

    # Typography for the overlay. The transcript is the product, so the
    # serif is a hard dependency, not a nicety — without Source Serif 4
    # the text falls back to DejaVu Serif and the design reads wrong.
    source-serif # transcript (OFL)
    ibm-plex # micro-labels in the guidance popover
  ];

  devDeps = with pkgs; [
    python313 # match Nix package; PyGObject 3.56 + Python 3.14 breaks constructors
    pkg-config # build-time
    wl-clipboard # manual testing (wl-copy)
    dotool # key/mouse simulation
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
