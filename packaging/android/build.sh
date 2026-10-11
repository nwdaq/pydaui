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

PYSIDE_VERSION=6.10.3
QT_VERSION=6.10.3
ARCH=${ARCH:-aarch64}
# The last p4a commit building Python 3.11 needed by the cp311 PySide6 wheels, later ones build
# 3.14. Releases up to v2024.01.21 have a Qt bootstrap not working with Qt 6.7 and later.
P4A_REPO=https://github.com/kivy/python-for-android.git
P4A_COMMIT=c02cf781a0d3d232eb4ffbeaf580cc82cf2bd65f

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
STAGE=$ROOT/build/android
WHEELS=$ROOT/build/android-wheels
P4A=$ROOT/build/python-for-android

# Pure Python dependencies installed by python-for-android, keep in sync with pdm.lock
REQUIREMENTS=(
	superqt==0.7.3 qtpy==2.4.3 packaging==26.3 pygments==2.21.0 typing-extensions==4.16.0
	fonticon-materialdesignicons7==7.2.96 cbor2==5.6.4 colorama==0.4.6
	humanize==4.11.0 pyelftools==0.33 pyserial==3.5 liblzma openssl
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

# python-for-android patched:
# - Its Qt bootstrap activity does not pass the activity results on to Qt, the file dialogs wait for them
#   forever otherwise.
# - The activity style sets no system bar colors, the bars stay black. Qt asks for dark icons in the light
#   mode, they are not visible then. The colors are in packaging/android/res, added below.
[ -d "$P4A/.git" ] || git clone --filter=blob:none "$P4A_REPO" "$P4A"
git -C "$P4A" checkout -f -q "$P4A_COMMIT"
python - "$P4A/pythonforandroid/bootstraps/qt/build" <<'EOF'
import os, sys
build = sys.argv[1]

def patch(path, old, new):
	path = os.path.join(build, path)
	with open(path) as f:
		source = f.read()
	assert source.count(old) == 1, f'{old!r} not found in {path}'
	with open(path, 'w') as f:
		f.write(source.replace(old, new))

method = 'protected void onActivityResult(int requestCode, int resultCode, Intent intent) {\n'
patch('src/main/java/org/kivy/android/PythonActivity.java', method,
      method + '        super.onActivityResult(requestCode, resultCode, intent);\n')
no_title = '<item name="android:windowNoTitle">true</item>\n'
patch('templates/strings.tmpl.xml', no_title,
      no_title + '        <item name="android:statusBarColor">@color/pydaui_status_bar</item>\n'
                 '        <item name="android:navigationBarColor">@color/pydaui_navigation_bar</item>\n')
EOF

# --init generates the recipes, the jars and buildozer.spec without building. The spec is
# patched below and buildozer is run directly, a regular run would regenerate the spec.
cd "$STAGE"
pyside6-android-deploy --init --keep-deployment-files --force --verbose --name pydaui \
	--wheel-pyside "$WHEELS/PySide6-$PYSIDE_VERSION-$QT_VERSION-cp311-cp311-android_$ARCH.whl" \
	--wheel-shiboken "$WHEELS/shiboken6-$PYSIDE_VERSION-$QT_VERSION-cp311-cp311-android_$ARCH.whl"

python - buildozer.spec "$P4A" "$ROOT/packaging/android/res" "${REQUIREMENTS[@]}" <<'EOF'
import configparser, sys
path, p4a, res, *requirements = sys.argv[1:]
spec = configparser.ConfigParser(interpolation=None, comment_prefixes='#')
spec.optionxform = str
spec.read(path)
app = spec['app']
app['requirements'] += ',' + ','.join(requirements)
app['source.include_exts'] = 'py,png,jpg,svg,json,qml,js'
permissions = [p.strip() for p in app.get('android.permissions', '').split(',') if p.strip()]
# BLE needs the location up to Android 11, BLUETOOTH_SCAN and BLUETOOTH_CONNECT from Android 12
for p in ['android.permission.INTERNET', 'android.permission.ACCESS_NETWORK_STATE',
          'android.permission.BLUETOOTH', 'android.permission.BLUETOOTH_ADMIN',
          'android.permission.BLUETOOTH_SCAN', 'android.permission.BLUETOOTH_CONNECT',
          'android.permission.ACCESS_FINE_LOCATION', 'android.permission.ACCESS_COARSE_LOCATION']:
	if p not in permissions:
		permissions.append(p)
app['android.permissions'] = ','.join(permissions)
app['android.minapi'] = '28'
app['android.api'] = '34'
app['android.accept_sdk_license'] = 'True'
app['p4a.source_dir'] = p4a
# Merged into the res directory of the APK
app['android.add_resources'] = res
spec['buildozer']['log_level'] = '2'
with open(path, 'w') as f:
	spec.write(f)
EOF
cat buildozer.spec

python -m buildozer android debug
