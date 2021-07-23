import argparse
import collections.abc
import hashlib
import json
import logging
import os
import select
import shutil
import subprocess
import tempfile
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
	_check: Optional[Callable[['Stage', BuildPaths, logging.Logger], bool]]  # Check whether this stage can function.
	_run: Callable[['Stage', BuildPaths, logging.Logger], None]  # The function to run this stage.
	requires: List[str
	               ]  # Stages that must run if this one runs. (Does not imply 'before' or 'after'.) e.g. 'kernel:build'
	after: List[
	    str
	]  # Specifies that this stage must run after the listed stages. (Does not imply 'requires'.)  e.g. 'kernel:build'
	before: List[
	    str
	]  # Specifies that this stage must run before the listed stages. (Does not imply 'requires'.)  e.g. 'kernel:build'
	include_in_all: bool

	def __init__(
	    self,
	    builder: 'BaseBuilder',
	    name: str,
	    check: Optional[Callable[['Stage', BuildPaths, logging.Logger], bool]],
	    run: Callable[['Stage', BuildPaths, logging.Logger], None],
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

	def check(self, paths: BuildPaths) -> bool:
		if self._check is None:
			return True
		else:
			return self._check(
			    self,
			    paths.respecialize(self.builder.NAME),
			    logging.getLogger('{builder.NAME}:{name}'.format(builder=self.builder, name=self.name)),
			)

	def run(self, paths: BuildPaths) -> None:
		self._run(
		    self,
		    paths.respecialize(self.builder.NAME),
		    logging.getLogger('{builder.NAME}:{name}'.format(builder=self.builder, name=self.name)),
		)


class BaseBuilder(object):
	NAME: str = 'NotImplemented'  # Used on the command line and in configuration. i.e. 'kernel'
	LOGGER: logging.Logger
	PATHS: BuildPaths
	STAGES: Dict[str, Stage]
	COMMON_CONFIG: Dict[str, Any]
	BUILDER_CONFIG: Dict[str, Any]

	def __init__(self):
		if self.NAME == 'NotImplemented':
			raise NotImplementedError('NAME is not set for ' + str(self.__class__.__name__))
		self.COMMON_CONFIG = {}
		self.BUILDER_CONFIG = {}

	def update_config(self, config: Dict[str, Any], ARGS: argparse.Namespace) -> None:
		self.COMMON_CONFIG = config
		self.BUILDER_CONFIG = config.get('builders', {}).get(self.NAME, {})
		self.ARGS = ARGS

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
		    'clean': Stage(self, 'distclean', None, self.__distclean, include_in_all=False),
		}

	def __distclean(self, STAGE: Stage, PATHS: BuildPaths, LOGGER: logging.Logger) -> None:
		LOGGER.debug('Using the default distclean implementation.')
		LOGGER.info('Deleting build workspace.')
		shutil.rmtree(PATHS.build, ignore_errors=True)
		PATHS.build.mkdir(parents=True, exist_ok=True)
		LOGGER.info('Deleting outputs.')
		shutil.rmtree(PATHS.output, ignore_errors=True)
		PATHS.output.mkdir(parents=True, exist_ok=True)


def FAIL(logger: logging.Logger, message: str, source: Optional[Exception] = None) -> NoReturn:
	logger.error(message)
	if source is not None:
		logger.debug('From exception: ' + str(source))
	raise StepFailedError(message) from source


StrOrBytesPath = Union[str, bytes, Path]  # Hacked from python typeshed


def RUN(
    PATHS: BuildPaths,
    LOGGER: logging.Logger,
    cmdargs: Union[StrOrBytesPath, Sequence[StrOrBytesPath]],
    check: bool = True,
    CHECK_RAISE: bool = True,
    DETAIL_LOGLEVEL: int = logging.DEBUG,
    OUTPUT_LOGLEVEL: int = logging.DEBUG,
    ERROR_LOGLEVEL: int = logging.ERROR,
    **kwargs
) -> Tuple[int, bytes]:
	kwargs.setdefault('stdin', subprocess.DEVNULL)  # Change default to "no input accepted".
	kwargs.setdefault('stdout', subprocess.PIPE)  # Change default to "capture output".
	kwargs.setdefault('stderr', subprocess.STDOUT)  # Change default to "capture alongside stdout".

	if isinstance(kwargs['stdin'], (str, bytes)):
		intmp = tempfile.NamedTemporaryFile('w+b', prefix='stdin.')
		intmp.write(kwargs['stdin'] if isinstance(kwargs['stdin'], bytes) else kwargs['stdin'].encode('utf8'))
		intmp.flush()
		intmp.seek(0, os.SEEK_SET)
		kwargs['stdin'] = intmp

	if isinstance(cmdargs, (str, bytes, PurePath)):
		LOGGER.log(DETAIL_LOGLEVEL, f'Running {cmdargs!r}...')
	elif isinstance(cmdargs, collections.abc.Sequence):
		cmdargs = list(cmdargs)
		LOGGER.log(
		    DETAIL_LOGLEVEL, f'Running {[(arg if isinstance(arg,(bytes,str)) else(str(arg))) for arg in cmdargs]!r}...'
		)
	assert kwargs.get(
	    'stderr', None
	) is not subprocess.PIPE, "RUN() helper cannot take stderr=subprocess.PIPE, consider subprocess.STDOUT or a file"
	assert kwargs.get('encoding', None) is None, 'RUN() helper cannot take nonbinary output'
	assert kwargs.get('errors', None) is None, 'RUN() helper cannot take nonbinary output'
	assert kwargs.get('univeral_newlines', False) is False, 'RUN() helper cannot take nonbinary output'

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


def HASH_FILE(algo: str, file: IO[bytes], block_size=16386):
	h = hashlib.new(algo)
	while True:
		data = file.read(block_size)
		h.update(data)
		if not data:
			return h


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
