{
  description = "Stacked GitHub pull requests for Jujutsu";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    {
      self,
      nixpkgs,
    }:
    let
      inherit (nixpkgs) lib;

      inherit (lib)
        convertHash
        fromTOML
        versionAtLeast
        warnIf
        ;
      inherit (lib.attrsets) genAttrs;
      inherit (lib.lists) findFirst;
      inherit (lib.meta) getExe;
      inherit (lib.sources) cleanSource;
      inherit (lib.strings) hasSuffix makeBinPath;
      inherit (lib.trivial) readFile;

      forAllSystems = genAttrs [
        "aarch64-darwin"
        "aarch64-linux"
        "x86_64-linux"
      ];

      pyproject = fromTOML (readFile ./pyproject.toml);

      uvLock = fromTOML (readFile ./uv.lock);

      # The project's own `uv.lock` entry lists its resolved runtime dependencies, so reading
      # the names there keeps the dependency set and the pinned versions on one snapshot.
      # Marker-carrying entries would be read as unconditional.
      uvLockedProject = findFirst (
        p: p.name == pyproject.project.name
      ) (throw "uv.lock has no ${pyproject.project.name} entry") uvLock.package;

      dependencyNames = map (dependency: dependency.name) uvLockedProject.dependencies;

      mainProgram = "jj-stack";
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python3Packages = pkgs.python314Packages;

          # pyproject requires `httpx2>=2.12.0,<3`, and `httpx2 2.13.1` pins `httpcore2==2.13.1`,
          # but nixpkgs only ships 2.9.1 of both. Build the versions `uv.lock` pins with all
          # fields so we can't drift from it.
          #
          # Wheels, not sdists because their sdists derive the version from git and fall back to
          # 0.0.0, and PyPI serves new uploads only at content-addressed URLs, which
          # `fetchPypi` cannot express.
          uvLockedWheel =
            name:
            let
              package = findFirst (p: p.name == name) (throw "uv.lock has no ${name}") uvLock.package;
              wheel = findFirst (
                w: hasSuffix "-py3-none-any.whl" w.url
              ) (throw "uv.lock has no pure-python wheel for ${name}") package.wheels;
            in
            {
              inherit (package) version;
              src = pkgs.fetchurl {
                inherit (wheel) url;
                hash = convertHash {
                  inherit (wheel) hash;
                  hashAlgo = "sha256";
                  toHashFormat = "sri";
                };
              };
            };

          httpcore2Locked = uvLockedWheel "httpcore2";

          httpcore2 = python3Packages.buildPythonPackage {
            pname = "httpcore2";
            format = "wheel";
            inherit (httpcore2Locked) version src;
            dependencies = with python3Packages; [
              h11
              truststore
            ];
          };

          httpx2Locked = uvLockedWheel "httpx2";

          # `httpx2-jsfetch` is emscripten-only, so it is not a dependency here.
          httpx2 = python3Packages.buildPythonPackage {
            pname = "httpx2";
            format = "wheel";
            inherit (httpx2Locked) version src;
            dependencies = with python3Packages; [
              anyio
              httpcore2
              idna
              truststore
            ];
          };

          # The runtime dependency set follows `uv.lock`. Anything not in nixpkgs is overridden
          # here; a name with no nixpkgs attribute fails evaluation instead of going missing
          # from the build.
          #
          # `httpx2` is overridden only while nixpkgs lags the floor pyproject.toml sets. The
          # warning fires once nixpkgs reaches the uv.lock version, so the override can go.
          runtimeDependencyOverrides = {
            httpx2 =
              warnIf (versionAtLeast python3Packages.httpx2.version httpx2Locked.version)
                "nixpkgs ships httpx2 ${python3Packages.httpx2.version}; this override can go"
                httpx2;
          };

          jj-stack = python3Packages.buildPythonApplication {
            pname = pyproject.project.name;
            inherit (pyproject.project) version;
            pyproject = true;
            src = cleanSource ./.;

            build-system = [ python3Packages.hatchling ];

            dependencies = map (
              name: runtimeDependencyOverrides.${name} or python3Packages.${name}
            ) dependencyNames;

            # jj-stack shells out to jj, git (backing git repos), and gh (token lookup).
            makeWrapperArgs = [
              "--prefix"
              "PATH"
              ":"
              (makeBinPath [
                pkgs.gitMinimal
                pkgs.gh
                pkgs.jujutsu
              ])
            ];

            # The integration suite needs real jj and git, writes describe helpers with
            # `#!/usr/bin/env python3` shebangs, and signs commits with ssh-keygen.
            nativeCheckInputs = [
              pkgs.git
              pkgs.jujutsu
              pkgs.openssh
              pkgs.python314
            ]
            ++ (with python3Packages; [
              pytestCheckHook
              fastapi
              hypothesis
              jsonschema
              pytest-cov
              pytest-randomly
              pytest-xdist
            ]);

            # These cases write describe helpers with a `#!/usr/bin/env python3` shebang.
            # The build sandbox has no `/usr/bin`, so the helper cannot execute there; the
            # local `just check` run covers them instead.
            disabledTestPaths = map (name: "tests/integration/test_submit_command.py::${name}") [
              "test_submit_describe_with_failure_aborts_before_mutation"
              "test_submit_refreshes_unchanged_pr_text_and_preserves_github_edits"
              "test_submit_explicit_base_creates_and_updates_only_the_child_stack[2-0]"
            ];

            pythonImportsCheck = [ "jj_stack" ];

            meta = {
              inherit mainProgram;
              inherit (pyproject.project) description;
              homepage = pyproject.project.urls.Homepage;
              license = lib.licenses.asl20;
              maintainers = [
                {
                  name = "PlumJam";
                  email = "git@plumj.am";
                  github = "plumj-am";
                  githubId = 154843404;
                }
              ];
            };
          };
        in
        {
          inherit jj-stack;
          default = jj-stack;
        }
      );

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = getExe self.packages.${system}.jj-stack;
          meta = {
            inherit mainProgram;
            inherit (pyproject.project) description;
          };
        };
      });

      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.mkShell {
            packages = [
              pkgs.git
              pkgs.gh
              pkgs.jujutsu
              pkgs.just
              pkgs.python314
              pkgs.python314Packages.uv
            ];
          };
        }
      );

      checks = forAllSystems (system: {
        inherit (self.packages.${system}) jj-stack;
      });
    };
}
