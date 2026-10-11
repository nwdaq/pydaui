#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Build an Android APK with pyside6-android-deploy (buildozer and python-for-android)
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.
#
# Needs Python 3.11 with PySide6, buildozer and cython installed, see the Android workflow.
# The application is staged into build/android with main.py as the entry point. Only the pure
# Python transports work, the native backends (SimplePyBLE, pyocd, pyserial) are left out.

set -euo pipefail

PYSIDE_VERSION=6.8.2.1
QT_VERSION=6.8.2
ARCH=${ARCH:-aarch64}
# p4a develop has moved past Python 3.11 required by the cp311 PySide6 wheels
P4A_BRANCH=v2024.01.21

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
STAGE=$ROOT/build/android
WHEELS=$ROOT/build/android-wheels

# Pure Python dependencies installed by python-for-android, keep in sync with pdm.lock
REQUIREMENTS=(
	superqt==0.7.3 qtpy==2.4.3 pygments==2.21.0 typing-extensions==4.16.0
	fonticon-materialdesignicons7==7.2.96 cbor2==5.6.4 colorama==0.4.6
	humanize==4.11.0 pyelftools==0.33 liblzma
)

rm -rf "$STAGE"
mkdir -p "$STAGE" "$WHEELS"

# The application and pynbus2, without the CMSIS packs used for SWD only
cp -r "$ROOT/src/pydaui" "$STAGE/"
rm -f "$STAGE"/pydaui/*.pack
find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +
tar -xzf "$ROOT"/vendor/pynbus2-*.tar.gz -C "$STAGE" --strip-components=2 --wildcards '*/src/pynbus2'

python - "$ROOT" "$STAGE/pydaui" <<'EOF'
import datetime, json, os, subprocess, sys
root, dest = sys.argv[1:]
sys.path.insert(0, os.path.join(root, 'src'))
from pydaui import changelog
version = subprocess.check_output(['git', 'describe', '--tags', '--always', '--dirty'], cwd=root, text=True).strip()
date = datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%d')
with open(os.path.join(dest, 'build.json'), 'w') as f:
	json.dump({'version': version, 'date': date}, f)
changelog.write(os.path.join(dest, 'changelog.json'), root)
EOF

cat > "$STAGE/main.py" <<'EOF'
from pydaui.__main__ import main

main()
EOF

# Catch syntax not supported by the Python 3.11 on the device
python -m compileall -q "$STAGE" >/dev/null

for wheel in PySide6 shiboken6; do
	file=$wheel-$PYSIDE_VERSION-$QT_VERSION-cp311-cp311-android_$ARCH.whl
	[ -f "$WHEELS/$file" ] || curl -fL -o "$WHEELS/$file" \
		"https://download.qt.io/official_releases/QtForPython/${wheel,,}/$file"
done

# --init generates the recipes, the jars and buildozer.spec without building. The spec is
# patched below and buildozer is run directly, a regular run would regenerate the spec.
cd "$STAGE"
pyside6-android-deploy --init --keep-deployment-files --force --verbose --name pydaui \
	--wheel-pyside "$WHEELS/PySide6-$PYSIDE_VERSION-$QT_VERSION-cp311-cp311-android_$ARCH.whl" \
	--wheel-shiboken "$WHEELS/shiboken6-$PYSIDE_VERSION-$QT_VERSION-cp311-cp311-android_$ARCH.whl"

python - buildozer.spec "$P4A_BRANCH" "${REQUIREMENTS[@]}" <<'EOF'
import configparser, sys
path, branch, *requirements = sys.argv[1:]
spec = configparser.ConfigParser(interpolation=None, comment_prefixes='#')
spec.optionxform = str
spec.read(path)
app = spec['app']
app['requirements'] += ',' + ','.join(requirements)
app['source.include_exts'] = 'py,png,jpg,svg,json,qml,js'
app['android.permissions'] = ','.join(p for p in [app.get('android.permissions', ''),
	'android.permission.INTERNET', 'android.permission.ACCESS_NETWORK_STATE'] if p)
app['android.minapi'] = '28'
app['android.api'] = '34'
app['android.accept_sdk_license'] = 'True'
app['p4a.branch'] = branch
spec['buildozer']['log_level'] = '2'
with open(path, 'w') as f:
	spec.write(f)
EOF
cat buildozer.spec

python -m buildozer android debug
