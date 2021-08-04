import argparse
import hashlib
import logging
import os
import shutil
import subprocess
import urllib.parse
from pathlib import Path
from typing import List, Optional

from . import base


class ATFBuilder(base.BaseBuilder):
	NAME: str = 'atf'
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

		statefile = base.JSONStateFile(PATHS.build / '.state.json')
		sourceurl: Optional[str] = self.BUILDER_CONFIG.get('atf_sourceurl', None)
		if sourceurl is None:
			sourceurl = 'https://github.com/Xilinx/arm-trusted-firmware/archive/refs/tags/{tag}.tar.gz'.format(
			    tag=self.BUILDER_CONFIG['atf_tag']
			)
		if base.import_source(PATHS, LOGGER, self.ARGS, sourceurl, PATHS.build / 'atf.tar.gz'):
			with statefile as state:
				state['tree_ready'] = False

	def prepare(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
			return  # We're bypassed.

		statefile = base.JSONStateFile(PATHS.build / '.state.json')
		atfdir = PATHS.build / 'atf'
		patcher = base.Patcher(PATHS.build / 'patches')
		if patcher.import_patches(PATHS, LOGGER, self.ARGS, self.BUILDER_CONFIG.get('patches', [])):
			with statefile as state:
				state['tree_ready'] = False
		if statefile.state.get('tree_ready', False):
			LOGGER.info('The ATF source tree has already been extracted.  Skipping.')
		else:
			base.untar(PATHS, LOGGER, PATHS.build / 'atf.tar.gz', atfdir)
			patcher.apply(PATHS, LOGGER, atfdir)
			with statefile as state:
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
