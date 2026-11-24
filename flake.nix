{
  description = "Voxize development shell, and the Voxize Remote client's build";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        # The Android SDK is unfree; only the client's build uses this package set.
        unfreePkgs = import nixpkgs {
          inherit system;
          config.allowUnfree = true;
        };
        version = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project.version;
        remote = unfreePkgs.callPackage ./nix/voxize-remote-apk.nix { inherit version; };
      in
      {
        devShells.default = import ./shell.nix { inherit pkgs; };
        # Unsigned; scripts/release-apk.sh signs it for a release.
        packages.voxize-remote-apk = remote.apk;
        packages.voxize-remote-tools = remote.tools;
      }
    );
}
