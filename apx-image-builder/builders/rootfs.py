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
  (defconfig): Run `make defconfig`.
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
		self.STAGES['defconfig'] = base.Stage(
		    self,
		    'defconfig',
		    self.check,
		    self.defconfig,
		    requires=[self.NAME + ':prepare'],
		    before=[self.NAME + ':build', self.NAME + ':nconfig'],
		    include_in_all=False
		)
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
		if STAGE.name in ('fetch', 'prepare'
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
		ubdir = PATHS.build / 'buildroot'
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
				shutil.copyfile(PATHS.build / '.config', ubdir / '.config')
				with statefile as state:
					state['user_config_hash'] = user_config_hash

		# Provide our config as an output.
		shutil.copyfile(ubdir / '.config', PATHS.output / 'rootfs.config')

	def defconfig(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
			return  # We're bypassed.

		zynq_series = self.COMMON_CONFIG.get('zynq_series', '')
		if zynq_series == 'zynq':
			defconfig = 'xilinx_zynq_virt_defconfig'
		elif zynq_series == 'zynqmp':
			defconfig = 'xilinx_zynqmp_virt_defconfig'
		else:
			base.fail(LOGGER, "Unknown zynq_series setting: " + repr(zynq_series))

		ubdir = PATHS.build / 'rootfs'
		LOGGER.info('Running `{defconfig}`...'.format(defconfig=defconfig))
		try:
			base.run(PATHS, LOGGER, ['make', 'CROSS_COMPILE=' + self.cross_compile, defconfig], cwd=ubdir)
		except subprocess.CalledProcessError:
			base.fail(LOGGER, 'rootfs `{defconfig}` returned with an error'.format(defconfig=defconfig))
		LOGGER.info('Finished `{defconfig}`.'.format(defconfig=defconfig))

		# Provide our kernel config as an output.
		shutil.copyfile(ubdir / '.config', PATHS.output / 'rootfs.config')
		LOGGER.warning(
		    'The output file `rootfs.config` has been created.  You must manually copy this to your sources directory.'
		)

	def nconfig(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
			return  # We're bypassed.

		ubdir = PATHS.build / 'rootfs'

		if not (ubdir / '.config').exists():
			base.fail(LOGGER, 'No rootfs configuration file was found.  Use rootfs:defconfig to generate one.')

		LOGGER.info('Running `nconfig`...')
		try:
			base.run(
			    PATHS,
			    LOGGER, ['make', 'CROSS_COMPILE=' + self.cross_compile, 'nconfig'],
			    cwd=ubdir,
			    stdin=None,
			    stdout=None,
			    stderr=None
			)
		except subprocess.CalledProcessError:
			base.fail(LOGGER, 'rootfs `nconfig` returned with an error')
		LOGGER.info('Finished `nconfig`.')

		# Provide our kernel config as an output.
		shutil.copyfile(ubdir / '.config', PATHS.output / 'rootfs.config')
		LOGGER.warning(
		    'The output file `rootfs.config` has been created.  You must manually copy this to your sources directory.'
		)

	def build(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
			return  # We're bypassed.

		ubdir = PATHS.build / 'buildroot'
		LOGGER.info('Running `make`...')
		try:
			base.run(PATHS, LOGGER, ['make', 'CROSS_COMPILE=' + self.cross_compile], cwd=ubdir)
		except subprocess.CalledProcessError:
			base.fail(LOGGER, '`make` returned with an error')

		# Provide PMU firmware ELF as an output.
		ub = ubdir / 'u-boot.elf'
		if not ub.exists():
			base.fail(LOGGER, 'u-boot.elf not found after build.')
		shutil.copyfile(ub, PATHS.output / 'u-boot.elf')

	def clean(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER, extract=False):
			return  # We're bypassed.

		LOGGER.info('Running `clean`...')
		try:
			base.run(
			    PATHS,
			    LOGGER, ['make', 'CROSS_COMPILE=' + self.cross_compile, 'clean'],
			    cwd=PATHS.build / 'u-boot'
			)
		except subprocess.CalledProcessError:
			base.fail(LOGGER, '`clean` returned with an error')
		LOGGER.info('Finished `clean`.')
		LOGGER.info('Deleting outputs.')
		shutil.rmtree(PATHS.output, ignore_errors=True)
		PATHS.output.mkdir(parents=True, exist_ok=True)
