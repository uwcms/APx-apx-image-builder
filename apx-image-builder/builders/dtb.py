import argparse
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Any, Dict, Optional

from . import base


class DTBBuilder(base.BaseBuilder):
	NAME: str = 'dtb'

	def __init__(self, config: Dict[str, Any], paths: base.BuildPaths, ARGS: argparse.Namespace):
		super().__init__(config, paths, ARGS)
		if self.COMMON_CONFIG.get('zynq_series', '') == 'zynq':
			self.BUILDER_CONFIG.setdefault('cpu_id', 'ps7_cortexa9_0')
		elif self.COMMON_CONFIG.get('zynq_series', '') == 'zynqmp':
			self.BUILDER_CONFIG.setdefault('cpu_id', 'psu_cortexa53_0')

	def prepare_argparse(self, group: argparse._ArgumentGroup) -> None:
		group.description = textwrap.dedent(
		    '''
			Build the Device Tree

			Stages available:
			fetch: Download or copy device-tree generator sources
			prepare: Extract DTG sources, generate automatic dts files, copy user dts files.
			build: Build the device tree
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
		self.STAGES['build'] = base.BypassableStage(
		    self, 'build', self.check, self.build, requires=[self.NAME + ':prepare']
		)

	def check(self, STAGE: base.Stage) -> bool:
		check_ok: bool = True
		if STAGE.name in (
		    'fetch', 'prepare') and 'dtg_tag' not in self.BUILDER_CONFIG and 'dtg_sourceurl' not in self.BUILDER_CONFIG:
			STAGE.logger.error(
			    'Please set a `dtg_tag` or `dtg_sourceurl` (file://... is valid) in the configuration for the "dtg" builder.'
			)
			check_ok = False
		if not shutil.which('dtc'):
			STAGE.logger.error('dtc not found. Did you source the Vivado environment files?')
			check_ok = False
		return check_ok

	def fetch(self, STAGE: base.Stage) -> None:
		statefile = base.JSONStateFile(self.PATHS.build / '.state.json')
		sourceurl: Optional[str] = self.BUILDER_CONFIG.get('dtg_sourceurl', None)
		if sourceurl is None:
			sourceurl = 'https://github.com/Xilinx/device-tree-xlnx/archive/refs/tags/{tag}.tar.gz'.format(
			    tag=self.BUILDER_CONFIG['dtg_tag']
			)
		if base.import_source(STAGE, sourceurl, self.PATHS.build / 'dtg.tar.gz'):
			with statefile as state:
				state['tree_ready'] = False

	def prepare(self, STAGE: base.Stage) -> None:
		statefile = base.JSONStateFile(self.PATHS.build / '.state.json')

		# We'll need the XSA
		if base.import_source(STAGE, 'system.xsa', self.PATHS.build / 'system.xsa'):
			with statefile as state:
				state['dts_generated'] = False
		patcher = base.Patcher(self.PATHS.build / 'patches')
		if patcher.import_patches(STAGE, self.BUILDER_CONFIG.get('patches', [])):
			with statefile as state:
				state['dts_generated'] = False

		# We'll need the DTG source repository.
		if not statefile.state.get('tree_ready', False):
			base.untar(STAGE, self.PATHS.build / 'dtg.tar.gz', self.PATHS.build / 'dtg')
			patcher.apply(STAGE, self.PATHS.build / 'dtg')
			with statefile as state:
				state['tree_ready'] = True

		# We'll need to generate the automatic dts files.
		dtsdir = self.PATHS.build / 'dts'
		if statefile.state.get('dts_generated', False):
			STAGE.logger.info('The automatic dts files have already been generated.')
		else:
			workdir = tempfile.TemporaryDirectory(prefix='xsi_workdir')
			shutil.rmtree(dtsdir, ignore_errors=True)

			base.copyfile(self.PATHS.build / 'system.xsa', Path(workdir.name) / 'system.xsa')
			xsct_script = textwrap.dedent(
			    """
			set hw_design [hsi open_hw_design system.xsa]
			hsi set_repo_path {builddir}/dtg
			hsi create_sw_design device-tree -os device_tree -proc {cpu_id}
			hsi generate_target -dir dts
			"""
			).strip().format(
			    builddir=self.PATHS.build.resolve(), **self.BUILDER_CONFIG
			)

			try:
				base.run(
				    STAGE,
				    ['xsct'],
				    stdin=xsct_script,
				    cwd=workdir.name,
				)
			except subprocess.CalledProcessError:
				base.fail(STAGE.logger, 'Unable to generate automatic dts files.')
			with statefile as state:
				state['dts_generated'] = True

			shutil.move(str(Path(workdir.name) / 'dts'), dtsdir)

			STAGE.logger.debug('Appending #include for system-user.dtsi.')
			with open(dtsdir / 'system-top.dts', 'a') as fd:
				fd.write('\n#include "system-user.dtsi"')

		STAGE.logger.info('Importing source: system-user.dtsi')
		configfile = self.PATHS.user_sources / 'system-user.dtsi'
		if not configfile.exists():
			base.fail(STAGE.logger, 'No source file named "system-user.dtsi".')
		else:
			base.copyfile(configfile, dtsdir / 'system-user.dtsi')

	def build(self, STAGE: base.Stage) -> None:
		dtsdir = self.PATHS.build / 'dts'
		STAGE.logger.info('Running `cpp` to generate the composite dts')
		try:
			base.run(
			    STAGE,
			    ['cpp', '-nostdinc', '-undef', '-x', 'assembler-with-cpp', 'system-top.dts', '-o', 'composite.dts'],
			    cwd=dtsdir
			)
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, '`dtc` returned with an error')

		STAGE.logger.info('Running `dtc` to generate dtb')
		try:
			base.run(STAGE, ['dtc', '-I', 'dts', '-O', 'dtb', '-o', 'composite.dtb', 'composite.dts'], cwd=dtsdir)
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, '`dtc` returned with an error')

		# Provide composite dts as an output.
		base.copyfile(dtsdir / 'composite.dts', self.PATHS.output / 'system.dts')
		# Provide dtb as an output.
		base.copyfile(dtsdir / 'composite.dtb', self.PATHS.output / 'system.dtb')

	def clean(self, STAGE: base.Stage) -> None:
		STAGE.logger.info('Deleting device-tree source files.')
		shutil.rmtree(self.PATHS.build / 'dts', ignore_errors=True)
		STAGE.logger.info('Deleting outputs.')
		shutil.rmtree(self.PATHS.output, ignore_errors=True)
		self.PATHS.output.mkdir(parents=True, exist_ok=True)
