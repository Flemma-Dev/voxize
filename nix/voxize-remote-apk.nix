{
  lib,
  stdenvNoCC,
  androidenv,
  jdk17_headless,
  zip,
  writeShellScriptBin,
  symlinkJoin,
  version,
}:
# Voxize Remote's Android client (../android), unsigned, and the tools scripts/release-apk.sh signs it with.
# No Gradle: aapt2 compiles the resources and links them with the manifest, javac and d8 compile, zipalign aligns.
let
  minSdk = 30;
  platformVersion = "35";
  buildToolsVersion = "37.0.0";
  # Android wants a growing integer for every release: 0.4.1 → 401, 1.2.3 → 10203.
  versionCode = lib.foldl' (code: part: code * 100 + lib.toInt part) 0 (lib.splitVersion version);

  # The Android SDK's licence, accepted for this build only.
  android = (androidenv.override { licenseAccepted = true; }).composeAndroidPackages {
    platformVersions = [ platformVersion ];
    buildToolsVersions = [ buildToolsVersion ];
    includeCmake = false;
    includeEmulator = false;
    includeNDK = false;
    includeSources = false;
    includeSystemImages = false;
  };
  sdk = "${android.androidsdk}/libexec/android-sdk";
  buildTools = "${sdk}/build-tools/${buildToolsVersion}";
  androidJar = "${sdk}/platforms/android-${platformVersion}/android.jar";
in
{
  apk = stdenvNoCC.mkDerivation {
    pname = "voxize-remote-apk";
    inherit version;
    # The manifest, resources and sources; android/tests holds Java mains for the Python tests, never in the app.
    src = lib.fileset.toSource {
      root = ../android;
      fileset = lib.fileset.unions [
        ../android/AndroidManifest.xml
        ../android/res
        ../android/src
      ];
    };
    nativeBuildInputs = [
      jdk17_headless
      zip
    ];
    buildPhase = ''
      runHook preBuild
      ${buildTools}/aapt2 compile --dir res -o resources.zip
      mkdir generated classes dex
      ${buildTools}/aapt2 link -o base.apk -I ${androidJar} --manifest AndroidManifest.xml --java generated \
        --min-sdk-version ${toString minSdk} --target-sdk-version ${platformVersion} \
        --version-code ${toString versionCode} --version-name ${version} resources.zip
      javac -encoding UTF-8 -source 8 -target 8 -Xlint:-options \
        -bootclasspath ${androidJar}:${buildTools}/core-lambda-stubs.jar -d classes \
        $(find src generated -name '*.java')
      ${buildTools}/d8 --release --min-api ${toString minSdk} --lib ${androidJar} --output dex \
        $(find classes -name '*.class')
      cp base.apk unaligned.apk
      (cd dex && zip -q ../unaligned.apk classes.dex)
      ${buildTools}/zipalign -f 4 unaligned.apk aligned.apk
      runHook postBuild
    '';
    installPhase = ''
      install -Dm644 aligned.apk $out/voxize-remote-unsigned.apk
    '';
    passthru = {
      inherit versionCode;
      packageName = "dev.flemma.voxize.remote";
    };
  };

  # For scripts/release-apk.sh: apksigner (it needs java on PATH) and keytool.
  tools = symlinkJoin {
    name = "voxize-remote-tools";
    paths = [
      (writeShellScriptBin "apksigner" ''
        PATH=${jdk17_headless}/bin:$PATH exec ${buildTools}/apksigner "$@"
      '')
      (writeShellScriptBin "keytool" ''
        exec ${jdk17_headless}/bin/keytool "$@"
      '')
    ];
  };
}
