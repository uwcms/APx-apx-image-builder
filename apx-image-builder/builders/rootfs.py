import argparse
import shutil
import subprocess
import textwrap
from typing import List, Optional

from . import base


class RootfsBuilder(base.BaseBuilder):
	NAME: str = 'rootfs'
	statefile: Optional[base.JSONStateFile] = None
	makeflags: List[str]

	def prepare_argparse(self, group: argparse._ArgumentGroup) -> None:
		group.description = textwrap.dedent(
		    '''
			Build rootfs using buildroot.

			Stages available:
			fetch: Download or copy buildroot sources.
			prepare: Extract sources.
			(nconfig): Run `make nconfig`
			build: Build the rootfs
			'''
		).strip()

	def instantiate_stages(self) -> None:
		super().instantiate_stages()
		self.STAGES['clean'] = base.BypassableStage(
		    self, 'clean', self.check, self.clean, include_in_all=False, extract_bypass=False
		)
		self.STAGES['fetch'] = base.BypassableStage(
		    self,
		    'fetch',
		    self.check,
		    self.fetch,
		    after=[self.NAME + ':distclean', self.NAME + ':clean'],
		    extract_bypass=False
		)
		self.STAGES['prepare'] = base.BypassableStage(
		    self, 'prepare', self.check, self.prepare, requires=[self.NAME + ':fetch'], extract_bypass=False
		)
		self.STAGES['nconfig'] = base.BypassableStage(
		    self,
		    'nconfig',
		    self.check,
		    self.nconfig,
		    requires=[self.NAME + ':prepare'],
		    before=[self.NAME + ':build'],
		    include_in_all=False,
		    extract_bypass=False
		)
		self.STAGES['build'] = base.BypassableStage(
		    self, 'build', self.check, self.build, requires=[self.NAME + ':prepare']
		)

	def check(self, STAGE: base.Stage) -> bool:
		check_ok: bool = True
		if STAGE.name in (
		    'fetch', 'prepare'
		) and 'buildroot_version' not in self.BUILDER_CONFIG and 'buildroot_sourceurl' not in self.BUILDER_CONFIG:
			STAGE.logger.error(
			    'Please set a `buildroot_version` or `buildroot_sourceurl` (file://... is valid) in the configuration for the "rootfs" builder.'
			)
			check_ok = False
		return check_ok

	def fetch(self, STAGE: base.Stage) -> None:
		statefile = base.JSONStateFile(self.PATHS.build / '.state.json')
		sourceurl: Optional[str] = self.BUILDER_CONFIG.get('buildroot_sourceurl', None)
		if sourceurl is None:
			sourceurl = 'https://buildroot.org/downloads/buildroot-{version}.tar.gz'.format(
			    version=self.BUILDER_CONFIG['buildroot_version']
			)
		if base.import_source(STAGE, sourceurl, 'buildroot.tar.gz'):
			with statefile as state:
				state['tree_ready'] = False

	def prepare(self, STAGE: base.Stage) -> None:
		statefile = base.JSONStateFile(self.PATHS.build / '.state.json')
		brdir = self.PATHS.build / 'buildroot'
		patcher = base.Patcher(self.PATHS.build / 'patches')
		patches = self.BUILDER_CONFIG.get('patches', [])
		if patcher.import_patches(STAGE, patches):
			with statefile as state:
				state['tree_ready'] = False
		if statefile.state.get('tree_ready', False):
			STAGE.logger.info('The buildroot source tree has already been extracted.  Skipping.')
		else:
			base.untar(STAGE, self.PATHS.build / 'buildroot.tar.gz', self.PATHS.build / 'buildroot')
			patcher.apply(STAGE, self.PATHS.build / 'buildroot')
			with statefile as state:
				state['tree_ready'] = True

		if base.import_source(STAGE, 'rootfs.config', self.PATHS.build / '.config', ignore_timestamps=True):
			# We need to use a two stage load here because we actually do update
			# the imported source, and don't want needless imports to interfere
			# with `make` caching.
			user_config_hash = base.hash_file('sha256', open(self.PATHS.build / '.config', 'rb'))
			if statefile.state.get('user_config_hash', '') != user_config_hash:
				base.copyfile(self.PATHS.build / '.config', brdir / '.config')
				with statefile as state:
					state['user_config_hash'] = user_config_hash

		# Fallback check required when the tree is regenerated with an unchanged config.
		if (self.PATHS.build / '.config').exists() and not (brdir / '.config').exists():
			base.copyfile(self.PATHS.build / '.config', brdir / '.config')

		# Provide our config as an output.
		base.copyfile(brdir / '.config', self.PATHS.output / 'rootfs.config')

	def nconfig(self, STAGE: base.Stage) -> None:
		brdir = self.PATHS.build / 'rootfs'

		STAGE.logger.info('Running `nconfig`...')
		try:
			base.run(STAGE, ['make', 'nconfig'], cwd=brdir, stdin=None, stdout=None, stderr=None)
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, 'rootfs `nconfig` returned with an error')
		STAGE.logger.info('Finished `nconfig`.')

		# Provide our kernel config as an output.
		base.copyfile(brdir / '.config', self.PATHS.output / 'rootfs.config')
		STAGE.logger.warning(
		    'The output file `rootfs.config` has been created.  You must manually copy this to your sources directory.'
		)

	def build(self, STAGE: base.Stage) -> None:
		brdir = self.PATHS.build / 'buildroot'
		STAGE.logger.info('Running `make`...')
		try:
			base.run(STAGE, ['make'], cwd=brdir)
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, '`make` returned with an error')

		# Provide buildroot image as an output.
		for image_name in ('rootfs.tar.gz', 'rootfs.cpio', 'rootfs.cpio.uboot'):
			image = self.PATHS.build / 'buildroot/output/images' / image_name
			if not image.exists():
				base.fail(STAGE.logger, image_name + ' not found after build.')
			base.copyfile(image, self.PATHS.output / image_name)

	def clean(self, STAGE: base.Stage) -> None:
		STAGE.logger.info('Running `clean`...')
		try:
			base.run(STAGE, ['make', 'clean'], cwd=self.PATHS.build / 'buildroot')
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, '`clean` returned with an error')
		STAGE.logger.info('Finished `clean`.')
		STAGE.logger.info('Deleting outputs.')
		shutil.rmtree(self.PATHS.output, ignore_errors=True)
		self.PATHS.output.mkdir(parents=True, exist_ok=True)
