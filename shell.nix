# shell.nix
{ pkgs ? import <nixpkgs> {} }:

let
  khal = pkgs.callPackage ./package.nix { };
  py = pkgs.python3.withPackages (ps: with ps; [
    click click-log icalendar urwid pyxdg pytz python-dateutil
    configobj tzlocal setproctitle
    pytest freezegun hypothesis packaging pip virtualenv
  ]);
in
pkgs.mkShell {
  packages = [ py pkgs.git khal ];
  shellHook = ''
    [ -d .venv ] || python -m venv --system-site-packages .venv
    . .venv/bin/activate
    pip install -q -e . --no-deps
    python -c "import vdirsyncer" 2>/dev/null || pip install -q vdirsyncer
  '';
}
