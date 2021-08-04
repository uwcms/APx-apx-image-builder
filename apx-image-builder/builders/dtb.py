import argparse
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import textwrap
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional

from . import base


class DTBBuilder(base.BaseBuilder):
	NAME: str = 'dtb'

	def update_config(self, config: Dict[str, Any], ARGS: argparse.Namespace) -> None:
		super().update_config(config, ARGS)
		if self.COMMON_CONFIG.get('zynq_series', '') == 'zynq':
			self.BUILDER_CONFIG.setdefault('cpu_id', 'ps7_cortexa9_0')
		elif self.COMMON_CONFIG.get('zynq_series', '') == 'zynqmp':
			self.BUILDER_CONFIG.setdefault('cpu_id', 'psu_cortexa53_0')

	@classmethod
	def prepare_argparse(cls, group: argparse._ArgumentGroup) -> None:
		group.description = '''
Build the Device Tree

Stages available:
  fetch: Download or copy device-tree generator sources
  prepare: Extract DTG sources, generate automatic dts files, copy user dts files.
  build: Build the device tree
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
		if STAGE.name in (
		    'fetch', 'prepare') and 'dtg_tag' not in self.BUILDER_CONFIG and 'dtg_sourceurl' not in self.BUILDER_CONFIG:
			LOGGER.error(
			    'Please set a `dtg_tag` or `dtg_sourceurl` (file://... is valid) in the configuration for the "dtg" builder.'
			)
			check_ok = False
		if not shutil.which('dtc'):
			LOGGER.error('dtc not found. Did you source the Vivado environment files?')
			check_ok = False
		return check_ok

	def fetch(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
			return  # We're bypassed.

		statefile = base.JSONStateFile(PATHS.build / '.state.json')
		sourceurl: Optional[str] = self.BUILDER_CONFIG.get('dtg_sourceurl', None)
		if sourceurl is None:
			sourceurl = 'https://github.com/Xilinx/device-tree-xlnx/archive/refs/tags/{tag}.tar.gz'.format(
			    tag=self.BUILDER_CONFIG['dtg_tag']
			)
		if base.import_source(PATHS, LOGGER, self.ARGS, sourceurl, PATHS.build / 'dtg.tar.gz'):
			with statefile as state:
				state['tree_ready'] = False

	def prepare(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
			return  # We're bypassed.

		statefile = base.JSONStateFile(PATHS.build / '.state.json')

		# We'll need the XSA
		if base.import_source(PATHS, LOGGER, self.ARGS, 'system.xsa', PATHS.build / 'system.xsa'):
			with statefile as state:
				state['dts_generated'] = False
		patcher = base.Patcher(PATHS.build / 'patches')
		if patcher.import_patches(PATHS, LOGGER, self.ARGS, self.BUILDER_CONFIG.get('patches', [])):
			with statefile as state:
				state['dts_generated'] = False

		# We'll need the DTG source repository.
		if not statefile.state.get('tree_ready', False):
			base.untar(PATHS, LOGGER, PATHS.build / 'dtg.tar.gz', PATHS.build / 'dtg')
			patcher.apply(PATHS, LOGGER, PATHS.build / 'dtg')
			with statefile as state:
				state['tree_ready'] = True

		# We'll need to generate the automatic dts files.
		dtsdir = PATHS.build / 'dts'
		if statefile.state.get('dts_generated', False):
			LOGGER.info('The automatic dts files have already been generated.')
		else:
			workdir = tempfile.TemporaryDirectory(prefix='xsi_workdir')
			shutil.rmtree(dtsdir, ignore_errors=True)

			shutil.copyfile(PATHS.build / 'system.xsa', Path(workdir.name) / 'system.xsa')
			xsct_script = textwrap.dedent(
			    """
			set hw_design [hsi open_hw_design system.xsa]
			hsi set_repo_path {builddir}/dtg
			hsi create_sw_design device-tree -os device_tree -proc {cpu_id}
			hsi generate_target -dir dts
			"""
			).strip().format(
			    builddir=PATHS.build.resolve(), **self.BUILDER_CONFIG
			)

			try:
				base.run(
				    PATHS,
				    LOGGER,
				    ['xsct'],
				    stdin=xsct_script,
				    cwd=workdir.name,
				)
			except subprocess.CalledProcessError:
				base.fail(LOGGER, 'Unable to generate automatic dts files.')
			with statefile as state:
				state['dts_generated'] = True

			shutil.move(str(Path(workdir.name) / 'dts'), dtsdir)

			LOGGER.debug('Appending #include for system-user.dtsi.')
			with open(dtsdir / 'system-top.dts', 'a') as fd:
				fd.write('\n#include "system-user.dtsi"')

		LOGGER.info('Importing source: system-user.dtsi')
		configfile = PATHS.user_sources / 'system-user.dtsi'
		if not configfile.exists():
			base.fail(LOGGER, 'No source file named "system-user.dtsi".')
		else:
			shutil.copyfile(configfile, dtsdir / 'system-user.dtsi')

	def build(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
			return  # We're bypassed.

		dtsdir = PATHS.build / 'dts'
		LOGGER.info('Running `cpp` to generate the composite dts')
		try:
			base.run(
			    PATHS,
			    LOGGER,
			    ['cpp', '-nostdinc', '-undef', '-x', 'assembler-with-cpp', 'system-top.dts', '-o', 'composite.dts'],
			    cwd=dtsdir
			)
		except subprocess.CalledProcessError:
			base.fail(LOGGER, '`dtc` returned with an error')

		LOGGER.info('Running `dtc` to generate dtb')
		try:
			base.run(
			    PATHS, LOGGER, ['dtc', '-I', 'dts', '-O', 'dtb', '-o', 'composite.dtb', 'composite.dts'], cwd=dtsdir
			)
		except subprocess.CalledProcessError:
			base.fail(LOGGER, '`dtc` returned with an error')

		# Provide composite dts as an output.
		shutil.copyfile(dtsdir / 'composite.dts', PATHS.output / 'system.dts')
		# Provide dtb as an output.
		shutil.copyfile(dtsdir / 'composite.dtb', PATHS.output / 'system.dtb')

	def clean(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER, extract=False):
			return  # We're bypassed.

		LOGGER.info('Deleting device-tree source files.')
		shutil.rmtree(PATHS.build / 'dts', ignore_errors=True)
		LOGGER.info('Deleting outputs.')
		shutil.rmtree(PATHS.output, ignore_errors=True)
		PATHS.output.mkdir(parents=True, exist_ok=True)
