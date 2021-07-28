import argparse
import hashlib
import itertools
import logging
import os
import shlex
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import base


class KernelBuilder(base.BaseBuilder):
	NAME: str = 'kernel'
	kbuild_args: List[str]
	target_arch: str
	statefile: Optional[base.JSONStateFile] = None

	def update_config(self, config: Dict[str, Any], ARGS: argparse.Namespace):
		super().update_config(config, ARGS)
		if self.COMMON_CONFIG.get('zynq_series', '') == 'zynq':
			self.BUILDER_CONFIG.setdefault('profile', 'arm')
		elif self.COMMON_CONFIG.get('zynq_series', '') == 'zynqmp':
			self.BUILDER_CONFIG.setdefault('profile', 'arm64')

	@classmethod
	def prepare_argparse(cls, group: argparse._ArgumentGroup) -> None:
		group.description = '''
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

	def check(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> bool:
		if base.check_bypass(STAGE, PATHS, LOGGER, extract=False):
			return True  # We're bypassed.  Our checks don't matter.

		if self.statefile is None:
			self.statefile = base.JSONStateFile(PATHS.build / '.state.json')

		check_ok: bool = True
		if STAGE.name in (
		    'fetch',
		    'prepare') and 'kernel_tag' not in self.BUILDER_CONFIG and 'kernel_sourceurl' not in self.BUILDER_CONFIG:
			LOGGER.error(
			    'Please set a `kernel_tag` or `kernel_sourceurl` (file://... is valid) in the configuration for the "kernel" builder.'
			)
			check_ok = False
		self.kbuild_args = []
		if self.BUILDER_CONFIG.get('profile', '') not in ('arm', 'arm64', 'custom'):
			LOGGER.error('You must set builders.kernel.profile to one of "arm", "arm64", "custom".')
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
			LOGGER.error(
			    'If you are using builders.kernel.profile "custom", you must supply ARCH=... and CROSS_COMPILE=... in builders.kernel.extra_kbuild_args.'
			)
			return False
		else:
			cross_compile = [x.split('=', 1)[-1] for x in self.kbuild_args if x.startswith('CROSS_COMPILE=')][0]
			if not shutil.which(cross_compile + 'gcc'):
				LOGGER.error(
				    'Unable to locate `{cross_compile}gcc`.  Did you source the Vivado environment files?'.format(
				        cross_compile=cross_compile
				    )
				)
				check_ok = False
		self.target_arch = [arg.split('=', 1)[1] for arg in self.kbuild_args if arg.startswith('ARCH=')][0]
		cross_compile = [arg.split('=', 1)[1] for arg in self.kbuild_args if arg.startswith('CROSS_COMPILE=')][0]
		with self.statefile as state:
			if state.setdefault('target_arch', self.target_arch) != self.target_arch:
				LOGGER.error(
				    'The existing workspace has ARCH={prepared}.  You have requested ARCH={target}.  Please run distclean.'
				    .format(prepared=state['target_arch'], target=self.target_arch)
				)
				check_ok = False
			if state.setdefault('cross_compile', cross_compile) != cross_compile:
				LOGGER.error(
				    'The existing workspace has CROSS_COMPILE={prepared}.  You have requested CROSS_COMPILE={target}.  Please run distclean.'
				    .format(prepared=state['cross_compile'], target=cross_compile)
				)
				check_ok = False
		# TODO: More checks.
		return check_ok

	def fetch(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
			return  # We're bypassed.

		assert self.statefile is not None
		sourceurl: Optional[str] = self.BUILDER_CONFIG.get('kernel_sourceurl', None)
		if sourceurl is None:
			sourceurl = 'https://github.com/Xilinx/linux-xlnx/archive/refs/tags/{tag}.tar.gz'.format(
			    tag=self.BUILDER_CONFIG['kernel_tag']
			)

		if base.import_source(PATHS, LOGGER, self.ARGS, sourceurl, PATHS.build / 'linux.tar.gz'):
			with self.statefile as state:
				state['tree_ready'] = False

	def prepare(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
			return  # We're bypassed.

		assert self.statefile is not None
		linuxdir = PATHS.build / 'linux'
		if self.statefile.state.get('tree_ready', False):
			LOGGER.info('The linux source tree has already been extracted.  Skipping.')
		else:
			base.untar(PATHS, LOGGER, 'linux.tar.gz', PATHS.build / 'linux')
			with self.statefile as state:
				state['tree_ready'] = True

		if base.import_source(PATHS, LOGGER, self.ARGS, 'kernel.config', PATHS.build / 'user.config'):
			# We need to use a two stage load here because we actually do update
			# the imported source, and don't want needless imports to interfere
			# with `make` caching.
			user_config_hash = base.hash_file('sha256', open(PATHS.build / 'user.config', 'rb'))
			if self.statefile.state.get('user_config_hash', '') != user_config_hash:
				shutil.copyfile(PATHS.build / 'user.config', PATHS.build / 'linux/.config')
				with self.statefile as state:
					state['user_config_hash'] = user_config_hash
					state['built_config_hash'] = None

		# Provide our kernel config as an output.
		shutil.copyfile(linuxdir / '.config', PATHS.output / 'kernel.config')

	def defconfig(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
			return  # We're bypassed.

		linuxdir = PATHS.build / 'linux'
		LOGGER.info('Running `defconfig`...')
		try:
			base.run(PATHS, LOGGER, ['make'] + self.kbuild_args + ['defconfig'], cwd=linuxdir)
		except subprocess.CalledProcessError:
			base.fail(LOGGER, 'Kernel `defconfig` returned with an error')
		LOGGER.info('Finished `defconfig`.')

		assert self.statefile is not None
		with self.statefile as state:
			state['user_config_hash'] = None  # disable any "caching" next run

		# Provide our kernel config as an output.
		shutil.copyfile(linuxdir / '.config', PATHS.output / 'kernel.config')
		LOGGER.warning(
		    'The output file `kernel.config` has been created.  You must manually copy this to your sources directory.'
		)

	def oldconfig(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
			return  # We're bypassed.

		linuxdir = PATHS.build / 'linux'

		if not (linuxdir / '.config').exists():
			base.fail(LOGGER, 'No kernel configuration file was found.  Use kernel:defconfig to generate one.')

		LOGGER.info('Running `oldconfig`...')
		try:
			base.run(PATHS, LOGGER, ['make'] + self.kbuild_args + ['oldconfig'], cwd=linuxdir)
		except subprocess.CalledProcessError:
			base.fail(LOGGER, 'Kernel `oldconfig` returned with an error')
		LOGGER.info('Finished `oldconfig`.')

		assert self.statefile is not None
		with self.statefile as state:
			state['user_config_hash'] = None  # disable any "caching" next run

		# Provide our kernel config as an output.
		shutil.copyfile(linuxdir / '.config', PATHS.output / 'kernel.config')
		LOGGER.warning(
		    'The output file `kernel.config` has been created.  You must manually copy this to your sources directory.'
		)

	def nconfig(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
			return  # We're bypassed.

		linuxdir = PATHS.build / 'linux'

		if not (linuxdir / '.config').exists():
			base.fail(LOGGER, 'No kernel configuration file was found.  Use kernel:defconfig to generate one.')

		LOGGER.info('Running `nconfig`...')
		try:
			base.run(
			    PATHS,
			    LOGGER, ['make'] + self.kbuild_args + ['nconfig'],
			    cwd=linuxdir,
			    stdin=None,
			    stdout=None,
			    stderr=None
			)
		except subprocess.CalledProcessError:
			base.fail(LOGGER, 'Kernel `nconfig` returned with an error')
		LOGGER.info('Finished `nconfig`.')

		assert self.statefile is not None
		with self.statefile as state:
			state['user_config_hash'] = None  # disable any "caching" next run

		# Provide our kernel config as an output.
		shutil.copyfile(linuxdir / '.config', PATHS.output / 'kernel.config')
		LOGGER.warning(
		    'The output file `kernel.config` has been created.  You must manually copy this to your sources directory.'
		)

	def olddefconfig(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
			return  # We're bypassed.

		assert self.statefile is not None
		linuxdir = PATHS.build / 'linux'

		if not (linuxdir / '.config').exists():
			base.fail(LOGGER, 'No kernel configuration file was found.  Use kernel:defconfig to generate one.')

		built_config_hash = base.hash_file('sha256', open(linuxdir / '.config', 'rb'))
		if self.statefile.state.get('built_config_hash', None) == built_config_hash:
			LOGGER.info('We have already run `olddefconfig` on this config file.')
		else:
			LOGGER.info('Running `olddefconfig` to ensure config consistency.')
			try:
				base.run(PATHS, LOGGER, ['make'] + self.kbuild_args + ['olddefconfig'], cwd=linuxdir)
			except subprocess.CalledProcessError:
				base.fail(LOGGER, 'Kernel `olddefconfig` returned with an error')
			LOGGER.info('Finished `olddefconfig`.')
			with self.statefile as state:
				state['built_config_hash'] = base.hash_file('sha256', open(linuxdir / '.config', 'rb'))

		# Provide our final, used kernel config as an output, separate from the user-defined one.
		shutil.copyfile(linuxdir / '.config', PATHS.output / 'kernel.config.built')

	def build(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
			return  # We're bypassed. (And chose to extract back in `prepare`)

		linuxdir = PATHS.build / 'linux'

		for output in itertools.chain.from_iterable(
		    PATHS.output.glob(x) for x in ('vmlinux', 'Image.gz', 'apx-kernel-*.rpm')):
			LOGGER.debug('Removing pre-existing output ' + str(output))
			try:
				output.unlink()
			except Exception:
				pass

		base.import_source(
		    PATHS, LOGGER, self.ARGS, 'builtin:///kernel_data/binkernel.spec', PATHS.build / 'binkernel.spec'
		)

		LOGGER.info('Running `make`...')
		try:
			base.run(PATHS, LOGGER, ['make'] + self.kbuild_args, cwd=linuxdir)
		except subprocess.CalledProcessError:
			base.fail(LOGGER, 'Kernel `make` returned with an error')

		# Provide vmlinux ELF as an output. (for JTAG boots)
		shutil.copyfile(linuxdir / 'vmlinux', PATHS.output / 'vmlinux')
		# Provide the Image.gz as an output. (for QSPI boots?)
		shutil.copyfile(linuxdir / 'arch/arm64/boot/Image.gz', PATHS.output / 'Image.gz')

		LOGGER.info('Building kernel RPMs')
		shutil.copyfile(PATHS.build / 'binkernel.spec', linuxdir / 'binkernel.spec')

		LOGGER.debug('Identifying kernel release.')
		kernelrelease = ''  # This will set the str type properly.  fail() below will ensure the value is set properly.
		try:
			kernelrelease = base.run(
			    PATHS,
			    LOGGER, ['make', '-s'] + self.kbuild_args + ['kernelrelease'],
			    cwd=linuxdir,
			    DETAIL_LOGLEVEL=logging.NOTSET,
			    OUTPUT_LOGLEVEL=logging.NOTSET
			)[1].decode('utf8').strip()
		except subprocess.CalledProcessError:
			base.fail(LOGGER, 'Kernel `kernelrelease` returned with an error')
		LOGGER.debug('Identified kernel release ' + kernelrelease)

		rpmbuilddir = PATHS.build / 'rpmbuild'
		shutil.rmtree(rpmbuilddir, ignore_errors=True)
		rpmbuilddir.mkdir()
		LOGGER.info('Running rpmbuild...')
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
			base.run(PATHS, LOGGER, rpmcmd, cwd=linuxdir)
		except subprocess.CalledProcessError:
			base.fail(LOGGER, 'rpmbuild returned with an error')
		LOGGER.info('Finished rpmbuild.')

		# Provide our rpms as an output. (for standard installation)
		for file in PATHS.build.glob('rpmbuild/RPMS/*/*.rpm'):
			shutil.copyfile(file, PATHS.output / file.name)

		# Provide our final, used kernel config as an output, separate from the user-defined one.
		shutil.copyfile(linuxdir / '.config', PATHS.output / 'kernel.config.built')

	def clean(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER, extract=False):
			return  # We're bypassed.

		LOGGER.info('Running `mrproper`...')
		try:
			base.run(PATHS, LOGGER, ['make'] + self.kbuild_args + ['mrproper'], cwd=PATHS.build / 'linux')
		except subprocess.CalledProcessError:
			base.fail(LOGGER, 'Kernel `mrproper` returned with an error')
		LOGGER.info('Finished `mrproper`.')
		LOGGER.info('Deleting outputs.')
		shutil.rmtree(PATHS.output, ignore_errors=True)
		PATHS.output.mkdir(parents=True, exist_ok=True)
