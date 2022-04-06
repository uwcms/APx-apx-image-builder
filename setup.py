import subprocess
import time

from setuptools import find_packages, setup

# We need to make the usual git describe PEP440 compatable.
git_ver = subprocess.check_output(['git', 'describe']).decode('utf8').strip().lstrip('v')
split_ver = git_ver.lstrip('v').replace('-', '.').split('.')
if split_ver[-1].startswith('g'):
	# Not a tagged release.
	# Rather than keep the .gHASH, we'll use a build timestamp.
	# This will cover dirty builds too.
	split_ver[-1] = str(time.time())
pep440_ver = '.'.join(split_ver)
setup(version=pep440_ver, packages=find_packages())
