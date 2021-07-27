from . import base
from .fsbl import FSBLBuilder
from .pmu import PMUBuilder
from .atf import ATFBuilder
from .dtb import DTBBuilder
from .kernel import KernelBuilder

# Keep this list in dependency order as much as possible, to minimize the amount
# of intermixing of steps between builders that results from dependency
# resolution.
all_builders = [
    FSBLBuilder,
    PMUBuilder,
    ATFBuilder,
    DTBBuilder,
    KernelBuilder,
]
