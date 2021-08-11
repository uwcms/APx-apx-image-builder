import argparse
import hashlib
import logging
import os
import re
import shutil
import subprocess
import textwrap
import urllib.parse
from pathlib import Path
from typing import Any, IO, Dict, List, Optional, Tuple

from . import base


class JTAGBuilder(base.BaseBuilder):
	NAME: str = 'jtag'

	@classmethod
	def prepare_argparse(cls, group: argparse._ArgumentGroup) -> None:
		group.description = '''
Build a JTAG boot image.

Stages available:
  build: Build the JTAG boot image.
'''.strip()

	def instantiate_stages(self) -> None:
		super().instantiate_stages()
		requirements: List[str] = ['fsbl:build', 'dtb:build', 'u-boot:build', 'kernel:build', 'rootfs:build']

		if self.COMMON_CONFIG.get('zynq_series', '') == 'zynqmp':
			requirements.extend(['pmu:build', 'atf:build'])

		self.STAGES['build'] = base.BypassableStage(
		    self,
		    'build',
		    self.check,
		    self.build,
		    requires=requirements,
		    after=[self.NAME + ':clean', self.NAME + ':distclean'] + requirements
		)

	def check(self, STAGE: base.Stage) -> bool:
		check_ok: bool = True
		if not shutil.which('bootgen'):
			STAGE.logger.error(f'Unable to locate `bootgen`.  Did you source the Vivado environment files?')
			check_ok = False
		if not shutil.which('mkimage'):
			STAGE.logger.error(
			    f'Unable to locate `mkimage`.  Is uboot-tools (CentOS) or u-boot-tools (ubuntu) installed?'
			)
			check_ok = False
		if not shutil.which('unzip'):
			STAGE.logger.error(f'Unable to locate `unzip`.')
			check_ok = False
		if not shutil.which('gzip'):
			STAGE.logger.error(f'Unable to locate `gzip`.')
			check_ok = False
		return check_ok

	def build(self, STAGE: base.Stage) -> None:
		base.import_source(STAGE, 'jtag.boot.scr', 'boot.scr', optional=True)
		try:
			base.import_source(STAGE, 'jtag-boot.tcl', 'jtag-boot.tcl')
		except:
			STAGE.logger.error('jtag-boot.tcl must be provided by the user for licensing reasons.')
			raise

		STAGE.logger.info('Importing prior build products...')
		built_sources = [
		    'fsbl:fsbl.elf',
		    'pmu:pmufw.elf',
		    'atf:bl31.elf',
		    'dtb:system.dtb',
		    'u-boot:u-boot.elf',
		    'kernel:Image',
		    'rootfs:rootfs.cpio.uboot',
		]
		for builder, source in (x.split(':', 1) for x in built_sources):
			base.import_source(STAGE, self.PATHS.respecialize(builder).output / source, source, quiet=True)

		bootscr = self.PATHS.build / 'boot.scr'
		if not bootscr.exists():
			STAGE.logger.info('Generating boot.scr automatically.')
			with open(bootscr, 'w') as fd:
				fd.write('booti 0x00200000 0x04000000 0x00100000')

		base.import_source(STAGE, 'system.xsa', 'system.xsa')
		xsadir = self.PATHS.build / 'xsa'
		shutil.rmtree(xsadir, ignore_errors=True)
		xsadir.mkdir()
		STAGE.logger.info('Extracting XSA...')
		try:
			base.run(STAGE, ['unzip', '-x', '../system.xsa'], cwd=xsadir)
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, '`unzip` returned with an error')
		bitfiles = list(xsadir.glob('*.bit'))
		if len(bitfiles) != 1:
			base.fail(STAGE.logger, f'Expected exactly one bitfile in the XSA.  Found {bitfiles!r}')
		shutil.move(str(bitfiles[0].resolve()), self.PATHS.build / 'system.bit')
		shutil.move(str(xsadir / 'psu_init.tcl'), self.PATHS.build / 'psu_init.tcl')

		STAGE.logger.info('Generating boot.scr FIT image')
		try:
			base.run(STAGE, ['mkimage', '-c', 'none', '-A', 'arm', '-T', 'script', '-d', 'boot.scr', 'boot.scr.ub'])
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, '`mkimage` returned with an error')

		# Provide our outputs
		outputs = [
		    'system.bit',
		    'psu_init.tcl',
		    'fsbl.elf',
		    'pmufw.elf',
		    'bl31.elf',
		    'system.dtb',
		    'u-boot.elf',
		    'Image',
		    'rootfs.cpio.uboot',
		    'boot.scr.ub',
		    'jtag-boot.tcl',
		]
		for file in outputs:
			output = self.PATHS.build / file
			if not output.exists():
				base.fail(STAGE.logger, file + ' not found after build.')
			base.copyfile(output, self.PATHS.output / file)
