import argparse
import logging
import shutil
import subprocess
import textwrap
from typing import Any, Dict, Optional

from . import base


class FSBLBuilder(base.BaseBuilder):
	NAME: str = 'fsbl'
	statefile: Optional[base.JSONStateFile] = None

	def __init__(self, config: Dict[str, Any], paths: base.BuildPaths, ARGS: argparse.Namespace):
		super().__init__(config, paths, ARGS)
		if self.COMMON_CONFIG.get('zynq_series', '') == 'zynq':
			self.BUILDER_CONFIG.setdefault('cpu_id', 'ps7_cortexa9_0')
			self.BUILDER_CONFIG.setdefault('app_name', 'zynq_fsbl')
		elif self.COMMON_CONFIG.get('zynq_series', '') == 'zynqmp':
			self.BUILDER_CONFIG.setdefault('cpu_id', 'psu_cortexa53_0')
			self.BUILDER_CONFIG.setdefault('app_name', 'zynqmp_fsbl')

	def prepare_argparse(self, group: argparse._ArgumentGroup) -> None:
		group.description = textwrap.dedent(
		    '''
			Build the FSBL

			Stages available:
			prepare: Generate sources from Vivado and import the hardware project.
			build: Build the FSBL
			'''
		).strip()

	def instantiate_stages(self) -> None:
		super().instantiate_stages()
		self.STAGES['clean'] = base.Stage(self, 'clean', self.check, self.clean, include_in_all=False)
		self.STAGES['prepare'] = base.Stage(
		    self, 'prepare', self.check, self.prepare, after=[self.NAME + ':distclean', self.NAME + ':clean']
		)
		self.STAGES['build'] = base.Stage(self, 'build', self.check, self.build, requires=[self.NAME + ':prepare'])

	def check(self, STAGE: base.Stage) -> bool:
		if base.check_bypass(STAGE, extract=False):
			return True  # We're bypassed.

		check_ok: bool = True
		if not shutil.which('vivado'):
			STAGE.logger.error('Vivado not found. Please source your Vivado settings.sh file.')
			check_ok = False
		return check_ok

	def prepare(self, STAGE: base.Stage) -> None:
		if base.check_bypass(STAGE):
			return  # We're bypassed.

		statefile = base.JSONStateFile(self.PATHS.build / '.state.json')
		# We'll need the XSA
		if base.import_source(STAGE, 'system.xsa', self.PATHS.build / 'system.xsa'):
			with statefile as state:
				state['project_generated'] = False
		patcher = base.Patcher(self.PATHS.build / 'patches')
		if patcher.import_patches(STAGE, self.BUILDER_CONFIG.get('patches', [])):
			with statefile as state:
				state['project_generated'] = False

		if statefile.state.get('project_generated', False):
			STAGE.logger.info('The FSBL project has already been generated.  Skipping.')
		else:
			STAGE.logger.debug('Removing any existing FSBL project.')
			shutil.rmtree((self.PATHS.build / 'workspace'), ignore_errors=True)
			STAGE.logger.info('Generating FSBL project.')
			(self.PATHS.build / 'workspace').mkdir()

			xsct_script = textwrap.dedent(
			    """
			set hw_design [hsi open_hw_design ../system.xsa]
			hsi generate_app -hw $hw_design -os standalone -proc {cpu_id} -app {app_name} -sw fsbl -dir fsbl
			"""
			).strip().format(**self.BUILDER_CONFIG)

			base.run(
			    STAGE,
			    ['xsct'],
			    stdin=xsct_script,
			    cwd=self.PATHS.build / 'workspace',
			)
			patcher.apply(STAGE, self.PATHS.build / 'workspace/fsbl')
			with statefile as state:
				state['project_generated'] = True

	def build(self, STAGE: base.Stage) -> None:
		if base.check_bypass(STAGE):
			return  # We're bypassed.

		STAGE.logger.info('Running `make`...')
		try:
			base.run(
			    STAGE, ['make'] + self.BUILDER_CONFIG.get('extra_makeflags', []),
			    cwd=self.PATHS.build / 'workspace' / 'fsbl'
			)
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, '`make` returned with an error')

		# Provide fsbl ELF as an output.
		elfs = list(self.PATHS.build.glob('workspace/fsbl/*.elf'))
		if len(elfs) != 1:
			base.fail(STAGE.logger, 'Found multiple elf files after build: ' + ' '.join(elf.name for elf in elfs))
		shutil.copyfile(elfs[0], self.PATHS.output / 'fsbl.elf')

	def clean(self, STAGE: base.Stage) -> None:
		if base.check_bypass(STAGE, extract=False):
			return  # We're bypassed.

		STAGE.logger.info('Running `clean`...')
		try:
			base.run(STAGE, ['make', 'clean'], cwd=self.PATHS.build / 'workspace/fsbl')
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, '`clean` returned with an error')
		STAGE.logger.info('Finished `clean`.')
		STAGE.logger.info('Deleting outputs.')
		shutil.rmtree(self.PATHS.output, ignore_errors=True)
		self.PATHS.output.mkdir(parents=True, exist_ok=True)
