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


class PMUBuilder(base.BaseBuilder):
	NAME: str = 'pmu'
	statefile: Optional[base.JSONStateFile] = None

	def update_config(self, config: Dict[str, Any], ARGS: argparse.Namespace) -> None:
		super().update_config(config, ARGS)
		# if self.COMMON_CONFIG.get('zynq_series', '') == 'zynq':
		# 	self.BUILDER_CONFIG.setdefault('cpu_id', 'ps7_cortexa9_0')
		# 	self.BUILDER_CONFIG.setdefault('app_name', 'zynq_fsbl')
		if self.COMMON_CONFIG.get('zynq_series', '') == 'zynqmp':
			self.BUILDER_CONFIG.setdefault('cpu_id', 'psu_pmu_0')
			self.BUILDER_CONFIG.setdefault('app_name', 'zynqmp_pmufw')

	@classmethod
	def prepare_argparse(cls, group: argparse._ArgumentGroup) -> None:
		group.description = '''
Build the PMU firmware (for ZynqMP only)

Stages available:
  prepare: Generate sources from Vivado and import the hardware project.
  build: Build the PMU firmware
'''.strip()

	def instantiate_stages(self) -> None:
		super().instantiate_stages()
		self.STAGES['clean'] = base.Stage(self, 'clean', self.check, self.clean, include_in_all=False)
		self.STAGES['prepare'] = base.Stage(
		    self, 'prepare', self.check, self.prepare, after=[self.NAME + ':distclean', self.NAME + ':clean']
		)
		self.STAGES['build'] = base.Stage(self, 'build', self.check, self.build, requires=[self.NAME + ':prepare'])

	def check(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> bool:
		if base.check_bypass(STAGE, PATHS, LOGGER, extract=False):
			return True  # We're bypassed.

		if self.statefile is None:
			self.statefile = base.JSONStateFile(PATHS.build / '.state.json')

		check_ok: bool = True
		if not shutil.which('vivado'):
			LOGGER.error('Vivado not found. Please source your Vivado settings.sh file.')
			check_ok = False
		if self.COMMON_CONFIG.get('zynq_series', '') != 'zynqmp':
			LOGGER.error('Only ZynqMP chips support PMU firmware.')
			check_ok = False
		return check_ok

	def prepare(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
			return  # We're bypassed.

		assert self.statefile is not None
		user_xsa = PATHS.user_sources / 'system.xsa'
		if not user_xsa.exists():
			base.fail(LOGGER, 'Unable to locate system.xsa in the sources directory.')
		user_xsa_hash = base.hash_file('sha256', open(user_xsa, 'rb')).hexdigest()
		if self.statefile.state.get('xsa_hash', None) == user_xsa_hash:
			LOGGER.info('The source system.xsa file has not changed.')
		else:
			LOGGER.info('Importing source: system.xsa')
			shutil.copyfile(user_xsa, PATHS.build / 'system.xsa')
			with self.statefile as state:
				state['xsa_hash'] = user_xsa_hash
				state['project_generated'] = False

		if self.statefile.state.get('project_generated', False):
			LOGGER.info('The PMU firmware project has already been generated.  Skipping.')
		else:
			LOGGER.debug('Removing any existing PMU firmware project.')
			shutil.rmtree((PATHS.build / 'workspace'), ignore_errors=True)
			LOGGER.info('Generating PMU firmware project.')
			(PATHS.build / 'workspace').mkdir()

			xsct_script = textwrap.dedent(
			    """
			set hw_design [hsi open_hw_design ../system.xsa]
			hsi generate_app -hw $hw_design -os standalone -proc {cpu_id} -app {app_name} -sw pmufw -dir pmufw
			"""
			).strip().format(**self.BUILDER_CONFIG)

			try:
				base.run(
				    PATHS,
				    LOGGER,
				    ['xsct'],
				    stdin=xsct_script,
				    cwd=PATHS.build / 'workspace',
				)
			except subprocess.CalledProcessError:
				base.fail(LOGGER, 'Unable to generate PMU firmware project')
			with self.statefile as state:
				state['project_generated'] = True

	def build(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
			return  # We're bypassed.

		LOGGER.info('Running `make`...')
		try:
			base.run(
			    PATHS,
			    LOGGER, ['make'] + self.BUILDER_CONFIG.get('extra_makeflags', []),
			    cwd=PATHS.build / 'workspace' / 'pmufw'
			)
		except subprocess.CalledProcessError:
			base.fail(LOGGER, '`make` returned with an error')

		# Provide PMU firmware ELF as an output.
		elfs = list(PATHS.build.glob('workspace/pmufw/*.elf'))
		if len(elfs) != 1:
			base.fail(LOGGER, 'Found multiple elf files after build: ' + ' '.join(elf.name for elf in elfs))
		shutil.copyfile(elfs[0], PATHS.output / 'pmufw.elf')

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
