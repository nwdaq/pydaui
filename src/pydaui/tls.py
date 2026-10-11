# SPDX-License-Identifier: GPL-3.0-or-later
#
# TLS context for the HTTPS connections
#
# Copyright (c) 2026, Marek Koza (qyx@krtko.org)
# All rights reserved.

"""The TLS context verifying the servers of the HTTPS connections, e.g. the plumCore repositories.

Python's default context trusts the CA certificates OpenSSL finds where it was built to look for them. On
Android there are none there, the system keeps them in a directory of its own, one PEM file per certificate
named by the old OpenSSL subject hash OpenSSL does not look up any more. They are loaded one by one then.
"""

import functools
import logging
import os
import ssl

#: The directories of the system CA certificates on Android, the APEX one from Android 14 on.
ANDROID_CA_DIRS = ('/apex/com.android.conscrypt/cacerts', '/system/etc/security/cacerts')

logger = logging.getLogger(__name__)


@functools.cache
def context():
	"""The TLS context for the HTTPS connections, created once."""
	result = ssl.create_default_context()
	paths = ssl.get_default_verify_paths()
	# OpenSSL finds its CA certificates, e.g. on a desktop.
	if paths.cafile or paths.capath:
		return result
	for directory in ANDROID_CA_DIRS:
		if not os.path.isdir(directory):
			continue
		loaded = 0
		for name in sorted(os.listdir(directory)):
			try:
				result.load_verify_locations(cafile=os.path.join(directory, name))
				loaded += 1
			except (OSError, ssl.SSLError) as e:
				logger.debug(f'cannot load the CA certificate {name}: {e}')
		logger.info(f'{loaded} system CA certificate(s) loaded from {directory}')
		return result
	logger.warning('no CA certificates found, the HTTPS servers cannot be verified')
	return result
