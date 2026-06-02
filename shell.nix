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
