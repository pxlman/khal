{ lib, python3Packages, installShellFiles, vdirsyncer }:

python3Packages.buildPythonApplication {
  pname = "khal";
  version = "0.14.2";
  pyproject = true;

  src = lib.cleanSourceWith {
    src = ./.;
    filter = path: type:
      let b = baseNameOf path; in
      !(builtins.elem b [ ".venv" ".git" "result" "__pycache__" ".pytest_cache" "khal.egg-info" "version.py" ]);
  };

  env.SETUPTOOLS_SCM_PRETEND_VERSION = "0.14.2";

  build-system = with python3Packages; [ setuptools setuptools-scm ];

  dependencies = with python3Packages; [
    click click-log icalendar urwid pyxdg pytz python-dateutil
    configobj tzlocal setproctitle
  ];

  nativeBuildInputs = [ installShellFiles ];

  nativeCheckInputs = with python3Packages; [ pytestCheckHook freezegun hypothesis packaging ] ++ [ vdirsyncer ];
  enabledTestPaths = [ "tests/delete_test.py" "tests/sync_test.py" "tests/settings_test.py" "tests/cli_test.py" ];
  disabledTests = [ "test_configure_command_cannot_write_config_file" "test_configure_command_cannot_create_vdir" ];

  postInstall = ''
    installShellCompletion --cmd khal \
      --bash <(_KHAL_COMPLETE=bash_source $out/bin/khal) \
      --zsh <(_KHAL_COMPLETE=zsh_source $out/bin/khal) \
      --fish <(_KHAL_COMPLETE=fish_source $out/bin/khal)
  '';

  meta = {
    description = "Terminal calendar with built-in two-way Google Calendar sync";
    homepage = "https://github.com/pimutils/khal";
    license = lib.licenses.mit;
    mainProgram = "khal";
  };
}
