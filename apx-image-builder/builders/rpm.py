import argparse
import itertools
import re
import shutil
import subprocess
import textwrap
import time
from typing import IO, Dict, List, Optional, Tuple

from . import base


class RPMBuilder(base.BaseBuilder):
	NAME: str = 'rpm'

	@classmethod
	def prepare_argparse(cls, group: argparse._ArgumentGroup) -> None:
		group.description = textwrap.dedent(
		    '''
			Build the BOOT.BIN file and firmware RPM.
			Stages available:
			- build: Build the BOOT.BIN file and firmware RPM.
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
		if not shutil.which('bootgen'):
			STAGE.logger.error(f'Unable to locate `bootgen`.  Did you source the Vivado environment files?')
			check_ok = False
		if not shutil.which('mkimage'):
			STAGE.logger.error(
			    f'Unable to locate `mkimage`.  Is uboot-tools (CentOS) or u-boot-tools (ubuntu) installed?'
			)
			check_ok = False
		if not shutil.which('rpmbuild'):
			STAGE.logger.error(f'Unable to locate `rpmbuildmkimage`.')
			check_ok = False
		if not shutil.which('unzip'):
			STAGE.logger.error(f'Unable to locate `unzip`.')
			check_ok = False
		if not self.BUILDER_CONFIG.get('image_name', ''):
			STAGE.logger.warning('You did not supply a value for the `image_name` option to the `rpm` builder.')
			STAGE.logger.warning('The firmware RPM will not be generated!')
		return check_ok

	def build(self, STAGE: base.Stage) -> None:
		dtb_address = self.COMMON_CONFIG.get('dtb_address', 0x00100000)
		image_name = self.BUILDER_CONFIG.get('image_name', '')
		for output in itertools.chain.from_iterable(self.PATHS.output.glob(x) for x in ('BOOT.BIN', '*.rpm')):
			STAGE.logger.debug('Removing pre-existing output ' + str(output))
			try:
				output.unlink()
			except Exception:
				pass

		if self.COMMON_CONFIG.get('zynq_series', '') == 'zynqmp':
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
		else:
			bif = textwrap.dedent(
			    '''
			the_ROM_image:
			{{
				[bootloader] fsbl.elf
				system.bit
				u-boot.elf
				[load=0x{dtb_address:08x}] system.dtb
			}}
			'''
			).format(dtb_address=dtb_address)
		with open(self.PATHS.build / 'boot.bif', 'w') as fd:
			fd.write(bif)

		base.import_source(STAGE, 'rpm.boot.scr', 'boot.scr', optional=True)
		STAGE.logger.info('Importing prior build products...')
		built_sources = [
		    'fsbl:fsbl.elf',
		    'dtb:system.dtb',
		    'u-boot:u-boot.elf',
		]
		if self.COMMON_CONFIG.get('zynq_series', '') == 'zynqmp':
			built_sources.extend(['pmu:pmufw.elf', 'atf:bl31.elf'])
		for builder, source in (x.split(':', 1) for x in built_sources):
			base.import_source(STAGE, self.PATHS.respecialize(builder).output / source, source, quiet=True)

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

		# Provide BOOT.BIN as an output.
		if not (self.PATHS.build / 'BOOT.BIN').exists():
			base.fail(STAGE.logger, 'BOOT.BIN not found after build.')
		base.copyfile(self.PATHS.build / 'BOOT.BIN', self.PATHS.output / 'BOOT.BIN')

		if not image_name:
			STAGE.logger.warning('You did not supply a value for the `image_name` option to the `rpm` builder.')
			STAGE.logger.warning('Not generating the firmware RPM!')
		else:
			base.import_source(STAGE, 'builtin:///rpm_data/apx-firmware.spec', self.PATHS.build / 'apx-firmware.spec')
			bootscr = self.PATHS.build / 'boot.scr'
			if not bootscr.exists():
				STAGE.logger.info('Generating boot.scr automatically.')
				with open(bootscr, 'w') as fd:
					fd.write(
						textwrap.dedent(
							'''
							echo Loading kernel bootargs...
							load $devtype ${{devnum}}:${{distro_bootpart}} $kernel_addr_r bootargs.scr
							source $kernel_addr_r
							echo Loading kernel...
							load $devtype ${{devnum}}:${{distro_bootpart}} $kernel_addr_r vmlinuz
							echo Booting...
							bootz $kernel_addr_r - 0x{dtb_address:08x}
							'''
						).format(dtb_address=dtb_address)
					)
			STAGE.logger.info('Generating boot.scr FIT image')
			try:
				base.run(STAGE, ['mkimage', '-c', 'none', '-A', 'arm', '-T', 'script', '-d', 'boot.scr', 'boot.scr.ub'])
			except subprocess.CalledProcessError:
				base.fail(STAGE.logger, '`mkimage` returned with an error')

			STAGE.logger.info('Building firmware RPM')
			rpmbuilddir = self.PATHS.build / 'rpmbuild'
			shutil.rmtree(rpmbuilddir, ignore_errors=True)
			rpmbuilddir.mkdir()
			STAGE.logger.info('Running rpmbuild...')
			try:
				rpmcmd = [
				    'rpmbuild',
				    '--define=_topdir ' + str(rpmbuilddir),
				    '--define=_builddir .',
				    '--define=rpm_version ' + self.BUILDER_CONFIG.get('rpm_version', '1.0.0'),
				    '--define=rpm_release ' + str(int(time.time())),
				    '--define=imagename ' + image_name,
				    '--target',
				    'aarch64' if self.COMMON_CONFIG.get('zynq_series', '') == 'zynqmp' else 'armv7hl',
				    '-bb',
				    'apx-firmware.spec',
				]
				base.run(STAGE, rpmcmd)
			except subprocess.CalledProcessError:
				base.fail(STAGE.logger, 'rpmbuild returned with an error')
			STAGE.logger.info('Built firmware RPM.')

			# Provide our rpms as an output. (for standard installation)
			for file in self.PATHS.build.glob('rpmbuild/RPMS/*/*.rpm'):
				base.copyfile(file, self.PATHS.output / file.name)
