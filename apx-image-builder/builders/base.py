import argparse
import collections.abc
import hashlib
import json
import logging
import os
import select
import shutil
import stat
import subprocess
import tempfile
import textwrap
import urllib.parse
from pathlib import Path, PurePath
from typing import (IO, Any, Callable, Dict, List, NoReturn, Optional, Sequence, Tuple, Union, cast)


class StepFailedError(RuntimeError):
	pass


class BuildPaths(object):
	user_sources: Path
	build_root: Path
	build: Path
	output_root: Path
	output: Path

	def __init__(self, user_sources: Path, build_root: Path, output_root: Path, module: Optional[str]):
		self.user_sources = user_sources
		self.build_root = build_root
		self.build = build_root
		if not self.build_root.exists():
			self.build_root.mkdir(parents=True, exist_ok=True)
			with open(self.build_root / 'CACHEDIR.TAG', 'w') as fd:
				fd.write(
				    textwrap.dedent(
				        '''
						Signature: 8a477f597d28d172789f06886806bc55
						# This file is a cache directory tag created by apx-image-builder.
						# For information about cache directory tags, see:
						#	http://bford.info/cachedir/
						'''
				    ).lstrip()
				)
		if module is not None:
			self.build = self.build_root / module
			self.build.mkdir(parents=True, exist_ok=True)
		self.output_root = output_root
		self.output = output_root
		if module is not None:
			self.output = self.output_root / module
			self.output.mkdir(parents=True, exist_ok=True)

	def respecialize(self, module: Optional[str]) -> 'BuildPaths':
		return BuildPaths(self.user_sources, self.build_root, self.output_root, module)


class Stage(object):
	# This would work well as a TypedDict, sadly we have to run on python3.6.
	name: str  # The unqualified stage name.  e.g. 'build'
	builder: 'BaseBuilder'  # The builder this stage is associated with.
	_check: Optional[Callable[['Stage'], bool]]  # Check whether this stage can function.
	_run: Callable[['Stage'], None]  # The function to run this stage.
	requires: List[str
	               ]  # Stages that must run if this one runs. (Does not imply 'before' or 'after'.) e.g. 'kernel:build'
	after: List[
	    str
	]  # Specifies that this stage must run after the listed stages. (Does not imply 'requires'.)  e.g. 'kernel:build'
	before: List[
	    str
	]  # Specifies that this stage must run before the listed stages. (Does not imply 'requires'.)  e.g. 'kernel:build'
	include_in_all: bool
	logger: logging.Logger

	def __init__(
	    self,
	    builder: 'BaseBuilder',
	    name: str,
	    check: Optional[Callable[['Stage'], bool]],
	    run: Callable[['Stage'], None],
	    *,
	    requires: List[str] = [],
	    after: Optional[List[str]] = None,
	    before: List[str] = [],
	    include_in_all: bool = True
	):
		'''
		Initialize a stage definition.

		:param builder: The builder that owns this stage.
		:param name: The unqualified name of this stage.  e.g. 'build'
		:param check: The function to call to confirm this stage can be run.
		:param run: The function to call when this stage is run.
		:param requires: Stages that must run if this one runs. (Does not imply ordering.)
		:param after: This stage must run after the listed stages (if they run) (Defaults to the contents of `requires` if not supplied)
		:param before: This stage must run before the listed stages (if they run)
		:param include_in_all: If true, this stage will be included in "builder:ALL" and "ALL:ALL".
		'''
		self.builder = builder
		self.name = name
		self._check = check
		self._run = run
		self.requires = requires
		self.after = after if after is not None else list(requires)
		self.before = before
		self.include_in_all = include_in_all
		self.logger = logging.getLogger('{builder.NAME}:{name}'.format(builder=self.builder, name=self.name))

	def check(self) -> bool:
		if self._check is None:
			return True
		else:
			return self._check(self)

	def run(self) -> None:
		self._run(self)


