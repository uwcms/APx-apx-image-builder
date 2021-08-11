from . import base
from .fsbl import FSBLBuilder
from .pmu import PMUBuilder
from .atf import ATFBuilder
from .dtb import DTBBuilder
from .uboot import UBootBuilder
from .kernel import KernelBuilder
from .rootfs import RootfsBuilder
from .qspi import QSPIBuilder
from .jtag import JTAGBuilder

# Keep this list in dependency order as much as possible, to minimize the amount
# of intermixing of steps between builders that results from dependency
# resolution.
all_builders = [
    FSBLBuilder,
    PMUBuilder,
    ATFBuilder,
    DTBBuilder,
    UBootBuilder,
    KernelBuilder,
	RootfsBuilder,
	QSPIBuilder,
	JTAGBuilder,
]
