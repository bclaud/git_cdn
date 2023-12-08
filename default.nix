{ pkgs
, poetry2nix
}:

let app = poetry2nix.mkPoetryApplication {
  projectDir = ./.;

  postPatch = ''
    substituteInPlace pyproject.toml \
    --replace poetry.masonry.api poetry.core.masonry.api \
    --replace "poetry>=" "poetry-core>="
  '';

#   nativeBuildInputs = [
#     pkgs.python3Packages.poetry-core
#   ];

  overrides = poetry2nix.defaultPoetryOverrides.extend
    (self: super: {

      aiohttp-devtools = super.aiohttp-devtools.overridePythonAttrs (old: {
        buildInputs = (old.buildInputs or [ ]) ++ [ super.setuptools ];
      });

    #   git-cdn = super.git-cdn.overridePythonAttrs
    #     (
    #       old: {
    #         buildInputs = (old.buildInputs or [ ]) ++ [
    #         #   super.poetry-core
    #         #   super.poetry
    #         #   super.poetry-dynamic-versioning-plugin
    #         #   super.black
    #         ];
    #         # build-backend = "poetry.masonry.api"

    #         postPatch = ''
    #         fdafsa
    #           substituteInPlace pyproject.toml \
    #           --replace poetry.masonry.api poetry.core.masonry.api \
    #         '';
    #         # --replace "poetry>=" "poetry-core>="

    #         #nativeBuildInputs = [ python3.pkgs.poetry-core ];
    #       }
    #     );
    });

};

  # Test support for overriding the app passed to the environment
  overridden = app.overrideAttrs (old: {
    name = "${old.pname}-overridden-${old.version}";
  });
  depEnv = app.dependencyEnv.override {
    app = overridden;
  };
in

app.dependencyEnv

### Executar programa assim:
# $(POETRY) run gunicorn -c config.py git_cdn.app:app -b :8000