class BaseBuilder(object):
	NAME: str = 'NotImplemented'  # Used on the command line and in configuration. i.e. 'kernel'
	LOGGER: logging.Logger
	PATHS: BuildPaths
	STAGES: Dict[str, Stage]
	COMMON_CONFIG: Dict[str, Any]
	BUILDER_CONFIG: Dict[str, Any]

	def __init__(self, config: Dict[str, Any], paths: BuildPaths, ARGS: argparse.Namespace):
		if self.NAME == 'NotImplemented':
			raise NotImplementedError('NAME is not set for ' + str(self.__class__.__name__))
		self.COMMON_CONFIG = config
		self.BUILDER_CONFIG = config.get('builders', {}).get(self.NAME, {})
		if self.BUILDER_CONFIG is None:  # Weird yaml artifact if you comment out all mapping values.
			self.BUILDER_CONFIG = {}
		self.PATHS = paths.respecialize(self.NAME)
		self.ARGS = ARGS
		self.instantiate_stages()

	def prepare_argparse(self, group: argparse._ArgumentGroup) -> None:
		'''
		Provide any command line arguments that this module wishes to recognize.

		Your arguments shoudl be in a --buildername-argumentname format.
		e.g. --kernel-menuconfig

		:param group: The argparse Argument Group to add arguments to.
		'''
		pass

	def instantiate_stages(self) -> None:
		'''
		You should include a 'build' stage that produces outputs.
		You should include a 'clean' stage that cleans the build.
		You should include a 'distclean' stage that destroys the entire workspace.

		You should call super() to start with default steps.

		:returns: A dict of unqualified stage name -> Stage definition.
		'''
		self.STAGES = {
		    'distclean': Stage(self, 'distclean', None, self.__distclean, include_in_all=False),
		    'clean': Stage(self, 'clean', None, self.__distclean, include_in_all=False),
		}

	def __distclean(self, STAGE: Stage) -> None:
		STAGE.logger.debug('Using the default distclean implementation.')
		STAGE.logger.info('Deleting build workspace.')
		shutil.rmtree(self.PATHS.build, ignore_errors=True)
		self.PATHS.build.mkdir(parents=True, exist_ok=True)
		STAGE.logger.info('Deleting outputs.')
		shutil.rmtree(self.PATHS.output, ignore_errors=True)
		self.PATHS.output.mkdir(parents=True, exist_ok=True)


def fail(logger: logging.Logger, message: str, source: Optional[Exception] = None) -> NoReturn:
	logger.error(message)
	if source is not None:
		logger.debug('From exception: ' + str(source))
	raise StepFailedError(message) from source


StrOrBytesPath = Union[str, bytes, Path]  # Hacked from python typeshed


