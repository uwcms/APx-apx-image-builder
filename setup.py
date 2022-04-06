import subprocess
import time

from setuptools import find_packages, setup

# We need to make the usual git describe PEP440 compatable.
git_ver = subprocess.check_output(['git', 'describe', '--long', '--dirty']).decode('utf8').strip().lstrip('v')
add_buildstamp = False
if git_ver.endswith('-dirty'):
	add_buildstamp = True
	git_ver = git_ver[:-len('-dirty')]
tag_ver, plus_commits, ghash = git_ver.rsplit('-', 2)
if plus_commits != '0':
	add_buildstamp = True
pep440_ver = tag_ver
if add_buildstamp:
	pep440_ver += '.' + plus_commits
	pep440_ver += '.' + str(int(time.time()))

setup(version=pep440_ver, packages=find_packages())
