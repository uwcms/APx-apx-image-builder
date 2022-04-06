import argparse
import shutil
import subprocess
import textwrap
from typing import List, Optional

from . import base


class UBootBuilder(base.BaseBuilder):
	NAME: str = 'u-boot'
	statefile: Optional[base.JSONStateFile] = None
	makeflags: List[str]

	@classmethod
	def prepare_argparse(cls, group: argparse._ArgumentGroup) -> None:
		group.description = textwrap.dedent(
		    '''
			Build U-Boot

			Stages available:
			- fetch: Download or copy sources.
			- prepare: Extract sources.
			- (defconfig): Run `make xilinx_zynq_virt_defconfig` or
			               Run `make xilinx_zynqmp_virt_defconfig`
			               as appropriate to your zynq_series configuration.
			- (nconfig): Run `make nconfig`
			- build: Build U-Boot
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
		self.STAGES['defconfig'] = base.BypassableStage(
		    self,
		    'defconfig',
		    self.check,
		    self.defconfig,
		    requires=[self.NAME + ':prepare'],
		    before=[self.NAME + ':build', self.NAME + ':nconfig'],
		    include_in_all=False,
		    extract_bypass=False
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
		if STAGE.name in ('fetch', 'prepare'
		                  ) and 'uboot_tag' not in self.BUILDER_CONFIG and 'uboot_sourceurl' not in self.BUILDER_CONFIG:
			STAGE.logger.error(
			    'Please set a `uboot_tag` or `uboot_sourceurl` (file://... is valid) in the configuration for the "u-boot" builder.'
			)
			check_ok = False
		if self.COMMON_CONFIG['zynq_series'] == 'zynq':
			self.cross_compile = 'arm-none-eabi-'
		elif self.COMMON_CONFIG['zynq_series'] == 'zynqmp':
			self.cross_compile = 'aarch64-none-elf-'
		if 'cross_compile' in self.BUILDER_CONFIG:
			self.cross_compile = self.BUILDER_CONFIG['cross_compile']
		if not shutil.which(self.cross_compile + 'gcc'):
			STAGE.logger.error(
			    'Unable to locate `{cross_compile}gcc`.  Did you source the Vivado environment files?'.format(
			        cross_compile=self.cross_compile
			    )
			)
			check_ok = False
		return check_ok

	def fetch(self, STAGE: base.Stage) -> None:
		statefile = base.JSONStateFile(self.PATHS.build / '.state.json')
		sourceurl: Optional[str] = self.BUILDER_CONFIG.get('uboot_sourceurl', None)
		if sourceurl is None:
			sourceurl = 'https://github.com/Xilinx/u-boot-xlnx/archive/refs/tags/{tag}.tar.gz'.format(
			    tag=self.BUILDER_CONFIG['uboot_tag']
			)
		if base.import_source(STAGE, sourceurl, 'u-boot.tar.gz'):
			with statefile as state:
				state['tree_ready'] = False

	def prepare(self, STAGE: base.Stage) -> None:
		statefile = base.JSONStateFile(self.PATHS.build / '.state.json')
		ubdir = self.PATHS.build / 'u-boot'
		patcher = base.Patcher(self.PATHS.build / 'patches')
		if patcher.import_patches(STAGE, self.BUILDER_CONFIG.get('patches', [])):
			with statefile as state:
				state['tree_ready'] = False
		if statefile.state.get('tree_ready', False):
			STAGE.logger.info('The U-Boot source tree has already been extracted.  Skipping.')
		else:
			base.untar(STAGE, self.PATHS.build / 'u-boot.tar.gz', self.PATHS.build / 'u-boot')
			patcher.apply(STAGE, self.PATHS.build / 'u-boot')
			with statefile as state:
				state['tree_ready'] = True

		if base.import_source(STAGE, 'u-boot.config', self.PATHS.build / '.config', optional=True,
		                      ignore_timestamps=True):
			# We need to use a two stage load here because we actually do update
			# the imported source, and don't want needless imports to interfere
			# with `make` caching.
			# .config might not exist, if we're running defconfig.
			if (self.PATHS.build / '.config').exists():
				user_config_hash = base.hash_file('sha256', open(self.PATHS.build / '.config', 'rb'))
				if statefile.state.get('user_config_hash', '') != user_config_hash:
					base.copyfile(self.PATHS.build / '.config', ubdir / '.config')
					with statefile as state:
						state['user_config_hash'] = user_config_hash
			else:
				(ubdir / '.config').unlink(missing_ok=True)

		# Fallback check required when the tree is regenerated with an unchanged config.
		if (self.PATHS.build / '.config').exists() and not (ubdir / '.config').exists():
			base.copyfile(self.PATHS.build / '.config', ubdir / '.config')

		# Provide our config as an output.
		if (ubdir / '.config').exists():
			base.copyfile(ubdir / '.config', self.PATHS.output / 'u-boot.config')

	def defconfig(self, STAGE: base.Stage) -> None:
		zynq_series = self.COMMON_CONFIG.get('zynq_series', '')
		if zynq_series == 'zynq':
			defconfig = 'xilinx_zynq_virt_defconfig'
		elif zynq_series == 'zynqmp':
			defconfig = 'xilinx_zynqmp_virt_defconfig'
		else:
			base.fail(STAGE.logger, "Unknown zynq_series setting: " + repr(zynq_series))

		ubdir = self.PATHS.build / 'u-boot'
		STAGE.logger.info('Running `{defconfig}`...'.format(defconfig=defconfig))
		try:
			base.run(STAGE, ['make', 'CROSS_COMPILE=' + self.cross_compile, defconfig], cwd=ubdir)
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, 'U-Boot `{defconfig}` returned with an error'.format(defconfig=defconfig))
		STAGE.logger.info('Finished `{defconfig}`.'.format(defconfig=defconfig))

		# Provide our kernel config as an output.
		base.copyfile(ubdir / '.config', self.PATHS.output / 'u-boot.config')
		STAGE.logger.warning(
		    'The output file `u-boot.config` has been created.  You must manually copy this to your sources directory.'
		)

	def nconfig(self, STAGE: base.Stage) -> None:
		ubdir = self.PATHS.build / 'u-boot'

		if not (ubdir / '.config').exists():
			base.fail(STAGE.logger, 'No U-Boot configuration file was found.  Use u-boot:defconfig to generate one.')

		STAGE.logger.info('Running `nconfig`...')
		try:
			base.run(
			    STAGE, ['make', 'CROSS_COMPILE=' + self.cross_compile, 'nconfig'],
			    cwd=ubdir,
			    stdin=None,
			    stdout=None,
			    stderr=None
			)
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, 'U-Boot `nconfig` returned with an error')
		STAGE.logger.info('Finished `nconfig`.')

		# Provide our kernel config as an output.
		base.copyfile(ubdir / '.config', self.PATHS.output / 'u-boot.config')
		STAGE.logger.warning(
		    'The output file `u-boot.config` has been created.  You must manually copy this to your sources directory.'
		)

	def build(self, STAGE: base.Stage) -> None:
		ubdir = self.PATHS.build / 'u-boot'
		if not (ubdir / '.config').exists():
			base.fail(STAGE.logger, 'No U-Boot configuration file was found.  Use u-boot:defconfig to generate one.')

		STAGE.logger.info('Running `make`...')
		try:
			base.run(STAGE, ['make', 'CROSS_COMPILE=' + self.cross_compile], cwd=ubdir)
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, '`make` returned with an error')

		# Provide uboot ELF as an output.
		ub = ubdir / 'u-boot.elf'
		if not ub.exists():
			base.fail(STAGE.logger, 'u-boot.elf not found after build.')
		base.copyfile(ub, self.PATHS.output / 'u-boot.elf')

	def clean(self, STAGE: base.Stage) -> None:
		STAGE.logger.info('Running `clean`...')
		try:
			base.run(STAGE, ['make', 'CROSS_COMPILE=' + self.cross_compile, 'clean'], cwd=self.PATHS.build / 'u-boot')
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, '`clean` returned with an error')
		STAGE.logger.info('Finished `clean`.')
		STAGE.logger.info('Deleting outputs.')
		shutil.rmtree(self.PATHS.output, ignore_errors=True)
		self.PATHS.output.mkdir(parents=True, exist_ok=True)
