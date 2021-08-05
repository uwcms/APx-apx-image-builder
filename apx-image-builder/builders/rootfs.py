import argparse
import logging
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

from . import base


class RootfsBuilder(base.BaseBuilder):
	NAME: str = 'rootfs'
	statefile: Optional[base.JSONStateFile] = None
	makeflags: List[str]

	@classmethod
	def prepare_argparse(cls, group: argparse._ArgumentGroup) -> None:
		group.description = '''
Build rootfs using buildroot.

Stages available:
  fetch: Download or copy buildroot sources.
  prepare: Extract sources.
  (nconfig): Run `make nconfig`
  build: Build the rootfs
'''.strip()

	def instantiate_stages(self) -> None:
		super().instantiate_stages()
		self.STAGES['clean'] = base.Stage(self, 'clean', self.check, self.clean, include_in_all=False)
		self.STAGES['fetch'] = base.Stage(
		    self, 'fetch', self.check, self.fetch, after=[self.NAME + ':distclean', self.NAME + ':clean']
		)
		self.STAGES['prepare'] = base.Stage(self, 'prepare', self.check, self.prepare, requires=[self.NAME + ':fetch'])
		self.STAGES['nconfig'] = base.Stage(
		    self,
		    'nconfig',
		    self.check,
		    self.nconfig,
		    requires=[self.NAME + ':prepare'],
		    before=[self.NAME + ':build'],
		    include_in_all=False
		)
		self.STAGES['build'] = base.Stage(self, 'build', self.check, self.build, requires=[self.NAME + ':prepare'])

	def check(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> bool:
		if base.check_bypass(STAGE, PATHS, LOGGER, extract=False):
			return True  # We're bypassed.

		check_ok: bool = True
		if STAGE.name in (
		    'fetch', 'prepare'
		) and 'buildroot_version' not in self.BUILDER_CONFIG and 'buildroot_sourceurl' not in self.BUILDER_CONFIG:
			LOGGER.error(
			    'Please set a `buildroot_version` or `buildroot_sourceurl` (file://... is valid) in the configuration for the "rootfs" builder.'
			)
			check_ok = False
		return check_ok

	def fetch(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
			return  # We're bypassed.

		statefile = base.JSONStateFile(PATHS.build / '.state.json')
		sourceurl: Optional[str] = self.BUILDER_CONFIG.get('buildroot_sourceurl', None)
		if sourceurl is None:
			sourceurl = 'https://buildroot.org/downloads/buildroot-{version}.tar.gz'.format(
			    version=self.BUILDER_CONFIG['buildroot_version']
			)
		if base.import_source(PATHS, LOGGER, self.ARGS, sourceurl, 'buildroot.tar.gz'):
			with statefile as state:
				state['tree_ready'] = False

	def prepare(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
			return  # We're bypassed.

		statefile = base.JSONStateFile(PATHS.build / '.state.json')
		brdir = PATHS.build / 'buildroot'
		patcher = base.Patcher(PATHS.build / 'patches')
		patches = self.BUILDER_CONFIG.get('patches', [])
		if patcher.import_patches(PATHS, LOGGER, self.ARGS, patches):
			with statefile as state:
				state['tree_ready'] = False
		if statefile.state.get('tree_ready', False):
			LOGGER.info('The buildroot source tree has already been extracted.  Skipping.')
		else:
			base.untar(PATHS, LOGGER, PATHS.build / 'buildroot.tar.gz', PATHS.build / 'buildroot')
			patcher.apply(PATHS, LOGGER, PATHS.build / 'buildroot')
			with statefile as state:
				state['tree_ready'] = True

		if base.import_source(PATHS, LOGGER, self.ARGS, 'rootfs.config', PATHS.build / '.config',
		                      ignore_timestamps=True):
			# We need to use a two stage load here because we actually do update
			# the imported source, and don't want needless imports to interfere
			# with `make` caching.
			user_config_hash = base.hash_file('sha256', open(PATHS.build / '.config', 'rb'))
			if statefile.state.get('user_config_hash', '') != user_config_hash:
				shutil.copyfile(PATHS.build / '.config', brdir / '.config')
				with statefile as state:
					state['user_config_hash'] = user_config_hash

		# Provide our config as an output.
		shutil.copyfile(brdir / '.config', PATHS.output / 'rootfs.config')

	def nconfig(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
			return  # We're bypassed.

		brdir = PATHS.build / 'rootfs'

		LOGGER.info('Running `nconfig`...')
		try:
			base.run(PATHS, LOGGER, ['make', 'nconfig'], cwd=brdir, stdin=None, stdout=None, stderr=None)
		except subprocess.CalledProcessError:
			base.fail(LOGGER, 'rootfs `nconfig` returned with an error')
		LOGGER.info('Finished `nconfig`.')

		# Provide our kernel config as an output.
		shutil.copyfile(brdir / '.config', PATHS.output / 'rootfs.config')
		LOGGER.warning(
		    'The output file `rootfs.config` has been created.  You must manually copy this to your sources directory.'
		)

	def build(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
			return  # We're bypassed.

		brdir = PATHS.build / 'buildroot'
		LOGGER.info('Running `make`...')
		try:
			base.run(PATHS, LOGGER, ['make'], cwd=brdir)
		except subprocess.CalledProcessError:
			base.fail(LOGGER, '`make` returned with an error')

		# Provide buildroot image as an output.
		for image_name in ('rootfs.tar.gz', 'rootfs.cpio'):
			image = PATHS.build / 'buildroot/output/images' / image_name
			if not image.exists():
				base.fail(LOGGER, image_name + ' not found after build.')
			shutil.copyfile(image, PATHS.output / image_name)

	def clean(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER, extract=False):
			return  # We're bypassed.

		LOGGER.info('Running `clean`...')
		try:
			base.run(PATHS, LOGGER, ['make', 'clean'], cwd=PATHS.build / 'buildroot')
		except subprocess.CalledProcessError:
			base.fail(LOGGER, '`clean` returned with an error')
		LOGGER.info('Finished `clean`.')
		LOGGER.info('Deleting outputs.')
		shutil.rmtree(PATHS.output, ignore_errors=True)
		PATHS.output.mkdir(parents=True, exist_ok=True)
