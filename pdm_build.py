# SPDX-License-Identifier: GPL-3.0-or-later
#
# pdm-backend build hook stamping the version and the build date into the package
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

import datetime
import json
import os


def pdm_build_initialize(context):
	# Editable installs run from the source tree, they have no build.
	if context.target == 'editable':
		return
	# A wheel built from an sdist keeps the build information of the sdist.
	if (context.root / 'src' / 'pydaui' / 'build.json').exists():
		return
	# Honour SOURCE_DATE_EPOCH for reproducible builds.
	epoch = os.environ.get('SOURCE_DATE_EPOCH')
	date = datetime.datetime.fromtimestamp(int(epoch), datetime.UTC) if epoch else datetime.datetime.now(datetime.UTC)
	package = 'src/pydaui' if context.target == 'sdist' else 'pydaui'
	path = context.ensure_build_dir() / package
	path.mkdir(parents=True, exist_ok=True)
	(path / 'build.json').write_text(json.dumps({
		'version': context.config.metadata['version'],
		'date': date.strftime('%Y-%m-%d'),
	}))
