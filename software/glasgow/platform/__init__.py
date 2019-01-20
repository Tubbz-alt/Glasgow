from migen.build.generic_platform import *
from migen.build.lattice import LatticePlatform

from .programmer import GlasgowProgrammer


__all__ = ['Platform']


_io = [
    ("clk_fx", 0, Pins("L5"), IOStandard("LVCMOS33")),
    ("clk_if", 0, Pins("K6"), IOStandard("LVCMOS33")),

    ("fx2", 0,
        Subsignal("sloe", Pins("L3")),
        Subsignal("slrd", Pins("J5")),
        Subsignal("slwr", Pins("J4")),
        Subsignal("pktend", Pins("L1")),
        Subsignal("fifoadr", Pins("K3 L2")),
        Subsignal("flag", Pins("L7 K5 L4 J3")),
        Subsignal("fd", Pins("H7 J7 J9 K10 L10 K9 L8 K7")),
        IOStandard("LVCMOS33")
    ),

    ("user_led", 0, Pins("G9 G8 E9 D9 E8"), IOStandard("LVCMOS33")),

    ("io", 0, Pins("A1 A2 B3 A3 B6 A4 B7 A5"), IOStandard("LVCMOS33")),
    ("io", 1, Pins("B11 C11 D10 D11 E10 E11 F11 F10"), IOStandard("LVCMOS33")),
    
    ("io_oe", 0, Pins("C7 C8 D7 A7 B8 A8 B9 A9"), IOStandard("LVCMOS33")),
    ("io_oe", 1, Pins("F9 G11 G10 H11 H10 J11 J10 K11"), IOStandard("LVCMOS33")),

    ("i2c", 0,
        Subsignal("scl", Pins("H9")),
        Subsignal("sda", Pins("J8")),
        IOStandard("LVCMOS33")
    ),

    # open-drain
    ("sync", 0, Pins("A11"), IOStandard("LVCMOS33")),

    #tri-state (Shared with IO_A 4/6 )
    ("tri", 0, Pins("A6 B5"), IOStandard("LVCMOS33")),
]

_connectors = [
]


class GlasgowPlatform(LatticePlatform):
    default_clk_name = "clk_if"
    default_clk_period = 1e9 / 30e6

    def __init__(self):
        LatticePlatform.__init__(self, "ice40-hx8k-bg121", _io, _connectors,
                                 toolchain="icestorm")

    def create_programmer(self):
        return GlasgowProgrammer()
