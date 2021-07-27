import argparse
import filecmp
import hashlib
import io
import itertools
import logging
import os
import shlex
import shutil
import subprocess
import textwrap
import time
import urllib.parse
from io import TextIOWrapper
from pathlib import Path
from typing import Any, Dict, List, Optional

from pkg_resources import require

from . import base


class ATFBuilder(base.BaseBuilder):
	NAME: str = 'atf'
	statefile: Optional[base.JSONStateFile] = None
	makeflags: List[str]

	@classmethod
	def prepare_argparse(cls, group: argparse._ArgumentGroup) -> None:
		group.description = '''
Build the Arm Trusted Firmware (for ZynqMP only)

Stages available:
  fetch: Download or copy sources.
  prepare: Extract sources.
  build: Build the Arm Trusted Firmware
'''.strip()

	def instantiate_stages(self) -> None:
		super().instantiate_stages()
		self.STAGES['clean'] = base.Stage(self, 'clean', self.check, self.clean, include_in_all=False)
		self.STAGES['fetch'] = base.Stage(
		    self, 'fetch', self.check, self.fetch, after=[self.NAME + ':distclean', self.NAME + ':clean']
		)
		self.STAGES['prepare'] = base.Stage(self, 'prepare', self.check, self.prepare, requires=[self.NAME + ':fetch'])
		self.STAGES['build'] = base.Stage(self, 'build', self.check, self.build, requires=[self.NAME + ':prepare'])

	def check(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> bool:
		if base.check_bypass(STAGE, PATHS, LOGGER, extract=False):
			return True  # We're bypassed.

		if self.statefile is None:
			self.statefile = base.JSONStateFile(PATHS.build / '.state.json')

		check_ok: bool = True
		if self.COMMON_CONFIG.get('zynq_series', '') != 'zynqmp':
			LOGGER.error('Only ZynqMP chips support Arm Trusted Firmware.')
			return False
		if STAGE.name in (
		    'fetch', 'prepare') and 'atf_tag' not in self.BUILDER_CONFIG and 'atf_sourceurl' not in self.BUILDER_CONFIG:
			LOGGER.error(
			    'Please set a `atf_tag` or `atf_sourceurl` (file://... is valid) in the configuration for the "atf" builder.'
			)
			check_ok = False
		self.makeflags = self.BUILDER_CONFIG.get(
		    'makeflags', ['CROSS_COMPILE=aarch64-none-elf-', 'PLAT=zynqmp', 'RESET_TO_BL31=1']
		)
		cross_compile_args = [x.split('=', 1)[-1] for x in self.makeflags if x.startswith('CROSS_COMPILE=')]
		if len(cross_compile_args) != 1:
			LOGGER.error('Please supply CROSS_COMPILE=... in `makeflags`.')
			return False
		if not shutil.which(cross_compile_args[0] + 'gcc'):
			LOGGER.error(
			    'Unable to locate `{cross_compile}gcc`.  Did you source the Vivado environment files?'.format(
			        cross_compile=cross_compile_args[0]
			    )
			)
			check_ok = False
		return check_ok

	def fetch(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
			return  # We're bypassed.

		assert self.statefile is not None
		sourceurl: Optional[str] = self.BUILDER_CONFIG.get('atf_sourceurl', None)
		if sourceurl is None:
			sourceurl = 'https://github.com/Xilinx/arm-trusted-firmware/archive/refs/tags/{tag}.tar.gz'.format(
			    tag=self.BUILDER_CONFIG['atf_tag']
			)
		sourceid = hashlib.new('sha256', sourceurl.encode('utf8')).hexdigest()
		tarfile = PATHS.build / 'atf-{sourceid}.tar.gz'.format(sourceid=sourceid)
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
		chosen_source = PATHS.build / 'atf.tar.gz'
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
		atfdir = PATHS.build / 'atf'
		if self.statefile.state.get('tree_ready', False):
			LOGGER.info('The ATF source tree has already been extracted.  Skipping.')
		else:
			LOGGER.debug('Removing any existing ATF source tree.')
			shutil.rmtree(atfdir, ignore_errors=True)
			LOGGER.info('Extracting the ATF source tree.')
			atfdir.mkdir()
			try:
				base.run(PATHS, LOGGER, ['tar', '-xf', str(PATHS.build / 'atf.tar.gz'), '-C', str(atfdir)])
			except subprocess.CalledProcessError:
				base.fail(LOGGER, 'Unable to extract ATF source tarball')
			while True:
				subdirs = [x for x in atfdir.glob('*') if x.is_dir()]
				if len(subdirs) == 1 and not (atfdir / 'Makefile').exists():
					LOGGER.debug(
					    'Found a single subdirectory {dir} and no Makefile.  Moving it up.'.format(
					        dir=repr(str(subdirs[0].name))
					    )
					)
					try:
						shutil.rmtree(str(atfdir) + '~', ignore_errors=True)
						os.rename(subdirs[0], Path(str(atfdir) + '~'))
						atfdir.rmdir()
						os.rename(Path(str(atfdir) + '~'), atfdir)
					except Exception as e:
						base.fail(LOGGER, 'Unable to relocate ATF source subdirectory.', e)
				else:
					break
			with self.statefile as state:
				state['tree_ready'] = True

	def build(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
			return  # We're bypassed.

		atfdir = PATHS.build / 'atf'
		LOGGER.info('Running `make`...')
		try:
			base.run(PATHS, LOGGER, ['make'] + self.makeflags, cwd=atfdir)
		except subprocess.CalledProcessError:
			base.fail(LOGGER, '`make` returned with an error')

		# Provide PMU firmware ELF as an output.
		atf = atfdir / 'build/zynqmp/release/bl31/bl31.elf'
		if not atf.exists():
			base.fail(LOGGER, 'bl31.elf not found after build.')
		shutil.copyfile(atf, PATHS.output / 'bl31.elf')

	def clean(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER, extract=False):
			return  # We're bypassed.

		LOGGER.info('Running `clean`...')
		try:
			base.run(PATHS, LOGGER, ['make', 'clean'], cwd=PATHS.build / 'workspace/pmufw')
		except subprocess.CalledProcessError:
			base.fail(LOGGER, '`clean` returned with an error')
		LOGGER.info('Finished `clean`.')
		LOGGER.info('Deleting outputs.')
		shutil.rmtree(PATHS.output, ignore_errors=True)
		PATHS.output.mkdir(parents=True, exist_ok=True)
