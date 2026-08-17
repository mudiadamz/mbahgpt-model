#!/bin/sh
# Download the Ionic assets the web UI needs into vendor/.
#
# They are not committed: 1.3 MB of build output does not belong in the repo.
# The version and hash below are pinned — this script runs code that will later
# execute in your browser, so it refuses to install anything that does not match.
#
#   ./fetch-vendor.sh          install if missing
#   ./fetch-vendor.sh --force  reinstall
set -eu

VERSION="8.8.18"
TARBALL="https://registry.npmjs.org/@ionic/core/-/core-${VERSION}.tgz"
INTEGRITY="sha512-QRFqi6gMSTk89FMjtoDx3p6e2dD7mYxqYzS0+Hl+yjxnoWoNFluWKM9rnGEEkdvpY7bF1pt6CI5IwMt+vjYQWg=="

HERE=$(cd "$(dirname "$0")" && pwd)
DEST="$HERE/vendor/ionic"

if [ "${1:-}" != "--force" ] && [ -f "$DEST/ionic.esm.js" ]; then
    echo "Ionic ${VERSION} already present in vendor/ionic (use --force to reinstall)."
    exit 0
fi

command -v curl >/dev/null || { echo "curl is required" >&2; exit 1; }
command -v tar  >/dev/null || { echo "tar is required" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "Downloading @ionic/core ${VERSION}..."
curl -fsSL -o "$TMP/core.tgz" "$TARBALL"

# Compare against the registry's published sha512, base64 encoded like npm does.
if command -v python3 >/dev/null; then
    GOT=$(python3 -c "import base64,hashlib,sys
d=open(sys.argv[1],'rb').read()
print('sha512-'+base64.b64encode(hashlib.sha512(d).digest()).decode())" "$TMP/core.tgz")
elif command -v openssl >/dev/null; then
    GOT="sha512-$(openssl dgst -sha512 -binary "$TMP/core.tgz" | openssl base64 -A)"
else
    echo "need python3 or openssl to verify the download" >&2
    exit 1
fi

if [ "$GOT" != "$INTEGRITY" ]; then
    echo "integrity check FAILED — refusing to install" >&2
    echo "  expected $INTEGRITY" >&2
    echo "  got      $GOT" >&2
    exit 1
fi
echo "Integrity verified."

tar xzf "$TMP/core.tgz" -C "$TMP"
rm -rf "$DEST"
mkdir -p "$DEST/css"
# Only the runtime is needed: component chunks, the loader, one CSS bundle.
cp "$TMP"/package/dist/ionic/*.js "$DEST/"
cp "$TMP"/package/css/ionic.bundle.css "$DEST/css/"
cp "$TMP"/package/LICENSE "$DEST/" 2>/dev/null || true

echo "Installed Ionic ${VERSION} into vendor/ionic ($(ls "$DEST"/*.js | wc -l | tr -d ' ') files)."