def run(
        STAGE: Stage,
        cmdargs: Union[StrOrBytesPath, Sequence[StrOrBytesPath]],
        check: bool = True,
        CHECK_RAISE: bool = True,
        DETAIL_LOGLEVEL: int = logging.DEBUG,
        OUTPUT_LOGLEVEL: int = logging.DEBUG,
        ERROR_LOGLEVEL: int = logging.ERROR,
        **kwargs
) -> Tuple[int, bytes]:
	PATHS = STAGE.builder.PATHS
	LOGGER = STAGE.logger

	kwargs.setdefault('stdin', subprocess.DEVNULL)  # Change default to "no input accepted".
	kwargs.setdefault('stdout', subprocess.PIPE)  # Change default to "capture output".
	kwargs.setdefault('stderr', subprocess.STDOUT)  # Change default to "capture alongside stdout".

	if isinstance(kwargs['stdin'], (str, bytes)):
		intmp = tempfile.NamedTemporaryFile('w+b', prefix='stdin.')
		intmp.write(kwargs['stdin'] if isinstance(kwargs['stdin'], bytes) else kwargs['stdin'].encode('utf8'))
		intmp.flush()
		intmp.seek(0, os.SEEK_SET)
		kwargs['stdin'] = intmp
	if 'cwd' not in kwargs:
		kwargs['cwd'] = PATHS.build

	if isinstance(cmdargs, (str, bytes, PurePath)):
		LOGGER.log(DETAIL_LOGLEVEL, f'Running {cmdargs!r}...')
	elif isinstance(cmdargs, collections.abc.Sequence):
		cmdargs = list(cmdargs)
		LOGGER.log(
		    DETAIL_LOGLEVEL, f'Running {[(arg if isinstance(arg,(bytes,str)) else(str(arg))) for arg in cmdargs]!r}...'
		)
	assert kwargs.get(
	    'stderr', None
	) is not subprocess.PIPE, "run() helper cannot take stderr=subprocess.PIPE, consider subprocess.STDOUT or a file"
	assert kwargs.get('encoding', None) is None, 'run() helper cannot take nonbinary output'
	assert kwargs.get('errors', None) is None, 'run() helper cannot take nonbinary output'
	assert kwargs.get('univeral_newlines', False) is False, 'run() helper cannot take nonbinary output'

	tmpoutfn: Optional[str] = None
	tmpoutfile: Optional[IO[bytes]] = None
	ret: Optional[int] = None
	proc = subprocess.Popen(cast(Any, cmdargs), **kwargs)
	if kwargs.get('stdout', subprocess.PIPE) is subprocess.PIPE:
		(PATHS.output_root / 'logs').mkdir(exist_ok=True)
		tmpoutfd, tmpoutfn = tempfile.mkstemp('.txt', LOGGER.name + '.', PATHS.output_root / 'logs')
		tmpoutfile = os.fdopen(tmpoutfd, 'w+b')
		while len(select.select([proc.stdout], [], [], None)[0]):
			# No timeout, we'll only get "not ready" if we're truely done.

			# Typing is wrong about proc.stdout, assuming we've configured
			# it for str, for some reason.
			line = cast(IO[bytes], proc.stdout).readline()
			if not line:
				break  # Done.
			tmpoutfile.write(line)
			LOGGER.log(OUTPUT_LOGLEVEL, '| ' + line.rstrip(b'\r\n').decode('utf8', errors='replace'))
		while True:
			line = cast(IO[bytes], proc.stdout).readline()
			if not line:
				break
			tmpoutfile.write(line)
			LOGGER.log(OUTPUT_LOGLEVEL, '| ' + line.rstrip(b'\r\n').decode('utf8', errors='replace'))
		tmpoutfile.flush()
		tmpoutfile.seek(0, os.SEEK_SET)

	stdout = tmpoutfile.read() if tmpoutfile is not None else b''
	ret = proc.wait()

	if not check:
		LOGGER.log(DETAIL_LOGLEVEL, f'Command finished with return status {ret!s}.')
		LOGGER.log(DETAIL_LOGLEVEL, f'Output stored as {tmpoutfn}.')
	elif ret != 0:
		LOGGER.log(ERROR_LOGLEVEL, f'Command failed with exit status {ret!s} for {cmdargs!r}.')
		LOGGER.log(ERROR_LOGLEVEL, f'Output stored as {tmpoutfn}.')
		if CHECK_RAISE:
			raise subprocess.CalledProcessError(ret, proc.args, output=stdout, stderr='')
	else:
		LOGGER.log(DETAIL_LOGLEVEL, f'Command finished successfully.')
		LOGGER.log(DETAIL_LOGLEVEL, f'Output stored as {tmpoutfn}.')
	return (ret, stdout)


def hash_file(algo: str, file: IO[bytes], block_size=16386) -> str:
	h = hashlib.new(algo)
	while True:
		data = file.read(block_size)
		h.update(data)
		if not data:
			return h.hexdigest()


class JSONStateFile(object):
	_path: Path

	def __init__(self, path: Path):
		self._path = path
		self.load()

	def load(self):
		if self._path.exists():
			self.state = json.load(open(self._path, 'r'))
		else:
			self.state = {}

	def save(self):
		tmp_path = Path(str(self._path) + '~')
		with open(tmp_path, 'w') as fd:
			json.dump(self.state, fd)
		os.rename(tmp_path, self._path)

	def __enter__(self) -> Dict[str, Any]:
		# This doesn't reload, just saves on exiting the `with: block
		return self.state

	def __exit__(self, exc_type, exc_value, traceback):
		self.save()


def copyfile(src: Path, dst: Path, *, follow_symlinks: bool = True, copy_x_bit: bool = True) -> None:
	shutil.copyfile(src, dst, follow_symlinks=follow_symlinks)
	if copy_x_bit and (stat.S_IMODE(src.stat().st_mode) & 0o111):
		# The source file was executable.
		dstmode = stat.S_IMODE(dst.stat().st_mode)
		# Take the 'r' mask, and convert it to an 'x' mask, and add it to the modes.
		dstmode |= ((dstmode & 0o444) >> 2)
		dst.chmod(dstmode)


