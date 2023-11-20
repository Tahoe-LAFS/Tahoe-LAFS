{
  description = "Tahoe-LAFS, free and open decentralized data store";

  nixConfig = {
    # Supply configuration for the build cache updated by our CI system.  This
    # should allow most users to avoid having to build a large number of
    # packages (otherwise necessary due to our Python package overrides).
    extra-substituters = ["https://tahoe-lafs-opensource.cachix.org"];
    extra-trusted-public-keys = ["tahoe-lafs-opensource.cachix.org-1:eIKCHOPJYceJ2gb74l6e0mayuSdXqiavxYeAio0LFGo="];
  };

  inputs = {
    # A couple possible nixpkgs pins.  Ideally these could be selected easily
    # from the command line but there seems to be no syntax/support for that.
    # However, these at least cause certain revisions to be pinned in our lock
    # file where you *can* dig them out - and the CI configuration does.
    #
    # These are really just examples for the time being since neither of these
    # releases contains a package set that is completely compatible with our
    # requirements.  We could decide in the future that supporting multiple
    # releases of NixOS at a time is worthwhile and then pins like these will
    # help us test each of those releases.
    "nixpkgs-22_11" = {
      url = github:NixOS/nixpkgs?ref=nixos-22.11;
    };
    "nixpkgs-23_05" = {
      url = github:NixOS/nixpkgs?ref=nixos-23.05;
    };

    # We depend on a very new python-cryptography which is not yet available
    # from any release branch of nixpkgs.  However, it is contained in a PR
    # currently up for review.  Point our nixpkgs at that for now.
    "nixpkgs-unstable" = {
      url = github:NixOS/nixpkgs?ref=pull/244135/head;
    };

    # Point the default nixpkgs at one of those.  This avoids having getting a
    # _third_ package set involved and gives a way to provide what should be a
    # working experience by default (that is, if nixpkgs doesn't get
    # overridden).
    nixpkgs.follows = "nixpkgs-unstable";

    # Also get flake-utils for simplified multi-system definitions.
    flake-utils = {
      url = github:numtide/flake-utils;
    };

    # And get a helper that lets us easily continue to provide a default.nix.
    flake-compat = {
      url = "github:edolstra/flake-compat";
      flake = false;
    };
  };

  outputs = { self, nixpkgs, flake-utils, ... }:
    {
      # Expose an overlay which adds our version of Tahoe-LAFS to the Python
      # package sets we specify, as well as all of the correct versions of its
      # dependencies.
      #
      # We will also use this to define some other outputs since it gives us
      # the most succinct way to get a working Tahoe-LAFS package.
      overlays.default = import ./nix/overlay.nix;

    } // (flake-utils.lib.eachDefaultSystem (system: let

      # The package set for this system architecture.
      pkgs = import nixpkgs {
        inherit system;
        # And include our Tahoe-LAFS package in that package set.
        overlays = [ self.overlays.default ];
      };

      # pythonVersions :: [string]
      #
      # The version strings for the Python runtimes we'll work with.
      pythonVersions =
        let
          # Match attribute names that look like a Python derivation - CPython
          # or PyPy.  We take care to avoid things like "python-foo" and
          # "python3Full-unittest" though.  We only want things like "pypy38"
          # or "python311".
          nameMatches = name: null != builtins.match "(python|pypy)3[[:digit:]]{0,2}" name;

          # Sometimes an old version is left in the package set as an error
          # saying something like "we remove this".  Make sure we whatever we
          # found by name evaluates without error, too.
          notError = drv: (builtins.tryEval drv).success;
        in
          # Discover all of the Python runtime derivations by inspecting names
          # and filtering out derivations with errors.
          builtins.attrNames (
            pkgs.lib.attrsets.filterAttrs
              (name: drv: nameMatches name && notError drv)
              pkgs
          );

      # defaultPyVersion :: string
      #
      # An element of pythonVersions which we'll use for the default package.
      defaultPyVersion = "python3";

      # pythons :: [derivation]
      #
      # Retrieve the actual Python package for each configured version.  We
      # already applied our overlay to pkgs so our packages will already be
      # available.
      pythons = builtins.map (pyVer: pkgs.${pyVer}) pythonVersions;

      # packageName :: string -> string
      #
      # Construct the Tahoe-LAFS package name for the given Python runtime.
      packageName = pyVersion: "${pyVersion}-tahoe-lafs";

      # string -> string
      #
      # Construct the unit test application name for the given Python runtime.
      unitTestName = pyVersion: "${pyVersion}-unittest";

      # string -> string
      #
      # Construct the integration test application name for the given Python runtime.
      integrationTestName = pyVersion: "${pyVersion}-integration";

      # (string -> a) -> (string -> b) -> string -> attrset a b
      #
      # Make a singleton attribute set from the result of two functions.
      singletonOf = f: g: x: { ${f x} = g x; };

      # [attrset] -> attrset
      #
      # Merge a list of attrset into a single attrset with overlap preferring
      # rightmost values.
      mergeAttrs = pkgs.lib.foldr pkgs.lib.mergeAttrs {};

      # makeRuntimeEnv :: string -> derivation
      #
      # Create a derivation that includes a Python runtime, Tahoe-LAFS, and
      # all of its dependencies.
      makeRuntimeEnv = singletonOf packageName makeRuntimeEnv';
      makeRuntimeEnv' = pyVersion: (pkgs.${pyVersion}.withPackages (ps: with ps;
        [ tahoe-lafs ] ++
        tahoe-lafs.passthru.extras.i2p ++
        tahoe-lafs.passthru.extras.tor
      )).overrideAttrs (old: {
        # By default, withPackages gives us a derivation with a fairly generic
        # name (like "python-env").  Put our name in there for legibility.
        # See the similar override in make*TestEnv.
        name = packageName pyVersion;
      });

      # extraRequirements :: derivation -> [string] -> [derivation]
      #
      # Get a flat list of the additional requirements from the derivation's
      # `passthru.extras` attribute for all of the given extra names.
      extraRequirements = drv: names:
        builtins.concatLists (builtins.map (name: drv.passthru.extras.${name}) names);

      # makeUnitTestEnv :: string -> derivation
      #
      # Create a derivation that includes a Python runtime and all of the
      # Tahoe-LAFS dependencies, but not Tahoe-LAFS itself, which we'll get
      # from the working directory.
      makeUnitTestEnv = pyVersion: (pkgs.${pyVersion}.withPackages (ps:
        ps.tahoe-lafs.passthru.dependencies ++
        (extraRequirements ps.tahoe-lafs ["i2p" "tor" "unittest"])
      )).overrideAttrs (old: {
        # See the similar override in makeRuntimeEnv'.
        name = packageName pyVersion;
      });

      # makeIntegrationTestEnv :: string -> derivation
      #
      # Create a derivation that includes a Python runtime, Tahoe-LAFS, and
      # all of its dependencies.  This is suitable for running the integration
      # tests, which expect to find `tahoe` on PATH.
      makeIntegrationTestEnv = pyVersion: (pkgs.${pyVersion}.withPackages (ps:
        [
          ps.tahoe-lafs

          # chutney is here instead of in the integrationtest extra because
          # it's not properly packaged and pip can't install it.
          ps.chutney

        ] ++
        (extraRequirements ps.tahoe-lafs ["i2p" "tor" "integrationtest"])
      )).overrideAttrs (old: {
        # See the similar override in makeRuntimeEnv'.
        name = packageName pyVersion;
      });

    in {
      # Include a package set with out overlay on it in our own output.  This
      # is mainly a development/debugging convenience as it will expose all of
      # our Python package overrides beneath it.  The magic name
      # "legacyPackages" is copied from nixpkgs and has special support in the
      # nix command line tool.
      legacyPackages = pkgs;

      # The flake's package outputs.  We'll define one version of the package
      # for each version of Python we could find.  We'll also point the
      # flake's "default" package at the derivation corresponding to the
      # default Python version we defined above.  The package consists of a
      # Python environment with Tahoe-LAFS available to it.
      packages =
        mergeAttrs (
          [ { default = self.packages.${system}.${packageName defaultPyVersion}; } ]
          ++ (builtins.map makeRuntimeEnv pythonVersions)
          ++ (builtins.map (singletonOf unitTestName makeUnitTestEnv) pythonVersions)
          ++ (builtins.map (singletonOf integrationTestName makeIntegrationTestEnv) pythonVersions)
        );

      # The flake's app outputs.  We'll define a version of an app for running
      # the test suite for each version of Python we could find.  We'll also
      # define a version of an app for running the "tahoe" command-line
      # entrypoint for each version of Python we could find.
      apps =
        let
          # writeScript :: string -> string -> path
          #
          # Write a shell program to a file so it can be run later.
          #
          # We avoid writeShellApplication here because it has ghc as a
          # dependency but ghc has Python as a dependency and our Python
          # package override triggers a rebuild of ghc and many Haskell
          # packages which takes a looong time.
          writeScript = name: text: "${pkgs.writeShellScript name text}";

          # makeTahoeApp :: string -> attrset
          #
          # A helper function to define the Tahoe-LAFS runtime entrypoint for
          # a certain Python runtime.
          makeTahoeApp = pyVersion: {
            "tahoe-${pyVersion}" = {
              type = "app";
              program =
                writeScript "tahoe"
                  ''
                    ${makeRuntimeEnv' pyVersion}/bin/tahoe "$@"
                  '';
            };
          };

          # makeUnitTestsApp :: string -> attrset
          #
          # A helper function to define the Tahoe-LAFS unit test entrypoint
          # for a certain Python runtime.
          makeUnitTestsApp = pyVersion: {
            "${unitTestName pyVersion}" = {
              type = "app";
              program =
                let
                  #
                  python =
                    "${self.packages.${system}.${unitTestName pyVersion}}/bin/python";
                in
                  writeScript "unit-tests"
                    ''
                    # Make sure there is recent generated-version information
                    # in the tree.
                    ${python} setup.py update_version

                    # Use the closer-to-deterministic Hypothesis profile for
                    # better reproducibility.
                    export TAHOE_LAFS_HYPOTHESIS_PROFILE=ci

                    # Use the source from the working tree for a
                    # development-friendly experience.
                    export PYTHONPATH=$PWD/src

                    # Run the unit tests.
                    if (( $# )) then
                      ${python} -m twisted.trial "$@"
                    else
                      ${python} -m twisted.trial -j$NIX_BUILD_CORES allmydata
                    fi
                  '';
            };
          };

          # makeIntegrationTestsApp :: string -> attrset
          #
          # A helper function to define the Tahoe-LAFS integration test
          # entrypoint for a certain Python runtime.
          makeIntegrationTestsApp = pyVersion: {
            "${integrationTestName pyVersion}" = {
              type = "app";
              program =
                let programDrv =
                      pkgs.writeShellApplication {
                        name = "integration-tests";
                        runtimeInputs = [
                          # Place the integration test package into the
                          # runtime environment so PATH is updated.  The
                          # integration tests require certain tools to be
                          # discoverable that way.
                          self.packages.${system}.${integrationTestName pyVersion}

                          # Also include non-Python dependencies for the
                          # integration test suite.
                          pkgs.tor
                        ];
                        # Disable checking the shell application.  Shell
                        # checking is nice but our Python package overrides
                        # mean a huge number of dependencies must be built
                        # before the check can be run.
                        checkPhase = "";
                        text =
                          ''
                            # Make sure there is recent generated-version
                            # information in the tree.
                            python setup.py update_version

                            # Use the source from the working tree for a
                            # development-friendly experience.
                            export PYTHONPATH=$PWD/src

                            # Chutney wants to write out files to track its
                            # state.  By default it does this alongside its
                            # source code but we put the source in the nix
                            # store so that location isn't writeable.  Give it
                            # somewhere else to write it.
                            export CHUTNEY_DATA_DIR=$PWD/chutney-net

                            # If args are given, use them.
                            if (( $# )) then
                              python -m pytest "$@"
                            else
                              python -m pytest -v --runslow integration
                            fi
                          '';
                      };
                in
                  "${programDrv}/bin/integration-tests";
            };
          };
        in
          # Merge a default app definition with the rest of the apps.
          mergeAttrs (
            [ { default = self.apps.${system}."tahoe-python3"; } ]
            ++ (builtins.map makeUnitTestsApp pythonVersions)
            ++ (builtins.map makeIntegrationTestsApp pythonVersions)
            ++ (builtins.map makeTahoeApp pythonVersions)
          );
    }));
}
