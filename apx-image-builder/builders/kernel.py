import argparse
import hashlib
import itertools
import logging
import os
import shlex
import shutil
import subprocess
import textwrap
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import base


class KernelBuilder(base.BaseBuilder):
	NAME: str = 'kernel'
	kbuild_args: List[str]
	target_arch: str
	cross_compile: str
	statefile: Optional[base.JSONStateFile] = None

	def __init__(self, config: Dict[str, Any], paths: base.BuildPaths, ARGS: argparse.Namespace):
		super().__init__(config, paths, ARGS)
		if self.COMMON_CONFIG.get('zynq_series', '') == 'zynq':
			self.BUILDER_CONFIG.setdefault('profile', 'arm')
		elif self.COMMON_CONFIG.get('zynq_series', '') == 'zynqmp':
			self.BUILDER_CONFIG.setdefault('profile', 'arm64')

	def prepare_argparse(self, group: argparse._ArgumentGroup) -> None:
		group.description = textwrap.dedent(
		    '''
			Build the linux kernel image.

			Stages available:
			fetch: Download or copy sources.
			prepare: Extract sources and import user config
			(defconfig): Run `make defconfig`
			(oldconfig): Run `make oldconfig`
			(nconfig): Run `make nconfig`
			olddefconfig: Run `make olddefconfig`
							(required by `build` to ensure config consistency)
			build: Build the kernel

			The user-defined configuration will be output as kernel.config.user during the
			`prepare` step, as well as any of def/old/nconfig.  You must manually move
			this back to the user sources directory for the kernel builder, as it will be
			replaced whenever prepare is run.

			`olddefconfig` is always run before `build` to ensure the config is complete and
			valid.  This may result in a slightly different kernel config being used for the
			actual build step, if there were undefined options in the user config.  This
			file will be output as kernel.config.built.
			'''
		).strip()

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
		    before=[self.NAME + ':olddefconfig'],
		    include_in_all=False
		)
		self.STAGES['oldconfig'] = base.Stage(
		    self,
		    'oldconfig',
		    self.check,
		    self.oldconfig,
		    requires=[self.NAME + ':prepare'],
		    after=[self.NAME + ':prepare', self.NAME + ':defconfig'],
		    before=[self.NAME + ':olddefconfig'],
		    include_in_all=False
		)
		self.STAGES['nconfig'] = base.Stage(
		    self,
		    'nconfig',
		    self.check,
		    self.nconfig,
		    requires=[self.NAME + ':prepare'],
		    after=[self.NAME + ':prepare', self.NAME + ':defconfig', self.NAME + ':oldconfig'],
		    before=[self.NAME + ':olddefconfig'],
		    include_in_all=False
		)
		self.STAGES['olddefconfig'] = base.Stage(
		    self, 'olddefconfig', self.check, self.olddefconfig, requires=[self.NAME + ':prepare']
		)
		self.STAGES['build'] = base.Stage(self, 'build', self.check, self.build, requires=[self.NAME + ':olddefconfig'])

	def check(self, STAGE: base.Stage) -> bool:
		if base.check_bypass(STAGE, extract=False):
			return True  # We're bypassed.  Our checks don't matter.

		check_ok: bool = True
		if STAGE.name in (
		    'fetch',
		    'prepare') and 'kernel_tag' not in self.BUILDER_CONFIG and 'kernel_sourceurl' not in self.BUILDER_CONFIG:
			STAGE.logger.error(
			    'Please set a `kernel_tag` or `kernel_sourceurl` (file://... is valid) in the configuration for the "kernel" builder.'
			)
			check_ok = False
		self.kbuild_args = []
		if self.BUILDER_CONFIG.get('profile', '') not in ('arm', 'arm64', 'custom'):
			STAGE.logger.error('You must set builders.kernel.profile to one of "arm", "arm64", "custom".')
			return False
		elif self.BUILDER_CONFIG['profile'] == 'arm':
			# TODO: Make this check 'default' vs 'custom' and use the 'zynq_series' setting.
			self.kbuild_args += ['ARCH=arm', 'CROSS_COMPILE=arm-none-eabi-']
		elif self.BUILDER_CONFIG['profile'] == 'arm64':
			self.kbuild_args += ['ARCH=arm64', 'CROSS_COMPILE=aarch64-none-elf-']
		elif self.BUILDER_CONFIG['profile'] == 'custom':
			pass  # Checked indirectly below.
		if self.BUILDER_CONFIG.get('extra_kbuild_args', []):
			self.kbuild_args.extend(self.BUILDER_CONFIG['extra_kbuild_args'])
		if set(('ARCH', 'CROSS_COMPILE')) - set(arg.split('=', 1)[0] for arg in self.kbuild_args if '=' in arg):
			STAGE.logger.error(
			    'If you are using builders.kernel.profile "custom", you must supply ARCH=... and CROSS_COMPILE=... in builders.kernel.extra_kbuild_args.'
			)
			return False
		else:
			self.cross_compile = [x.split('=', 1)[-1] for x in self.kbuild_args if x.startswith('CROSS_COMPILE=')][0]
			if not shutil.which(self.cross_compile + 'gcc'):
				STAGE.logger.error(
				    'Unable to locate `{cross_compile}gcc`.  Did you source the Vivado environment files?'.format(
				        cross_compile=self.cross_compile
				    )
				)
				check_ok = False
		self.target_arch = [arg.split('=', 1)[1] for arg in self.kbuild_args if arg.startswith('ARCH=')][0]
		self.cross_compile = [arg.split('=', 1)[1] for arg in self.kbuild_args if arg.startswith('CROSS_COMPILE=')][0]
		# TODO: More checks.
		return check_ok

	def fetch(self, STAGE: base.Stage) -> None:
		if base.check_bypass(STAGE):
			return  # We're bypassed.

		statefile = base.JSONStateFile(self.PATHS.build / '.state.json')
		sourceurl: Optional[str] = self.BUILDER_CONFIG.get('kernel_sourceurl', None)
		if sourceurl is None:
			sourceurl = 'https://github.com/Xilinx/linux-xlnx/archive/refs/tags/{tag}.tar.gz'.format(
			    tag=self.BUILDER_CONFIG['kernel_tag']
			)

		if base.import_source(STAGE, sourceurl, self.PATHS.build / 'linux.tar.gz'):
			with statefile as state:
				state['tree_ready'] = False

	def prepare(self, STAGE: base.Stage) -> None:
		if base.check_bypass(STAGE):
			return  # We're bypassed.

		statefile = base.JSONStateFile(self.PATHS.build / '.state.json')

		with statefile as state:
			if state.setdefault('target_arch', self.target_arch) != self.target_arch:
				base.fail(
				    STAGE.logger,
				    'The existing workspace has ARCH={prepared}.  You have requested ARCH={target}.  Please run distclean.'
				    .format(prepared=state['target_arch'], target=self.target_arch)
				)
			if state.setdefault('cross_compile', self.cross_compile) != self.cross_compile:
				base.fail(
				    STAGE.logger,
				    'The existing workspace has CROSS_COMPILE={prepared}.  You have requested CROSS_COMPILE={target}.  Please run distclean.'
				    .format(prepared=state['cross_compile'], target=self.cross_compile)
				)

		linuxdir = self.PATHS.build / 'linux'
		patcher = base.Patcher(self.PATHS.build / 'patches')
		if patcher.import_patches(STAGE, self.BUILDER_CONFIG.get('patches', [])):
			with statefile as state:
				state['tree_ready'] = False
		if statefile.state.get('tree_ready', False):
			STAGE.logger.info('The linux source tree has already been extracted.  Skipping.')
		else:
			base.untar(STAGE, 'linux.tar.gz', self.PATHS.build / 'linux')
			patcher.apply(STAGE, self.PATHS.build / 'linux')
			with statefile as state:
				state['tree_ready'] = True

		if base.import_source(STAGE, 'kernel.config', self.PATHS.build / 'user.config', ignore_timestamps=True):
			# We need to use a two stage load here because we actually do update
			# the imported source, and don't want needless imports to interfere
			# with `make` caching.
			user_config_hash = base.hash_file('sha256', open(self.PATHS.build / 'user.config', 'rb'))
			if statefile.state.get('user_config_hash', '') != user_config_hash:
				shutil.copyfile(self.PATHS.build / 'user.config', self.PATHS.build / 'linux/.config')
				with statefile as state:
					state['user_config_hash'] = user_config_hash
					state['built_config_hash'] = None

		# Provide our kernel config as an output.
		shutil.copyfile(linuxdir / '.config', self.PATHS.output / 'kernel.config')

	def defconfig(self, STAGE: base.Stage) -> None:
		if base.check_bypass(STAGE):
			return  # We're bypassed.

		linuxdir = self.PATHS.build / 'linux'
		STAGE.logger.info('Running `defconfig`...')
		try:
			base.run(STAGE, ['make'] + self.kbuild_args + ['defconfig'], cwd=linuxdir)
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, 'Kernel `defconfig` returned with an error')
		STAGE.logger.info('Finished `defconfig`.')

		statefile = base.JSONStateFile(self.PATHS.build / '.state.json')
		with statefile as state:
			state['user_config_hash'] = None  # disable any "caching" next run

		# Provide our kernel config as an output.
		shutil.copyfile(linuxdir / '.config', self.PATHS.output / 'kernel.config')
		STAGE.logger.warning(
		    'The output file `kernel.config` has been created.  You must manually copy this to your sources directory.'
		)

	def oldconfig(self, STAGE: base.Stage) -> None:
		if base.check_bypass(STAGE):
			return  # We're bypassed.

		linuxdir = self.PATHS.build / 'linux'

		if not (linuxdir / '.config').exists():
			base.fail(STAGE.logger, 'No kernel configuration file was found.  Use kernel:defconfig to generate one.')

		STAGE.logger.info('Running `oldconfig`...')
		try:
			base.run(STAGE, ['make'] + self.kbuild_args + ['oldconfig'], cwd=linuxdir)
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, 'Kernel `oldconfig` returned with an error')
		STAGE.logger.info('Finished `oldconfig`.')

		statefile = base.JSONStateFile(self.PATHS.build / '.state.json')
		with statefile as state:
			state['user_config_hash'] = None  # disable any "caching" next run

		# Provide our kernel config as an output.
		shutil.copyfile(linuxdir / '.config', self.PATHS.output / 'kernel.config')
		STAGE.logger.warning(
		    'The output file `kernel.config` has been created.  You must manually copy this to your sources directory.'
		)

	def nconfig(self, STAGE: base.Stage) -> None:
		if base.check_bypass(STAGE):
			return  # We're bypassed.

		linuxdir = self.PATHS.build / 'linux'

		if not (linuxdir / '.config').exists():
			base.fail(STAGE.logger, 'No kernel configuration file was found.  Use kernel:defconfig to generate one.')

		STAGE.logger.info('Running `nconfig`...')
		try:
			base.run(
			    STAGE, ['make'] + self.kbuild_args + ['nconfig'], cwd=linuxdir, stdin=None, stdout=None, stderr=None
			)
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, 'Kernel `nconfig` returned with an error')
		STAGE.logger.info('Finished `nconfig`.')

		statefile = base.JSONStateFile(self.PATHS.build / '.state.json')
		with statefile as state:
			state['user_config_hash'] = None  # disable any "caching" next run

		# Provide our kernel config as an output.
		shutil.copyfile(linuxdir / '.config', self.PATHS.output / 'kernel.config')
		STAGE.logger.warning(
		    'The output file `kernel.config` has been created.  You must manually copy this to your sources directory.'
		)

	def olddefconfig(self, STAGE: base.Stage) -> None:
		if base.check_bypass(STAGE):
			return  # We're bypassed.

		statefile = base.JSONStateFile(self.PATHS.build / '.state.json')
		linuxdir = self.PATHS.build / 'linux'

		if not (linuxdir / '.config').exists():
			base.fail(STAGE.logger, 'No kernel configuration file was found.  Use kernel:defconfig to generate one.')

		built_config_hash = base.hash_file('sha256', open(linuxdir / '.config', 'rb'))
		if statefile.state.get('built_config_hash', None) == built_config_hash:
			STAGE.logger.info('We have already run `olddefconfig` on this config file.')
		else:
			STAGE.logger.info('Running `olddefconfig` to ensure config consistency.')
			try:
				base.run(STAGE, ['make'] + self.kbuild_args + ['olddefconfig'], cwd=linuxdir)
			except subprocess.CalledProcessError:
				base.fail(STAGE.logger, 'Kernel `olddefconfig` returned with an error')
			STAGE.logger.info('Finished `olddefconfig`.')
			with statefile as state:
				state['built_config_hash'] = base.hash_file('sha256', open(linuxdir / '.config', 'rb'))

		# Provide our final, used kernel config as an output, separate from the user-defined one.
		shutil.copyfile(linuxdir / '.config', self.PATHS.output / 'kernel.config.built')

	def build(self, STAGE: base.Stage) -> None:
		if base.check_bypass(STAGE):
			return  # We're bypassed. (And chose to extract back in `prepare`)

		linuxdir = self.PATHS.build / 'linux'

		for output in itertools.chain.from_iterable(
		    self.PATHS.output.glob(x) for x in ('vmlinux', 'Image.gz', 'apx-kernel-*.rpm')):
			STAGE.logger.debug('Removing pre-existing output ' + str(output))
			try:
				output.unlink()
			except Exception:
				pass

		base.import_source(STAGE, 'builtin:///kernel_data/binkernel.spec', self.PATHS.build / 'binkernel.spec')

		STAGE.logger.info('Running `make`...')
		try:
			base.run(STAGE, ['make'] + self.kbuild_args, cwd=linuxdir)
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, 'Kernel `make` returned with an error')

		# Provide vmlinux ELF as an output. (for JTAG boots)
		shutil.copyfile(linuxdir / 'vmlinux', self.PATHS.output / 'vmlinux')
		# Provide the Image.gz as an output. (for QSPI boots?)
		shutil.copyfile(linuxdir / 'arch/arm64/boot/Image.gz', self.PATHS.output / 'Image.gz')

		STAGE.logger.info('Building kernel RPMs')
		shutil.copyfile(self.PATHS.build / 'binkernel.spec', linuxdir / 'binkernel.spec')

		STAGE.logger.debug('Identifying kernel release.')
		kernelrelease = ''  # This will set the str type properly.  fail() below will ensure the value is set properly.
		try:
			kernelrelease = base.run(
			    STAGE, ['make', '-s'] + self.kbuild_args + ['kernelrelease'],
			    cwd=linuxdir,
			    DETAIL_LOGLEVEL=logging.NOTSET,
			    OUTPUT_LOGLEVEL=logging.NOTSET
			)[1].decode('utf8').strip()
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, 'Kernel `kernelrelease` returned with an error')
		STAGE.logger.debug('Identified kernel release ' + kernelrelease)

		rpmbuilddir = self.PATHS.build / 'rpmbuild'
		shutil.rmtree(rpmbuilddir, ignore_errors=True)
		rpmbuilddir.mkdir()
		STAGE.logger.info('Running rpmbuild...')
		try:
			rpmcmd = [
			    'rpmbuild',
			    '--define=_topdir ' + str(rpmbuilddir),
			    '--define=_builddir .',
			    '--define=rpm_release ' + str(int(time.time())),
			    '--define=kernelrelease ' + kernelrelease,
			    '--define=kernel_makeargs ' + ' '.join(shlex.quote(arg) for arg in self.kbuild_args),
			    '--target',
			    'aarch64' if self.target_arch == 'arm64' else 'armv7hl',
			    '-bb',
			    'binkernel.spec',
			]
			base.run(STAGE, rpmcmd, cwd=linuxdir)
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, 'rpmbuild returned with an error')
		STAGE.logger.info('Finished rpmbuild.')

		# Provide our rpms as an output. (for standard installation)
		for file in self.PATHS.build.glob('rpmbuild/RPMS/*/*.rpm'):
			shutil.copyfile(file, self.PATHS.output / file.name)

		# Provide our final, used kernel config as an output, separate from the user-defined one.
		shutil.copyfile(linuxdir / '.config', self.PATHS.output / 'kernel.config.built')

	def clean(self, STAGE: base.Stage) -> None:
		if base.check_bypass(STAGE, extract=False):
			return  # We're bypassed.

		STAGE.logger.info('Running `mrproper`...')
		try:
			base.run(STAGE, ['make'] + self.kbuild_args + ['mrproper'], cwd=self.PATHS.build / 'linux')
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, 'Kernel `mrproper` returned with an error')
		STAGE.logger.info('Finished `mrproper`.')
		STAGE.logger.info('Deleting outputs.')
		shutil.rmtree(self.PATHS.output, ignore_errors=True)
		self.PATHS.output.mkdir(parents=True, exist_ok=True)
