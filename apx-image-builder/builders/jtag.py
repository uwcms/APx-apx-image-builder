import argparse
import shutil
import subprocess
import textwrap
from typing import List

from . import base


class JTAGBuilder(base.BaseBuilder):
	NAME: str = 'jtag'

	@classmethod
	def prepare_argparse(cls, group: argparse._ArgumentGroup) -> None:
		group.description = textwrap.dedent(
		    '''
			Build a JTAG boot image.

			Stages available:
			- build: Build the JTAG boot image.
			'''
		).strip()

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
		if not shutil.which('mkimage'):
			STAGE.logger.error(
			    f'Unable to locate `mkimage`.  Is uboot-tools (CentOS) or u-boot-tools (ubuntu) installed?'
			)
			check_ok = False
		if not shutil.which('unzip'):
			STAGE.logger.error(f'Unable to locate `unzip`.')
			check_ok = False
		return check_ok

	def build(self, STAGE: base.Stage) -> None:
		base.import_source(STAGE, 'jtag.boot.scr', 'boot.scr', optional=True)
		try:
			base.import_source(STAGE, 'jtag-boot.tcl', 'jtag-boot.tcl')
		except:
			STAGE.logger.error('jtag-boot.tcl must be provided by the user for licensing reasons.')
			raise

		dtb_address = self.COMMON_CONFIG.get('dtb_address', 0x00100000)

		STAGE.logger.info('Importing prior build products...')
		built_sources = [
		    'fsbl:fsbl.elf',
		    'dtb:system.dtb',
		    'u-boot:u-boot.elf',
		    'rootfs:rootfs.cpio.uboot',
		]
		if self.COMMON_CONFIG.get('zynq_series', '') == 'zynqmp':
			built_sources.extend(['kernel:Image', 'pmu:pmufw.elf', 'atf:bl31.elf'])
			ps_init = 'psu_init.tcl'
		else:
			built_sources.extend(['kernel:zImage'])
			ps_init = 'ps7_init.tcl'
		for builder, source in (x.split(':', 1) for x in built_sources):
			base.import_source(STAGE, self.PATHS.respecialize(builder).output / source, source, quiet=True)

		bootscr = self.PATHS.build / 'boot.scr'
		if not bootscr.exists():
			STAGE.logger.info('Generating boot.scr automatically.')
			with open(bootscr, 'w') as fd:
				bootcmd = 'booti' if self.COMMON_CONFIG.get('zynq_series', '') == 'zynqmp' else 'bootz'
				fd.write(
				    '{bootcmd} ${{kernel_addr_r}} ${{ramdisk_addr_r}} 0x{dtb_address:08x}'.format(
				        bootcmd=bootcmd, dtb_address=dtb_address
				    )
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
		shutil.move(str(xsadir / ps_init), self.PATHS.build / ps_init)

		STAGE.logger.info('Generating boot.scr FIT image')
		try:
			base.run(STAGE, ['mkimage', '-c', 'none', '-A', 'arm', '-T', 'script', '-d', 'boot.scr', 'boot.scr.ub'])
		except subprocess.CalledProcessError:
			base.fail(STAGE.logger, '`mkimage` returned with an error')

		# Provide our outputs
		outputs = [
		    'system.bit',
		    ps_init,
		    'fsbl.elf',
		    'system.dtb',
		    'u-boot.elf',
		    'Image' if self.COMMON_CONFIG.get('zynq_series', '') == 'zynqmp' else 'zImage',
		    'rootfs.cpio.uboot',
		    'boot.scr.ub',
		    'jtag-boot.tcl',
		]
		if self.COMMON_CONFIG.get('zynq_series', '') == 'zynqmp':
			outputs.extend(['pmufw.elf', 'bl31.elf'])
		for file in outputs:
			output = self.PATHS.build / file
			if not output.exists():
				base.fail(STAGE.logger, file + ' not found after build.')
			base.copyfile(output, self.PATHS.output / file)