def import_source(
        STAGE: Stage,
        source_url: Union[str, Path],
        target: Union[str, Path],
        *,
        quiet: Optional[bool] = None,
        ignore_timestamps: bool = False,
        optional: bool = False,
) -> bool:
	PATHS = STAGE.builder.PATHS
	LOGGER = STAGE.logger
	comprehensible_source_url = source_url

	# If the target is relative, it's relative to the build directory.
	target = PATHS.build / Path(target)

	if isinstance(source_url, str):
		parsed_sourceurl = urllib.parse.urlparse(source_url)
		sourceid = hashlib.new('sha256', source_url.encode('utf8')).hexdigest()
		if parsed_sourceurl.scheme in ('http', 'https', 'ftp'):
			if quiet is None:
				quiet = False  # Default to verbose, for these.
			cachefile = PATHS.build / 'downloaded-source-{sourceid}.dat'.format(sourceid=sourceid)
			if not cachefile.exists():
				LOGGER.info(f'Downloading source file {comprehensible_source_url!s}')
				try:
					run(
					    STAGE,
					    ['wget', '-O', str(cachefile.resolve()) + '~', source_url],
					    stdout=None if STAGE.builder.ARGS.verbose else subprocess.PIPE,
					    stderr=None if STAGE.builder.ARGS.verbose else subprocess.STDOUT,
					    OUTPUT_LOGLEVEL=logging.NOTSET,
					)
				except Exception:
					try:
						cachefile.unlink()
					except:
						pass
					fail(LOGGER, f'Unable to download source file {comprehensible_source_url!s}')
				os.rename(str(cachefile.resolve()) + '~', cachefile.resolve())
			else:
				LOGGER.info(f'Already downloaded source file from {comprehensible_source_url!s}')
			source_url = cachefile
		elif parsed_sourceurl.scheme == 'builtin':
			if quiet is None:
				quiet = True  # Default to quiet for these.
			cachefile = PATHS.build / 'builtin-resource-{sourceid}.dat'.format(sourceid=sourceid)
			if not cachefile.exists():
				# Builtin resources don't change, so we won't check.
				try:
					import pkg_resources
					data = pkg_resources.resource_string(parsed_sourceurl.netloc or __name__, parsed_sourceurl.path)
					with open(cachefile, 'wb') as fd:
						fd.write(data)
				except ImportError:
					fail(
					    LOGGER,
					    'No supported package resource access module installed.  (install the `pkg_resources` python module)'
					)
			source_url = cachefile
		else:
			if quiet is None:
				quiet = False  # Default to verbose, for these.
			# If the source is relative, it's relative to the user sources dir.
			source_url = PATHS.user_sources / Path(source_url)
			try:
				comprehensible_source_url = source_url.relative_to(PATHS.user_sources)
			except ValueError:
				pass  # Guess it's not a user source.

	# If the source is relative, it's relative to the user sources dir.
	source_url = PATHS.user_sources / Path(source_url)

	if not source_url.exists():
		if not optional:
			fail(LOGGER, f'Unable to locate source file {comprehensible_source_url!s}')
		else:
			if not quiet:
				LOGGER.info(f'Importing optional source file {comprehensible_source_url!s} as missing.')
			target_exists = target.exists()
			target.unlink(missing_ok=True)
			return target_exists

	changed = False
	if not target.exists():
		# Well that's obvious then.
		changed = True
	if not changed and not ignore_timestamps:
		# Timestamp check.
		try:
			sts = source_url.stat()
			stt = target.stat()
			if stt.st_mtime < sts.st_mtime or stt.st_ctime < sts.st_ctime or stt.st_size != sts.st_size:
				changed = True
		except Exception:
			changed = True
	if not changed:
		# Hash check.
		# We really don't want to update source timestamps if we don't have
		# to, to avoid unnecessary `make` invocations.
		try:
			if hash_file('sha256', open(source_url, 'rb')) != hash_file('sha256', open(target, 'rb')):
				changed = True
		except Exception:
			changed = True

	if not changed:
		if not quiet:
			LOGGER.info(f'Skipping unchanged source file {comprehensible_source_url!s}')
		return False
	else:
		if not quiet:
			LOGGER.info(f'Importing source file {comprehensible_source_url!s}')
		copyfile(source_url, target, follow_symlinks=True)
		return True


