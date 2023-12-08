{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    poetry2nix.url = "github:nix-community/poetry2nix";
    flake-utils.url = "github:numtide/flake-utils";
  };
  outputs = { self, nixpkgs, flake-utils, poetry2nix, ... }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs {
          inherit system;
          overlays = [ poetry2nix.overlays.default ];
        };
      in
      {
        packages = rec {
          git-cdn = pkgs.callPackage ./default.nix { };

          script = pkgs.writeShellApplication {
              runtimeInputs = [git-cdn];
              name = "git-cdn-start";
              text = ''
              ${git-cdn}/bin/gunicorn git_cdn:app:app -b :8000
              '';
          };

          default = git-cdn;
        };
        apps = rec {
          git-cdn = flake-utils.lib.mkApp { drv = self.packages.${system}.git-cdn; };
          default = git-cdn;
        };
      }
    );
}
