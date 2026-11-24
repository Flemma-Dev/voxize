#!/usr/bin/env bash
# Builds and signs the Voxize Remote client for a release: dist/voxize-remote-VERSION.apk and its .sha256, for the
# GitHub release of the same tag (gh release upload vVERSION dist/voxize-remote-VERSION.apk*).
#
# The release key lives in android/signing/ (gitignored) and is made on first use. Losing it costs one reinstall:
# `voxize-remote setup-client` replaces a client signed with another key, and the client keeps nothing but its pairing.
set -euo pipefail

cd "$(dirname "$0")/.."
version=$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -n 1)
signing=android/signing
apk=dist/voxize-remote-$version.apk

tools=$(nix build --no-link --print-out-paths "path:$PWD#voxize-remote-tools")
unsigned=$(nix build --no-link --print-out-paths "path:$PWD#voxize-remote-apk")/voxize-remote-unsigned.apk

mkdir -p "$signing" dist
chmod 700 "$signing"
if [[ ! -f $signing/release.p12 ]]; then
	echo "No release key yet: making android/signing/release.p12."
	(
		umask 077
		od -An -N24 -tx1 /dev/urandom | tr -d ' \n' >"$signing/password"
	)
	"$tools/bin/keytool" -genkeypair -keystore "$signing/release.p12" -storetype PKCS12 \
		-storepass:file "$signing/password" -alias voxize-remote -keyalg RSA -keysize 3072 -validity 10000 \
		-dname "CN=Voxize Remote"
	chmod 600 "$signing/release.p12"
fi

"$tools/bin/apksigner" sign --ks "$signing/release.p12" --ks-pass "file:$signing/password" \
	--ks-key-alias voxize-remote --out "$apk" "$unsigned"
rm -f "$apk.idsig"
"$tools/bin/apksigner" verify "$apk"
(cd dist && sha256sum "voxize-remote-$version.apk" >"voxize-remote-$version.apk.sha256")
echo "Signed: $apk"
cat "$apk.sha256"
