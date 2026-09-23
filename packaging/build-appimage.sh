#!/bin/bash
# Builds Wii-Remote-Control-x86_64.AppImage from the two scripts in the
# repo root. Downloads appimagetool on first run if it isn't present.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

APPIMAGETOOL=appimagetool.AppImage
if [ ! -x "$APPIMAGETOOL" ]; then
    echo "Downloading appimagetool..."
    curl -L -o "$APPIMAGETOOL" \
        https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x "$APPIMAGETOOL"
fi

mkdir -p AppDir/usr/bin
cp ../wiimote_bridge.py ../wiimote_gui.py AppDir/usr/bin/

ARCH=x86_64 ./"$APPIMAGETOOL" AppDir Wii-Remote-Control-x86_64.AppImage

echo "Built packaging/Wii-Remote-Control-x86_64.AppImage"
