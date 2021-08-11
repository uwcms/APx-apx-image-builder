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


class QSPIBuilder(base.BaseBuilder):
	NAME: str = 'qspi'
	makeflags: List[str]

	@classmethod
	def prepare_argparse(cls, group: argparse._ArgumentGroup) -> None:
		group.description = '''
Build a QSPI boot image.

Stages available:
  build: Build the QSPI boot image.
'''.strip()

	def instantiate_stages(self) -> None:
		super().instantiate_stages()
		requirements: List[str] = [
		    self.NAME + ':clean', self.NAME + ':distclean', 'fsbl:build', 'dtb:build', 'u-boot:build', 'kernel:build',
		    'rootfs:build'
		]

		if self.COMMON_CONFIG.get('zynq_series', '') == 'zynqmp':
			requirements.extend(['pmu:build', 'atf:build'])

		self.STAGES['build'] = base.Stage(self, 'build', self.check, self.build, requires=requirements)

	def check(self, STAGE: base.Stage) -> bool:
		if base.check_bypass(STAGE, extract=False):
			return True  # We're bypassed.

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
		if base.check_bypass(STAGE):
			return  # We're bypassed.

		dtb_address = self.BUILDER_CONFIG.get('dtb_address', 0x00100000)
		bif = textwrap.dedent(
		    '''
		the_ROM_image:
		{{
			[bootloader, destination_cpu=a53-0] fsbl.elf
			[pmufw_image] pmufw.elf
			[destination_device=pl] system.bit
			[destination_cpu=a53-0, exception_level=el-3, trustzone] bl31.elf
			[destination_cpu=a53-0, load=0x{dtb_address:08x}] system.dtb
			[destination_cpu=a53-0, exception_level=el-2] u-boot.elf
		}}
		'''
		).format(dtb_address=dtb_address)
		with open(self.PATHS.build / 'boot.bif', 'w') as fd:
			fd.write(bif)

		base.import_source(STAGE, 'qspi.boot.scr', 'boot.scr', optional=True)
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

		STAGE.logger.info('Parsing flash partition scheme from dts')
		try:
			base.run(STAGE, ['dtc', '-I', 'dtb', '-O', 'dts', 'system.dtb', '-o', 'system.dts'])
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, '`dtc` returned with an error')
		partition_spec = parse_dts_partitions(open(self.PATHS.build / 'system.dts', 'r'))

		bootscr = self.PATHS.build / 'boot.scr'
		if not bootscr.exists():
			STAGE.logger.info('Generating boot.scr automatically.')
			kernel_address: Tuple[int, int, int] = partition_spec.get('kernel', (0, 0, 0))
			rootfs_address: Tuple[int, int, int] = partition_spec.get('rootfs', (0, 0, 0))
			if not kernel_address[2] or not rootfs_address[2]:
				base.fail(
				    STAGE.logger,
				    'Unable to find "kernel" and "rootfs" partitions in the device tree.  Please manually supply `qspi.boot.scr`.'
				)
			with open(bootscr, 'w') as fd:
				fd.write(
				    textwrap.dedent(
				        '''
						sf read 0x00200000 0x{kernel_address[1]:08x} 0x{kernel_address[2]:08x};
						sf read 0x04000000 0x{rootfs_address[1]:08x} 0x{rootfs_address[2]:08x};
						booti 0x00200000 0x04000000 0x{dtb_address:08x}'''
				    ).strip().
				    format(kernel_address=kernel_address, rootfs_address=rootfs_address, dtb_address=dtb_address) + '\n'
				)

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

		STAGE.logger.info('Generating BOOT.BIN')
		try:
			base.run(
			    STAGE, [
			        'bootgen', '-o', 'BOOT.BIN', '-w', 'on', '-image', 'boot.bif', '-arch',
			        self.COMMON_CONFIG['zynq_series']
			    ]
			)
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, '`bootgen` returned with an error')

		STAGE.logger.info('Generating boot.scr FIT image')
		try:
			base.run(STAGE, ['mkimage', '-c', 'none', '-A', 'arm', '-T', 'script', '-d', 'boot.scr', 'boot.scr.ub'])
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, '`mkimage` returned with an error')

		# Provide our outputs
		outputs = [
		    ('BOOT.BIN', 'BOOT.BIN', 'boot'),
		    ('boot.scr.ub', 'bootscr.ub', 'bootscr'),
		    ('Image', 'kernel.bin', 'kernel'),
		    ('rootfs.cpio.uboot', 'rootfs.ub', 'rootfs'),
		]
		for outputfn, file, _ in outputs:
			output = self.PATHS.build / outputfn
			if not output.exists():
				base.fail(STAGE.logger, outputfn + ' not found after build.')
			shutil.copyfile(output, self.PATHS.output / file)

		# Let's generate a basic convenient "flash.sh" script.
		STAGE.logger.info('Generating flash.sh helper script.')
		partition_files: List[Tuple[int, str]] = []
		for _, file, partition in outputs:
			if partition not in partition_spec:
				STAGE.logger.info(f'Unable to generate flash.sh: Could not locate {partition} partition.')
				from pprint import pprint
				pprint(partition_spec)
				partition_files = []
				break
			partition_files.append((partition_spec[partition][0], file))

		if partition_files:
			base.import_source(STAGE, 'builtin:///qspi_data/flash.sh', 'flash.template.sh', quiet=True)
			with open(self.PATHS.build / 'flash.sh', 'w') as fd:
				template = open(self.PATHS.build / 'flash.template.sh', 'r').read()
				fd.write(
				    template.replace(
				        '###PARTITIONS###',
				        ' '.join('{0}:{1}'.format(*partpair) for partpair in partition_files),
				    )
				)
			shutil.copyfile(self.PATHS.build / 'flash.sh', self.PATHS.output / 'flash.sh')
			(self.PATHS.output / 'flash.sh').chmod(0o755)


def parse_dts_partitions(fd: IO[str]) -> Dict[str, Tuple[int, int, int]]:
	partid: Optional[int] = None
	partname: Optional[str] = None
	parts: Dict[str, Tuple[int, int, int]] = {}
	for line in fd:
		m = re.search(r'partition@([0-9]+)\s*{', line)
		if m is not None:
			partid = int(m.group(1))
		if '};' in line:
			partid = None
			partname = None
		if partid is not None:
			m = re.search(r'label\s*=\s*"([^"]+)"', line)
			if m is not None:
				partname = m.group(1)
			m = re.search(r'reg\s*=\s*<\s*([0-9a-fx]+)+\s+([0-9a-fx]+)\s*>', line)
			if m is not None and partname is not None:
				parts[partname] = (partid, int(m.group(1), 0), int(m.group(2), 0))
	return parts
