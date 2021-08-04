import argparse
import logging
import shutil
import subprocess
import textwrap
from typing import Any, Dict, Optional

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

		statefile = base.JSONStateFile(PATHS.build / '.state.json')
		# We'll need the XSA
		if base.import_source(PATHS, LOGGER, self.ARGS, 'system.xsa', PATHS.build / 'system.xsa'):
			with statefile as state:
				state['project_generated'] = False
		patcher = base.Patcher(PATHS.build / 'patches')
		if patcher.import_patches(PATHS, LOGGER, self.ARGS, self.BUILDER_CONFIG.get('patches', [])):
			with statefile as state:
				state['project_generated'] = False

		if statefile.state.get('project_generated', False):
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
			patcher.apply(PATHS, LOGGER, PATHS.build / 'workspace/pmufw')
			with statefile as state:
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
