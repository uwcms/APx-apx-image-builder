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
  (menuconfig): Run `make menuconfig`
  olddefconfig: Run `make olddefconfig`
                (required by `build` to ensure config consistency)
  build: Build the kernel

The user-defined configuration will be output as kernel.config.user during the
`prepare` step, as well as any of def/old/menuconfig.  You must manually move
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
		self.STAGES['menuconfig'] = base.Stage(
		    self,
		    'menuconfig',
		    self.check,
		    self.menuconfig,
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
		if STAGE.name == 'fetch' and not shutil.which('wget'):
			LOGGER.error('Please install `wget`.')
			check_ok = False
		self.kbuild_args = []
		if self.BUILDER_CONFIG.get('profile', '') not in ('arm', 'arm64', 'custom'):
			LOGGER.error('You must set builders.kernel.profile to one of "arm", "arm64", "custom".')
			return False
		elif self.BUILDER_CONFIG['profile'] == 'arm':
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
		sourceid = hashlib.new('sha256', sourceurl.encode('utf8')).hexdigest()
		tarfile = PATHS.build / 'linux-{sourceid}.tar.gz'.format(sourceid=sourceid)
		if tarfile.exists():
			# This step is already complete.
			LOGGER.info('Sources already available.  Not fetching.')
		else:
			parsed_sourceurl = urllib.parse.urlparse(sourceurl)
			if parsed_sourceurl.scheme == 'file':
				try:
					shutil.copyfile(parsed_sourceurl.path, tarfile, follow_symlinks=True)
				except Exception as e:
					base.fail(LOGGER, 'Unable to copy kernel source tarball', e)
			else:
				try:
					base.run(
					    PATHS,
					    LOGGER,
					    ['wget', '-O', tarfile, sourceurl],
					    stdout=None if self.ARGS.verbose else subprocess.PIPE,
					    stderr=None if self.ARGS.verbose else subprocess.STDOUT,
					    OUTPUT_LOGLEVEL=logging.NOTSET,
					)
				except Exception as e:
					try:
						tarfile.unlink()
					except:
						pass
					base.fail(LOGGER, 'Unable to download kernel source tarball')
		chosen_source = PATHS.build / 'linux.tar.gz'
		if chosen_source.resolve() != tarfile.resolve():
			LOGGER.info('Selected new source, forcing new `prepare`.')
			try:
				chosen_source.unlink()
			except FileNotFoundError:
				pass
			chosen_source.symlink_to(tarfile)
			with self.statefile as state:
				state['tree_ready'] = False
		LOGGER.debug('Selected source ' + str(tarfile.name))

	def prepare(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
			return  # We're bypassed.

		assert self.statefile is not None
		linuxdir = PATHS.build / 'linux'
		if self.statefile.state.get('tree_ready', False):
			LOGGER.info('The linux source tree has already been extracted.  Skipping.')
		else:
			LOGGER.debug('Removing any existing linux source tree.')
			shutil.rmtree(linuxdir, ignore_errors=True)
			LOGGER.info('Extracting the linux source tree.')
			linuxdir.mkdir()
			try:
				base.run(PATHS, LOGGER, ['tar', '-xf', str(PATHS.build / 'linux.tar.gz'), '-C', str(linuxdir)])
			except subprocess.CalledProcessError:
				base.fail(LOGGER, 'Unable to extract kernel source tarball')
			while True:
				subdirs = [x for x in linuxdir.glob('*') if x.is_dir()]
				if len(subdirs) == 1 and not (linuxdir / 'Makefile').exists():
					LOGGER.debug(
					    'Found a single subdirectory {dir} and no Makefile.  Moving it up.'.format(
					        dir=repr(str(subdirs[0].name))
					    )
					)
					try:
						shutil.rmtree(str(linuxdir) + '~', ignore_errors=True)
						os.rename(subdirs[0], Path(str(linuxdir) + '~'))
						linuxdir.rmdir()
						os.rename(Path(str(linuxdir) + '~'), linuxdir)
					except Exception as e:
						base.fail(LOGGER, 'Unable to relocate linux source subdirectory.', e)
				else:
					break
			with self.statefile as state:
				state['tree_ready'] = True

		LOGGER.info('Importing user kernel config file.')
		configfile = PATHS.user_sources / 'kernel.config'
		if not configfile.exists():
			LOGGER.warning('No source file named "kernel.config".')
		else:
			user_config_hash = base.hash_file('sha256', open(configfile, 'rb')).hexdigest()
			if self.statefile.state.get('user_config_hash', None) == user_config_hash:
				LOGGER.info('The user config file has not changed.')
			else:
				with self.statefile as state:
					state['user_config_hash'] = None
					state['built_config_hash'] = None
					try:
						shutil.copyfile(configfile, linuxdir / '.config')
					except Exception as e:
						base.fail(LOGGER, 'Unable to copy kernel.config source file.', e)
					state['user_config_hash'] = user_config_hash

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

		# Provide our kernel config as an output.
		shutil.copyfile(linuxdir / '.config', PATHS.output / 'kernel.config')
		LOGGER.warning(
		    'The output file `kernel.config` has been created.  You must manually copy this to your sources directory.'
		)

	def menuconfig(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
			return  # We're bypassed.

		linuxdir = PATHS.build / 'linux'

		if not (linuxdir / '.config').exists():
			base.fail(LOGGER, 'No kernel configuration file was found.  Use kernel:defconfig to generate one.')

		LOGGER.info('Running `menuconfig`...')
		try:
			base.run(
			    PATHS,
			    LOGGER, ['make'] + self.kbuild_args + ['menuconfig'],
			    cwd=linuxdir,
			    stdin=None,
			    stdout=None,
			    stderr=None
			)
		except subprocess.CalledProcessError:
			base.fail(LOGGER, 'Kernel `menuconfig` returned with an error')
		LOGGER.info('Finished `menuconfig`.')

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

		built_config_hash = base.hash_file('sha256', open(linuxdir / '.config', 'rb')).hexdigest()
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
				state['built_config_hash'] = base.hash_file('sha256', open(linuxdir / '.config', 'rb')).hexdigest()

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

		try:
			import pkg_resources
			specfile = pkg_resources.resource_string(__name__, "binkernel.spec")
			with open(PATHS.build / 'binkernel.spec', 'wb') as fd:
				fd.write(specfile)
		except ImportError as e:
			base.fail(
			    LOGGER,
			    'The python pkg_resources module is not available, so we cannot access our bundled RPM spec file.'
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
