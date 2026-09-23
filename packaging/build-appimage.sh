#!/bin/bash
# Builds Wii-Remote-Control-x86_64.AppImage, fully self-contained: it
# bundles its own Python interpreter (with evdev + Tkinter already
# installed) so the AppImage doesn't depend on whatever Python packages
# happen to be on the host. Downloads appimagetool and the portable
# Python build on first run if they aren't present; both are cached here
# for subsequent builds.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

PYTHON_RELEASE=20260901
PYTHON_VERSION=3.12.14

APPIMAGETOOL=appimagetool.AppImage
if [ ! -x "$APPIMAGETOOL" ]; then
    echo "Downloading appimagetool..."
    curl -L -o "$APPIMAGETOOL" \
        https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x "$APPIMAGETOOL"
fi

if [ ! -x AppDir/usr/python/bin/python3 ]; then
    echo "Downloading portable Python ${PYTHON_VERSION}..."
    curl -L -o /tmp/cpython.tar.gz \
        "https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_RELEASE}/cpython-${PYTHON_VERSION}%2B${PYTHON_RELEASE}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
    rm -rf AppDir/usr/python
    mkdir -p AppDir/usr
    tar -xzf /tmp/cpython.tar.gz -C AppDir/usr
    rm -f /tmp/cpython.tar.gz

    echo "Installing evdev into the bundled interpreter..."
    AppDir/usr/python/bin/python3 -m ensurepip --upgrade
    CC=gcc CXX=g++ AppDir/usr/python/bin/python3 -m pip install evdev

    echo "Trimming non-runtime files..."
    rm -rf AppDir/usr/python/include AppDir/usr/python/share/doc AppDir/usr/python/share/man
    rm -rf AppDir/usr/python/lib/python3.12/{test,idlelib,turtledemo,lib2to3,ensurepip,__pycache__}
    rm -rf AppDir/usr/python/lib/python3.12/site-packages/{pip,pip-*,setuptools,setuptools-*,wheel,wheel-*,_distutils_hack}
    find AppDir/usr/python -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    rm -f AppDir/usr/python/bin/{idle3.12,2to3-3.12,pip,pip3,pip3.12,idle3,2to3}

    AppDir/usr/python/bin/python3 -c "import evdev, tkinter" \
        || { echo "Bundled interpreter failed evdev/tkinter smoke test" >&2; exit 1; }
fi

mkdir -p AppDir/usr/bin
cp ../wiimote_bridge.py ../wiimote_gui.py setup-permissions.sh AppDir/usr/bin/

ARCH=x86_64 ./"$APPIMAGETOOL" AppDir Wii-Remote-Control-x86_64.AppImage

echo "Built packaging/Wii-Remote-Control-x86_64.AppImage"
