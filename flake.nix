{
  description = "khal with built-in Google Calendar sync";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (s: f nixpkgs.legacyPackages.${s});
    in {
      packages = forAll (pkgs: rec {
        khal = pkgs.callPackage ./package.nix { };
        default = khal;
      });
      overlays.default = final: prev: { khal = final.callPackage ./package.nix { }; };
      devShells = forAll (pkgs: {
        default = pkgs.mkShell { inputsFrom = [ self.packages.${pkgs.system}.khal ]; packages = [ pkgs.git ]; };
      });
    };
}
