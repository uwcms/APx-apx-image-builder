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
from typing import IO, Dict, List, Optional, Tuple

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
		self.STAGES['build'] = base.Stage(
		    self,
		    'build',
		    self.check,
		    self.build,
		    requires=[
		        self.NAME + ':clean',
		        self.NAME + ':distclean',
		        'fsbl:build',
		        'pmu:build',
		        'atf:build',
		        'dtb:build',
		        'u-boot:build',
		        'kernel:build',
		        'rootfs:build',
		    ]
		)

	def check(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> bool:
		if base.check_bypass(STAGE, PATHS, LOGGER, extract=False):
			return True  # We're bypassed.

		check_ok: bool = True
		if not shutil.which('bootgen'):
			LOGGER.error(f'Unable to locate `bootgen`.  Did you source the Vivado environment files?')
			check_ok = False
		if not shutil.which('mkimage'):
			LOGGER.error(f'Unable to locate `mkimage`.  Is uboot-tools (CentOS) or u-boot-tools (ubuntu) installed?')
			check_ok = False
		if not shutil.which('unzip'):
			LOGGER.error(f'Unable to locate `unzip`.')
			check_ok = False
		if not shutil.which('gzip'):
			LOGGER.error(f'Unable to locate `gzip`.')
			check_ok = False
		return check_ok

	def build(self, STAGE: base.Stage, PATHS: base.BuildPaths, LOGGER: logging.Logger) -> None:
		if base.check_bypass(STAGE, PATHS, LOGGER):
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
		with open(PATHS.build / 'boot.bif', 'w') as fd:
			fd.write(bif)

		base.import_source(PATHS, LOGGER, self.ARGS, 'qspi.boot.scr', 'boot.scr', optional=True)
		LOGGER.info('Importing prior build products...')
		built_sources = [
		    'fsbl:fsbl.elf',
		    'pmu:pmufw.elf',
		    'atf:bl31.elf',
		    'dtb:system.dtb',
		    'u-boot:u-boot.elf',
		    'kernel:Image.gz',
		    'rootfs:rootfs.cpio.uboot',
		]
		for builder, source in (x.split(':', 1) for x in built_sources):
			base.import_source(
			    PATHS, LOGGER, self.ARGS, PATHS.respecialize(builder).output / source, source, quiet=True
			)

		LOGGER.info('Parsing flash partition scheme from dts')
		try:
			base.run(PATHS, LOGGER, ['dtc', '-I', 'dtb', '-O', 'dts', 'system.dtb', '-o', 'system.dts'])
		except subprocess.CalledProcessError:
			base.fail(LOGGER, '`dtc` returned with an error')
		partition_spec = parse_dts_partitions(open(PATHS.build / 'system.dts', 'r'))

		bootscr = PATHS.build / 'boot.scr'
		if not bootscr.exists():
			LOGGER.info('Generating boot.scr automatically.')
			kernel_address: Tuple[int, int, int] = partition_spec.get('kernel', (0, 0, 0))
			rootfs_address: Tuple[int, int, int] = partition_spec.get('rootfs', (0, 0, 0))
			if not kernel_address[2] or not rootfs_address[2]:
				base.fail(
				    LOGGER,
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

		base.import_source(PATHS, LOGGER, self.ARGS, 'system.xsa', 'system.xsa')
		xsadir = PATHS.build / 'xsa'
		shutil.rmtree(xsadir, ignore_errors=True)
		xsadir.mkdir()
		LOGGER.info('Extracting XSA...')
		try:
			base.run(PATHS, LOGGER, ['unzip', '-x', '../system.xsa'], cwd=xsadir)
		except subprocess.CalledProcessError:
			base.fail(LOGGER, '`unzip` returned with an error')
		bitfiles = list(xsadir.glob('*.bit'))
		if len(bitfiles) != 1:
			base.fail(LOGGER, f'Expected exactly one bitfile in the XSA.  Found {bitfiles!r}')
		shutil.move(str(bitfiles[0].resolve()), PATHS.build / 'system.bit')

		LOGGER.info('Generating BOOT.BIN')
		try:
			base.run(
			    PATHS, LOGGER, [
			        'bootgen', '-o', 'BOOT.BIN', '-w', 'on', '-image', 'boot.bif', '-arch',
			        self.COMMON_CONFIG['zynq_series']
			    ]
			)
		except subprocess.CalledProcessError:
			base.fail(LOGGER, '`bootgen` returned with an error')

		LOGGER.info('Extracting kernel image')
		try:
			base.run(PATHS, LOGGER, ['gzip', '-d', '-f', 'Image.gz'])
		except subprocess.CalledProcessError:
			base.fail(LOGGER, '`gzip` returned with an error')

		LOGGER.info('Generating boot.scr FIT image')
		try:
			base.run(
			    PATHS, LOGGER, ['mkimage', '-c', 'none', '-A', 'arm', '-T', 'script', '-d', 'boot.scr', 'boot.scr.ub']
			)
		except subprocess.CalledProcessError:
			base.fail(LOGGER, '`mkimage` returned with an error')

		# Provide our outputs
		outputs = [
		    ('BOOT.BIN', 'BOOT.BIN', 'boot'),
		    ('boot.scr.ub', 'bootscr.ub', 'bootscr'),
		    ('Image', 'kernel.bin', 'kernel'),
		    ('rootfs.cpio.uboot', 'rootfs.ub', 'rootfs'),
		]
		for outputfn, file, _ in outputs:
			output = PATHS.build / outputfn
			if not output.exists():
				base.fail(LOGGER, outputfn + ' not found after build.')
			shutil.copyfile(output, PATHS.output / file)

		# Let's generate a basic convenient "flash.sh" script.
		LOGGER.info('Generating flash.sh helper script.')
		partition_files: List[Tuple[int, str]] = []
		for _, file, partition in outputs:
			if partition not in partition_spec:
				LOGGER.info(f'Unable to generate flash.sh: Could not locate {partition} partition.')
				from pprint import pprint
				pprint(partition_spec)
				partition_files = []
				break
			partition_files.append((partition_spec[partition][0], file))

		if partition_files:
			base.import_source(
			    PATHS, LOGGER, self.ARGS, 'builtin:///qspi_data/flash.sh', 'flash.template.sh', quiet=True
			)
			with open(PATHS.build / 'flash.sh', 'w') as fd:
				template = open(PATHS.build / 'flash.template.sh', 'r').read()
				fd.write(
				    template.replace(
				        '###PARTITIONS###',
				        ' '.join('{0}:{1}'.format(*partpair) for partpair in partition_files),
				    )
				)
			shutil.copyfile(PATHS.build / 'flash.sh', PATHS.output / 'flash.sh')
			(PATHS.output / 'flash.sh').chmod(0o755)


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
