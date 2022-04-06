import argparse
import logging
import os
import shutil
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, Iterable, List, NamedTuple, NoReturn, Set, Tuple, cast

import yaml

from . import builders
from .builders.base import BaseBuilder, BuildPaths, Stage, StepFailedError

def main() -> None:
	logging.basicConfig(format='%(levelname).1s: %(name)s: %(message)s', level=logging.INFO)
	LOGGER = logging.getLogger()
	LOGGER.name = 'apx-image-builder'

	# We're going to parse our arguments twice.  Once to get a config, then once
	# after we know (based on the config) the configuration and availability of our
	# builders.
	parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter, allow_abbrev=False, add_help=False)
	parser.add_argument(
		'-c',
		'--config',
		action='store',
		type=Path,
		default=Path('config.yaml'),
		help='The configuration file to load (default ./config.yaml)'
	)
	parser.add_argument(
		'--write-example-config',
		action='store_true',
		help='Write the example config to the path specified by --config. (must not exist)'
	)
	parser_logging = parser.add_mutually_exclusive_group()
	parser_logging.add_argument('-v', '--verbose', action='store_true', help='Set loglevel to DEBUG')
	parser_logging.add_argument('-q', '--quiet', action='store_true', help='Set loglevel to WARNING')
	ARGS, _ = parser.parse_known_args()
	if not cast(Path, ARGS.config).exists():

		def write_example_config(f: Path) -> NoReturn:
			if f.exists():
				print('Unable to write example config: File exists: ' + str(f), file=sys.stderr)
				raise SystemExit(1)
			try:
				import pkg_resources
				exampleconf = pkg_resources.resource_string(__name__, 'config.example.yaml')
			except ImportError as e:
				print(
					'No supported package resource access module installed.  (install the `pkg_resources` python module)',
					file=sys.stderr
				)
				raise SystemExit(1)
			with open(f, 'wb') as fd:
				fd.write(exampleconf)
			print('Example config written to ' + str(f))
			raise SystemExit(0)

		if ARGS.write_example_config:
			write_example_config(ARGS.config)

		print('Unable to open config file.', file=sys.stderr)
		parser.print_help()
		raise SystemExit(1)

	# Parsed.  We have a config path, and verbosity level.
	if ARGS.verbose:
		logging.getLogger().setLevel(logging.DEBUG)
	if ARGS.quiet:
		logging.getLogger().setLevel(logging.WARNING)

	LOGGER.debug('Running from ' + repr(str(Path(__file__).parent)))

	CONFIG: Dict[str, Any] = {
		'working_directory': './',
		'working_directory_config_relative': True,
		'sources_directory': './sources',
		'build_directory': './build',
		'output_directory': './output',
		'builders': {},
	}
	LOGGER.debug('Loading configuration from {config}'.format(config=ARGS.config))
	try:
		CONFIG.update(yaml.safe_load(open(ARGS.config, 'r')))
	except Exception as e:
		print('Unable to load configuration file: ' + str(e))
		raise SystemExit(1)
	LOGGER.info('Loaded configuration from {config}'.format(config=ARGS.config))

	LOGGER.debug('Resolving base configuration paths...')
	CONFIG['working_directory_config_relative'] = bool(CONFIG['working_directory_config_relative'])
	LOGGER.debug(f'working_directory_config_relative: {CONFIG["working_directory_config_relative"] !r}')
	if CONFIG['working_directory_config_relative']:
		CONFIG['working_directory'] = Path(ARGS.config).resolve().parent / CONFIG['working_directory']
	else:
		CONFIG['working_directory'] = Path(CONFIG['working_directory']).resolve()
	LOGGER.debug(f'working_directory: {str(CONFIG["working_directory"]) !r}')
	CONFIG['sources_directory'] = CONFIG['working_directory'] / CONFIG['sources_directory']
	CONFIG['build_directory'] = CONFIG['working_directory'] / CONFIG['build_directory']
	CONFIG['fetch_cache_directory'] = CONFIG['working_directory'] / CONFIG['fetch_cache_directory']
	CONFIG['output_directory'] = CONFIG['working_directory'] / CONFIG['output_directory']
	LOGGER.debug(f'sources_directory: {str(CONFIG["sources_directory"]) !r}')
	LOGGER.debug(f'build_directory: {str(CONFIG["build_directory"]) !r}')
	LOGGER.debug(f'fetch_cache_directory: {str(CONFIG["fetch_cache_directory"]) !r}')
	LOGGER.debug(f'output_directory: {str(CONFIG["output_directory"]) !r}')

	LOGGER.debug('Changing to working directory.')
	try:
		os.chdir(CONFIG['working_directory'])
	except Exception as e:
		LOGGER.error('Failed to change to working directory {wd}'.format(wd=repr(CONFIG['working_directory'])))
		raise SystemExit(1)

	LOGGER.debug('Initializing builders.')
	BUILD_PATHS = BuildPaths(
		CONFIG['sources_directory'],
		CONFIG['build_directory'],
		CONFIG['output_directory'],
		CONFIG['fetch_cache_directory'],
		None,
	)

	# Now we can instantiate stages and prepare our actual argument parser.
	# This will replace the output of the previous so it must contain all of the previous's options.
	BUILDERS: Dict[str, BaseBuilder] = {
		builder.NAME: builder(CONFIG, BUILD_PATHS, ARGS)
		for builder in builders.all_builders if builder.NAME not in CONFIG.get('disabled_builders', [])
	}
	STAGES: Dict[str, Dict[str, Stage]] = {}
	valid_stages: Set[str] = set(['ALL:ALL'])

	for builder in BUILDERS.values():
		valid_stages.add('{builder.NAME}:ALL'.format(builder=builder))
		for stage in builder.STAGES.values():
			STAGES.setdefault(builder.NAME, {})[stage.name] = stage
			valid_stages.add('{builder.NAME}:{stage.name}'.format(builder=builder, stage=stage))
			valid_stages.add('ALL:{stage.name}'.format(stage=stage))


	def generate_stage_helptext(stagedata: Dict[str, Dict[str, Stage]]) -> str:
		result: str = ''
		for builder_name, builder_stages in stagedata.items():
			stages = []
			for stage_name, stage in builder_stages.items():
				if stage_name not in ('distclean', 'clean', 'bypass'):
					stages.append(stage_name if stage.include_in_all else f'({stage_name})')
			if not stages:
				stages = ['[unavailable in this configuration]']
			result += '\n'.join([builder_name + ':'] +
								textwrap.wrap(', '.join(stages), initial_indent='  ', subsequent_indent='  ')) + '\n'
		return result


	parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter)
	parser.add_argument(
		'-c',
		'--config',
		action='store',
		type=Path,
		default=Path('config.yaml'),
		help='The configuration file to load (default ./config.yaml)'
	)

	parser.add_argument(
		'stages',
		action='store',
		nargs='+',
		default=['ALL:ALL'],
		# It turns out that this is terribly spammy in the helptext.
		# We'll check it manually.
		#choices=sorted(valid_stages),
		help='''
	Choose which build stages to run.
	A stage is specified by a builder name, a colon, then a stage name.
	For example: 'kernel:build'.

	Either the builder or stage name (or both) may be replaced with 'ALL', to
	specify all matching stages.  The default is ALL:ALL.  Some stages are not
	included in xxx:ALL.  Those stages are listed in parentheses.

	All builders offer the stages (distclean) and (clean).
	All builders that are not bypassed offer the stage (bypass), which will
	generate and install a bypass file for that builder.
	For more information on stages, see the builder-specific help below.

	Available builders and their stages are:
	{stages}
	'''.format(stages=generate_stage_helptext(STAGES)).strip()
	)
	parser_logging = parser.add_mutually_exclusive_group()
	parser_logging.add_argument('-v', '--verbose', action='store_true', help='Set loglevel to DEBUG')
	parser_logging.add_argument('-q', '--quiet', action='store_true', help='Set loglevel to WARNING')

	for builder in BUILDERS.values():
		builder.prepare_argparse(parser.add_argument_group('"{builder.NAME}" builder'.format(builder=builder)))

	ARGS = parser.parse_args()

	invalid_stages = [stage for stage in ARGS.stages if stage not in valid_stages]
	if invalid_stages:
		print('The following stages are unknown (see help): ' + ', '.join(invalid_stages), file=sys.stderr)
		parser.print_usage(sys.stderr)
		raise SystemExit(1)


	# Identify the stages that must be run.
	def sequence_stages() -> List[Tuple[str, str]]:
		# The function is just to create a temporary scope.

		# Step 1: Generate our own table of stages and their dependency information.
		class StageInfo(NamedTuple):
			stage: Stage
			requires: Set[Tuple[str, str]]
			before: Set[Tuple[str, str]]
			after: Set[Tuple[str, str]]

		stageset: Dict[Tuple[str, str], StageInfo] = {}
		for builder_name, builder_stages in STAGES.items():
			for stage_name, stage in builder_stages.items():
				stageset[(builder_name, stage_name)] = StageInfo(
					stage,
					set(cast(Tuple[str, str], tuple(x.split(':', 1))) for x in stage.requires),
					set(cast(Tuple[str, str], tuple(x.split(':', 1))) for x in stage.before),
					set(cast(Tuple[str, str], tuple(x.split(':', 1))) for x in stage.after),
				)
		# Resolve any 'ALL:x' or 'x:ALL'
		def resolve_alls(all_keys: List[Tuple[str, str]], listed_keys: Iterable[Tuple[str, str]]) -> Set[Tuple[str, str]]:
			out: Set[Tuple[str, str]] = set()
			for key in listed_keys:
				if 'ALL' not in key:
					# Straightforward.
					out.add(key)
				else:
					# We need to filtergroup.
					for iterkey in all_keys:
						if key[1] == 'ALL' and not STAGES[iterkey[0]][iterkey[1]].include_in_all:
							continue
						if key[0] in ('ALL', iterkey[0]) and key[1] in ('ALL', iterkey[1]):
							out.add(iterkey)
			return out

		all_keys = list(stageset.keys())
		stageset = {
			stage: StageInfo(
				info.stage,
				resolve_alls(all_keys, info.requires),
				resolve_alls(all_keys, info.before),
				resolve_alls(all_keys, info.after),
			)
			for stage, info in stageset.items()
		}

		# We now have a stageset with all 'ALL's, etc resolved.
		# Next up, resolve all 'before's into 'after's, so we only need to check one way.
		for key, info in stageset.items():
			for before in info.before:
				stageset[before].after.add(key)
			info.before.clear()

		## Now to resolve all 'require's, and identify the set of steps to actually be run.

		# Get a list of required steps from the user.
		requested_stages = list(
			resolve_alls(all_keys, (cast(Tuple[str, str], tuple(stage.split(':', 1))) for stage in ARGS.stages))
		)
		# Update the list with the requirements of those steps, recursively.
		required_stages_set: Set[Tuple[str, str]] = set()
		while requested_stages:
			stage = requested_stages.pop(0)
			required_stages_set.add(stage)
			for required_stage in stageset[stage].requires:
				if required_stage not in stageset:
					LOGGER.error(
						f'Stage {":".join(required_stage)}, required by {":".join(stage)}, is unavailable (disabled?).'
					)
					raise SystemExit(1)
				if required_stage not in required_stages_set:
					requested_stages.append(required_stage)

		# Re-sequence to the natural ordering.
		required_stages = [stage for stage in stageset.keys() if stage in required_stages_set]
		sequenced_stages: List[Tuple[str, str]] = []

		# Now reorder based on dependencies as needed.
		while required_stages:
			stage = required_stages[0]
			if stage in sequenced_stages:
				# Got this one already.
				required_stages.pop(0)
				continue
			stages_inserted = False
			for after in stageset[stage].after:
				if after in required_stages and after not in sequenced_stages:
					# We need to do the 'after'd one first.
					#
					# This will only pull strict ordered dependencies, so it's
					# technically already in "minimum disruption" form, generally.
					# We trust the builders to be provided to us already roughly
					# sequenced.
					required_stages.insert(0, after)
					stages_inserted = True
			if stages_inserted:
				continue  # Not time to consume our focused stage yet.
			else:
				sequenced_stages.append(stage)
				required_stages.pop(0)
		return sequenced_stages


	sequenced_stages = sequence_stages()

	LOGGER.debug(f'Stages to be run: {", ".join(":".join(stage) for stage in sequenced_stages)}')
	shutil.rmtree(BUILD_PATHS.output_root / 'logs', ignore_errors=True)  # Fresh init the log output directory.


	def check_conditions() -> List[str]:
		# The function is just to create a temporary scope.

		LOGGER.info('Checking configuration...')
		conditions_failed_for: List[str] = []
		for builder_name, stage_name in sequenced_stages:
			try:
				if not STAGES[builder_name][stage_name].check():
					LOGGER.error('Conditions not met for {builder}:{stage}'.format(builder=builder_name, stage=stage_name))
					conditions_failed_for.append(builder_name + ':' + stage_name)
			except StepFailedError:
				conditions_failed_for.append(builder_name + ':' + stage_name)
				LOGGER.error('Check failed for {builder}:{stage}'.format(builder=builder_name, stage=stage_name))
		return conditions_failed_for


	conditions_failed_for = check_conditions()
	if conditions_failed_for:
		LOGGER.error('Conditions were not met for the following stages: ' + ', '.join(conditions_failed_for))
		LOGGER.error('Build failed.  See above for further details.')
		raise SystemExit(1)

	for i, (builder_name, stage_name) in enumerate(sequenced_stages):
		LOGGER.info(f'Running {builder_name}:{stage_name} ({i+1}/{len(sequenced_stages)})')
		try:
			STAGES[builder_name][stage_name].run()
		except StepFailedError as e:
			LOGGER.error(f'{builder_name}:{stage_name} failed: {e!s}')
			raise SystemExit(1)

	LOGGER.info('Build complete.')
