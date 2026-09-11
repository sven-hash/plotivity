#!/usr/bin/env bash
# Builds "Run Map.app" and a .dmg for each architecture.
#   ./packaging/build_mac.sh            -> arm64 (Apple Silicon) + x86_64 (Intel, via Rosetta)
#   ./packaging/build_mac.sh arm64      -> just one
set -euo pipefail
cd "$(dirname "$0")/.."
ARCHES=("${@:-arm64 x86_64}")
[ $# -eq 0 ] && ARCHES=(arm64 x86_64)
VERSION=$(grep -m1 '^version' pyproject.toml | sed 's/.*"\(.*\)"/\1/')

for ARCH in "${ARCHES[@]}"; do
  echo "=== Building for $ARCH ==="
  VENV=".venv-build-$ARCH"
  if [ "$ARCH" = "x86_64" ]; then
    PY="cpython-3.12-macos-x86_64-none"; RUN="arch -x86_64"
  else
    PY="cpython-3.12-macos-aarch64-none"; RUN=""
  fi
  uv python install "$PY" >/dev/null
  UV_PROJECT_ENVIRONMENT="$VENV" uv sync --python "$PY" --group build --no-dev --quiet
  $RUN "$VENV/bin/python" -c "import platform; print('python arch:', platform.machine())"

  rm -rf "build/pyi-$ARCH" "dist/$ARCH"
  $RUN "$VENV/bin/pyinstaller" --noconfirm --windowed --name "Run Map" \
    --icon "$PWD/packaging/icon.icns" \
    --osx-bundle-identifier local.runmap.app \
    --add-data "$PWD/ui.html:." \
    --collect-data folium --collect-data branca --collect-data xyzservices \
    --collect-all curl_cffi --collect-data ua_generator --collect-data certifi \
    --hidden-import garminconnect --hidden-import fitdecode \
    --workpath "build/pyi-$ARCH" --distpath "dist/$ARCH" --specpath "build" \
    app.py

  APP="dist/$ARCH/Run Map.app"
  /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$APP/Contents/Info.plist" || true
  /usr/libexec/PlistBuddy -c "Add :NSHighResolutionCapable bool true" "$APP/Contents/Info.plist" 2>/dev/null || true
  codesign --force --deep --sign - "$APP"   # ad-hoc signature (no developer account)

  STAGE="dist/dmg-$ARCH"; rm -rf "$STAGE"; mkdir -p "$STAGE"
  cp -R "$APP" "$STAGE/"; ln -s /Applications "$STAGE/Applications"
  LABEL=$([ "$ARCH" = "arm64" ] && echo "AppleSilicon" || echo "Intel")
  DMG="dist/RunMap-$VERSION-$LABEL.dmg"; rm -f "$DMG"
  hdiutil create -volname "Run Map" -srcfolder "$STAGE" -ov -format UDZO -quiet "$DMG"
  rm -rf "$STAGE"
  echo "=== $DMG ($(du -h "$DMG" | cut -f1)) ==="
done
