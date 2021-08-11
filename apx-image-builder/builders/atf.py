import argparse
import hashlib
import logging
import os
import shutil
import subprocess
import textwrap
import urllib.parse
from pathlib import Path
from typing import List, Optional

from . import base


class ATFBuilder(base.BaseBuilder):
	NAME: str = 'atf'
	makeflags: List[str]

	def prepare_argparse(self, group: argparse._ArgumentGroup) -> None:
		if self.COMMON_CONFIG.get('zynq_series', '') != 'zynqmp':
			group.description = 'Build the Arm Trusted Firmware (for ZynqMP only)'
			return
		group.description = textwrap.dedent(
		    '''
			Build the Arm Trusted Firmware (for ZynqMP only)

			Stages available:
			fetch: Download or copy sources.
			prepare: Extract sources.
			build: Build the Arm Trusted Firmware
			'''
		).strip()

	def instantiate_stages(self) -> None:
		super().instantiate_stages()
		if self.COMMON_CONFIG.get('zynq_series', '') != 'zynqmp':
			return
		self.STAGES['clean'] = base.Stage(self, 'clean', self.check, self.clean, include_in_all=False)
		self.STAGES['fetch'] = base.Stage(
		    self, 'fetch', self.check, self.fetch, after=[self.NAME + ':distclean', self.NAME + ':clean']
		)
		self.STAGES['prepare'] = base.Stage(self, 'prepare', self.check, self.prepare, requires=[self.NAME + ':fetch'])
		self.STAGES['build'] = base.Stage(self, 'build', self.check, self.build, requires=[self.NAME + ':prepare'])

	def check(self, STAGE: base.Stage) -> bool:
		if base.check_bypass(STAGE, extract=False):
			return True  # We're bypassed.

		check_ok: bool = True
		if self.COMMON_CONFIG.get('zynq_series', '') != 'zynqmp':
			STAGE.logger.error('Only ZynqMP chips support Arm Trusted Firmware.')
			return False
		if STAGE.name in (
		    'fetch', 'prepare') and 'atf_tag' not in self.BUILDER_CONFIG and 'atf_sourceurl' not in self.BUILDER_CONFIG:
			STAGE.logger.error(
			    'Please set a `atf_tag` or `atf_sourceurl` (file://... is valid) in the configuration for the "atf" builder.'
			)
			check_ok = False
		self.makeflags = self.BUILDER_CONFIG.get(
		    'makeflags', ['CROSS_COMPILE=aarch64-none-elf-', 'PLAT=zynqmp', 'RESET_TO_BL31=1']
		)
		cross_compile_args = [x.split('=', 1)[-1] for x in self.makeflags if x.startswith('CROSS_COMPILE=')]
		if len(cross_compile_args) != 1:
			STAGE.logger.error('Please supply CROSS_COMPILE=... in `makeflags`.')
			return False
		if not shutil.which(cross_compile_args[0] + 'gcc'):
			STAGE.logger.error(
			    'Unable to locate `{cross_compile}gcc`.  Did you source the Vivado environment files?'.format(
			        cross_compile=cross_compile_args[0]
			    )
			)
			check_ok = False
		return check_ok

	def fetch(self, STAGE: base.Stage) -> None:
		if base.check_bypass(STAGE):
			return  # We're bypassed.

		statefile = base.JSONStateFile(self.PATHS.build / '.state.json')
		sourceurl: Optional[str] = self.BUILDER_CONFIG.get('atf_sourceurl', None)
		if sourceurl is None:
			sourceurl = 'https://github.com/Xilinx/arm-trusted-firmware/archive/refs/tags/{tag}.tar.gz'.format(
			    tag=self.BUILDER_CONFIG['atf_tag']
			)
		if base.import_source(STAGE, sourceurl, self.PATHS.build / 'atf.tar.gz'):
			with statefile as state:
				state['tree_ready'] = False

	def prepare(self, STAGE: base.Stage) -> None:
		if base.check_bypass(STAGE):
			return  # We're bypassed.

		statefile = base.JSONStateFile(self.PATHS.build / '.state.json')
		atfdir = self.PATHS.build / 'atf'
		patcher = base.Patcher(self.PATHS.build / 'patches')
		if patcher.import_patches(STAGE, self.BUILDER_CONFIG.get('patches', [])):
			with statefile as state:
				state['tree_ready'] = False
		if statefile.state.get('tree_ready', False):
			STAGE.logger.info('The ATF source tree has already been extracted.  Skipping.')
		else:
			base.untar(STAGE, self.PATHS.build / 'atf.tar.gz', atfdir)
			patcher.apply(STAGE, atfdir)
			with statefile as state:
				state['tree_ready'] = True

	def build(self, STAGE: base.Stage) -> None:
		if base.check_bypass(STAGE):
			return  # We're bypassed.

		atfdir = self.PATHS.build / 'atf'
		STAGE.logger.info('Running `make`...')
		try:
			base.run(STAGE, ['make'] + self.makeflags, cwd=atfdir)
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, '`make` returned with an error')

		# Provide PMU firmware ELF as an output.
		atf = atfdir / 'build/zynqmp/release/bl31/bl31.elf'
		if not atf.exists():
			base.fail(STAGE.logger, 'bl31.elf not found after build.')
		base.copyfile(atf, self.PATHS.output / 'bl31.elf')

	def clean(self, STAGE: base.Stage) -> None:
		if base.check_bypass(STAGE, extract=False):
			return  # We're bypassed.

		STAGE.logger.info('Running `clean`...')
		try:
			base.run(STAGE, ['make', 'clean'], cwd=self.PATHS.build / 'workspace/pmufw')
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, '`clean` returned with an error')
		STAGE.logger.info('Finished `clean`.')
		STAGE.logger.info('Deleting outputs.')
		shutil.rmtree(self.PATHS.output, ignore_errors=True)
		self.PATHS.output.mkdir(parents=True, exist_ok=True)