class BypassableStage(Stage):
	extract_bypass: bool

	def __init__(
	    self,
	    builder: 'BaseBuilder',
	    name: str,
	    check: Optional[Callable[['Stage'], bool]],
	    run: Callable[['Stage'], None],
	    *,
	    requires: List[str] = [],
	    after: Optional[List[str]] = None,
	    before: List[str] = [],
	    include_in_all: bool = True,
	    extract_bypass: bool = True,
	):
		super().__init__(
		    builder, name, check, run, requires=requires, after=after, before=before, include_in_all=include_in_all
		)
		self.extract_bypass = extract_bypass

	def check(self) -> bool:
		bypass_file = 'bypass.{builder_name}.tbz2'.format(builder_name=self.builder.NAME)
		bypass_file = self.builder.PATHS.user_sources / bypass_file
		if bypass_file.exists():
			self.logger.debug(f'{self.builder.NAME!s}:{self.name!s} is bypassed.  Skipping requirements checks.')
			return True
		return super().check()

	def run(self) -> None:
		bypass_file = 'bypass.{builder_name}.tbz2'.format(builder_name=self.builder.NAME)
		bypass_file = self.builder.PATHS.user_sources / bypass_file
		if bypass_file.exists():
			self.logger.info(f'{self.builder.NAME!s}:{self.name!s} is bypassed.')
			if not self.extract_bypass:
				self.logger.debug("Extracting pre-generated output files is not this stage's responsibility.")
				return
			bypass_canary = self.builder.PATHS.output / '.bypassed'
			if import_source(self, bypass_file, self.builder.PATHS.build / '.bypass.tbz2',
			                 quiet=True) or not bypass_canary.exists():
				self.logger.debug('Extracting pre-generated output files.')
				shutil.rmtree(self.builder.PATHS.output, ignore_errors=True)
				self.builder.PATHS.output.mkdir()
				run(self, ['tar', '-xf', bypass_file, '-C', self.builder.PATHS.output])
				bypass_canary.touch()
			else:
				self.logger.debug('Pre-generated output files have already been extracted.')
			return
		super().run()


def untar(
        STAGE: Stage,
        source: Union[str, Path],
        target: Union[str, Path],
        *,
        reparent: bool = True,
) -> None:
	LOGGER = STAGE.logger

	# If source or target are relative, they're relative to the build dir.
	source = STAGE.builder.PATHS.build / Path(source)
	target = STAGE.builder.PATHS.build / Path(target)
	try:
		shutil.rmtree(target, ignore_errors=True)
		target.mkdir(parents=True)
	except Exception:
		fail(LOGGER, f'Unable to clean up {str(target)!r} before extracting archive.')

	try:
		run(STAGE, ['tar', '-xf', str(source.resolve()), '-C', str(target.resolve())])
	except subprocess.CalledProcessError:
		fail(LOGGER, f'Unable to extract source archive {str(source)!r}')

	if reparent:
		while True:
			contents = list(target.glob('*'))
			if len(contents) == 1 and contents[0].is_dir():
				LOGGER.debug(f'Found lone subdirectory {str(contents[0].name)!r}.  Reparenting.')
				try:
					shutil.rmtree(str(target) + '~', ignore_errors=True)
					os.rename(contents[0], Path(str(target) + '~'))
					target.rmdir()
					os.rename(Path(str(target) + '~'), target)
				except Exception as e:
					fail(LOGGER, 'Unable to reparent subdirectory.', e)
			else:
				break


class Patcher(object):
	cache_dir: Path
	sequence_number: int
	patchset: List[Path]

	def __init__(self, cache_dir: Path):
		self.cache_dir = cache_dir
		self.cache_dir.mkdir(exist_ok=True)
		self.sequence_number = 0
		self.patchset = []

	def import_patches(
	    self,
	    STAGE: Stage,
	    patchset: Sequence[Union[str, Path]],
	    *,
	    quiet: Optional[bool] = None,
	) -> bool:
		patchset = list(patchset)
		prefix_fmt = '{serial:04d}_'
		changed = False
		for patch in patchset:
			target = self.cache_dir / (prefix_fmt.format(serial=self.sequence_number) + str(Path(patch).name))
			self.sequence_number += 1
			if import_source(STAGE, patch, target, quiet=quiet):
				changed = True
			self.patchset.append(target.resolve())
		for patch in self.cache_dir.glob('*'):
			if patch.resolve() not in self.patchset:
				patch.unlink()
				changed = True
		return changed

	def apply(
	    self,
	    STAGE: Stage,
	    target_dir: Union[str, Path],
	    *,
	    LOGLEVEL: int = logging.INFO,
	) -> None:
		target_dir = STAGE.builder.PATHS.build / target_dir
		for i, patch in enumerate(self.patchset):
			STAGE.logger.log(LOGLEVEL, f'Applying patch ({i+1}/{len(self.patchset)}) {patch.name!s}')
			exit, _output = run(STAGE, ['patch', '-tNp1', '-d', target_dir, '-i', patch.resolve()], CHECK_RAISE=False)
			if exit == 2:
				fail(STAGE.logger, '`patch` failed to execute correctly.')
			elif exit == 1:
				fail(STAGE.logger, f'Patch {patch.name!s} did not apply correctly.')
