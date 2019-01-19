import re
import sys
import struct
import logging
import argparse

from .. import *
from ..jtag import JTAGApplet
from ...database.jedec import *
from ..jtag_svf import JTAGSVFInterface
from ...protocol.jtag_svf import _hex_to_bitarray, bitarray


class ProgramECP5Error(GlasgowAppletError):
    pass


BIT_WIP  = 0b00000001
BIT_WEL  = 0b00000010
MSK_PROT = 0b00111100
BIT_CP   = 0b01000000
BIT_ERR  = 0b10000000

def _hex_to_bitarray(input_nibbles):
    byte_len = (len(input_nibbles) + 1) // 2
    input_bytes = bytes.fromhex(input_nibbles.rjust(byte_len * 2, "0"))
    bits = bitarray(endian="little")
    bits.frombytes(input_bytes)
    bits.reverse()
    bits.bytereverse()
    return bits

def _bitarray(input_nibbles):
    byte_len = (len(input_nibbles) + 1) // 2
    input_bytes = bytes.fromhex(input_nibbles.rjust(byte_len * 2, "0"))
    bits = bitarray(endian="little")
    bits.frombytes(input_bytes)

    bits.bytereverse()
    #bits.reverse()
    return bits


    
class ECP5ProgramInterface:
    def __init__(self, interface, logger,args):
        self.lower       = interface
        self.args        = args
        self._logger     = logger
        self._level      = logging.DEBUG if self._logger.name == __name__ else logging.TRACE
       # self._svf        = JTAGSVFInterface(interface, logger,args.frequency * 1000)
        

    def _log(self, message, *args):
        self._logger.log(self._level, "ECP5 PROGRAM: " + message, *args)

    async def _command(self, cmd, arg=[], dummy=0, ret=0, hold_ss=False):
        arg = bytes(arg)

        self._log("cmd=%02X arg=<%s> dummy=%d ret=%d", cmd, arg.hex(), dummy, ret)

        xmit = ''.join('{:02x}'.format(x) for x in [cmd, *arg, *[0 for _ in range(dummy)]])
        value = await self._sdr_array(_bitarray(xmit))

        # Clock in return value
        xmit = ''.join('{:02x}'.format(0) for _ in range(ret))
        value = await self._sdr_array(_bitarray(xmit))
        
        self._log("result=<%s>", value.hex())
        value.reverse()
        value.bytereverse()
        
        return value.tobytes()
        
    async def _sir(self, tdi):
        await self.lower.enter_shift_ir()
        await self.lower.shift_tdi(_hex_to_bitarray(tdi))
        await self.lower.enter_pause_ir()

    async def _sdr(self, tdi):
        await self.lower.enter_shift_dr()
        tdo_bits = await self.lower.shift_tdio(_hex_to_bitarray(tdi))
        await self.lower.enter_pause_dr()
        return tdo_bits

    async def _sdr_array(self, tdi):
        await self.lower.enter_shift_dr()
        tdo_bits = await self.lower.shift_tdio(tdi)
        await self.lower.enter_pause_dr()
        return tdo_bits

    async def _run_test(self, millis):
        await self.lower.run_test_idle(int(millis * self.args.frequency))

    async def wakeup(self):
        self._log("wakeup")
        await self._command(0xAB, dummy=4)

    async def deep_sleep(self):
        self._log("deep sleep")
        await self._command(0xB9)

    async def read_device_id(self):
        self._log("read device ID")
        device_id, = await self._command(0xAB, dummy=3, ret=1)
        return (device_id,)
    
    async def read_manufacturer_device_id(self):
        self._log("read manufacturer/8-bit device ID")
        manufacturer_id, device_id = await self._command(0x90, dummy=3, ret=2)
        return (manufacturer_id, device_id)

    async def read_manufacturer_long_device_id(self):
        self._log("read manufacturer/16-bit device ID")
        manufacturer_id, device_id = struct.unpack(">BH",
            await self._command(0x9F, ret=3))
        return (manufacturer_id, device_id)

    def _format_addr(self, addr):
        return bytes([(addr >> 16) & 0xff, (addr >> 8) & 0xff, addr & 0xff])

    async def _read_command(self, address, length, chunk_size, cmd, dummy=0,
                            callback=lambda done, total, status: None):
        if chunk_size is None:
            chunk_size = 0xff # FIXME: raise once #44 is fixed

        data = bytearray()
        while length > len(data):
            callback(len(data), length, "reading address {:#08x}".format(address))
            chunk    = await self._command(cmd, arg=self._format_addr(address),
                                           dummy=dummy, ret=min(chunk_size, length - len(data)))
            data    += chunk
            address += len(chunk)

        return data

    async def read(self, address, length, chunk_size=None,
                   callback=lambda done, total, status: None):
        self._log("read addr=%#08x len=%d", address, length)
        return await self._read_command(address, length, chunk_size, cmd=0x03,
                                        callback=callback)

    async def fast_read(self, address, length, chunk_size=None,
                        callback=lambda done, total, status: None):
        self._log("fast read addr=%#08x len=%d", address, length)
        return await self._read_command(address, length, chunk_size, cmd=0x0B, dummy=1,
                                        callback=callback)

    async def read_status(self):
        status, = await self._command(0x05, ret=1)
        self._log("read status=%s", "{:#010b}".format(status))
        return status

    async def write_enable(self):
        self._log("write enable")
        await self._command(0x06)

    async def write_disable(self):
        self._log("write disable")
        await self._command(0x04)

    async def write_in_progress(self, command="write"):
        status = await self.read_status()
        if status & BIT_WEL and not status & BIT_WIP:
            # Looks like some flashes (this was determined on Macronix MX25L3205D) have a race
            # condition between WIP going low and WEL going low, so we can sometimes observe
            # that. Work around by checking twice in a row. Sigh.
            status = await self.read_status()
            if status & BIT_WEL and not status & BIT_WIP:
                raise ProgramECP5Error("{} command failed (status {:08b})".format(command, status))
        return bool(status & BIT_WIP)

    async def write_status(self, status):
        self._log("write status=%s", "{:#010b}".format(status))
        await self._command(0x01, arg=[status])
        while await self.write_in_progress(command="WRITE STATUS"): pass

    async def sector_erase(self, address):
        self._log("sector erase addr=%#08x", address)
        await self._command(0x20, arg=self._format_addr(address))
        while await self.write_in_progress(command="SECTOR ERASE"): pass

    async def block_erase(self, address):
        self._log("block erase addr=%#08x", address)
        await self._command(0x52, arg=self._format_addr(address))
        while await self.write_in_progress(command="BLOCK ERASE"): pass

    async def chip_erase(self):
        self._log("chip erase")
        await self._command(0x60)
        while await self.write_in_progress(command="CHIP ERASE"): pass

    async def page_program(self, address, data):
        data = bytes(data)
        self._log("page program addr=%#08x data=<%s>", address, data.hex())
        await self._command(0x02, arg=self._format_addr(address) + data)
        while await self.write_in_progress(command="PAGE PROGRAM"): pass

    async def program(self, address, data, page_size,
                      callback=lambda done, total, status: None):
        data = bytes(data)
        done, total = 0, len(data)
        while len(data) > 0:
            chunk    = data[:page_size - address % page_size]
            data     = data[len(chunk):]

            callback(done, total, "programming page {:#08x}".format(address))
            await self.write_enable()
            await self.page_program(address, chunk)

            address += len(chunk)
            done    += len(chunk)

        callback(done, total, None)

    async def erase_program(self, address, data, sector_size, page_size,
                            callback=lambda done, total, status: None):
        data = bytes(data)
        done, total = 0, len(data)
        while len(data) > 0:
            chunk    = data[:sector_size - address % sector_size]
            data     = data[len(chunk):]

            sector_start = address & ~(sector_size - 1)
            if address % sector_size == 0 and len(chunk) == sector_size:
                sector_data = chunk
            else:
                sector_data = await self.read(sector_start, sector_size)
                sector_data[address % sector_size:(address % sector_size) + len(chunk)] = chunk

            callback(done, total, "erasing sector {:#08x}".format(sector_start))
            await self.write_enable()
            await self.sector_erase(sector_start)

            if not re.match(rb"^\xff*$", sector_data):
                await self.program(sector_start, sector_data, page_size,
                    callback=lambda page_done, page_total, status:
                                callback(done + page_done, total, status))

            address += len(chunk)
            done    += len(chunk)

        callback(done, total, None)

    async def enter_spi_mode(self):
        self._log("JTAG Reset")
        await self.lower.test_reset()
        await self.lower.enter_run_test_idle()
        self._log("Check IDcode")
        await self._sir("E0")
        idcode_bits = await self._sdr("00000000")
        idcode, = struct.unpack("<L", idcode_bits.tobytes())
        self._log("ID code=<%08X>",idcode)

        await self._sir("1C")
        await self._sdr("3FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF")
        await self._sir("C6")
        await self._sdr("00")
        await self._run_test(10)
        
        self._log("Erase ECP5 Bitstream") 
        await self._sir("0E")
        await self._sdr("01")
        await self._run_test(200)

        await self._sir("26")
        await self._run_test(10)

        await self._sir("FF")
        await self.lower.enter_run_test_idle()
        await self._run_test(20)

        self._log("Enter SPI passthrough") 
        await self._sir("3A")
        await self._sdr("68FE")
        await self.lower.enter_run_test_idle()
        await self._run_test(20)        


class ProgramECP5Applet(JTAGApplet, name="program-ecp5"):
    logger = logging.getLogger(__name__)
    help = "Program Lattice ECP5 FPGAs"
    description = """
    Connection to ECP5 over JTAG, support direct loading of bitstream, as well as 
    interfacing through backgrounud SPI mode to 25x SPI FLASH chips.

    When using this applet for erasing or programming, it is necessary to look up the page
    and sector sizes in the memory datasheet.
    """

    @classmethod
    def add_build_arguments(cls, parser, access):
        super().add_build_arguments(parser, access)

    async def run(self, device, args):
        jtag_iface = await super().run(device, args)
        await jtag_iface.pulse_trst()
        return ECP5ProgramInterface(jtag_iface, self.logger,args)
       

    @classmethod
    def add_interact_arguments(cls, parser):
        def address(arg):
            return int(arg, 0)
        def length(arg):
            return int(arg, 0)
        def hex_bytes(arg):
            return bytes.fromhex(arg)
        def bits(arg):
            return int(arg, 2)

        p_operation = parser.add_subparsers(dest="operation", metavar="OPERATION")

        p_identify = p_operation.add_parser(
            "identify", help="identify memory using REMS and RDID commands")

        def add_read_arguments(parser):
            parser.add_argument(
                "address", metavar="ADDRESS", type=address, default=0,
                help="read memory starting at address ADDRESS, with wraparound")
            parser.add_argument(
                "length", metavar="LENGTH", type=length, default=0,
                help="read LENGTH bytes from memory")
            parser.add_argument(
                "-f", "--file", metavar="FILENAME", type=argparse.FileType("wb"),
                help="write memory contents to FILENAME")

        p_read = p_operation.add_parser(
            "read", help="read memory using READ command")
        add_read_arguments(p_read)

        p_fast_read = p_operation.add_parser(
            "fast-read", help="read memory using FAST READ command")
        add_read_arguments(p_fast_read)

        def add_program_arguments(parser):
            parser.add_argument(
                "address", metavar="ADDRESS", type=address, default=0,
                help="program memory starting at address ADDRESS")
            g_data = parser.add_mutually_exclusive_group(required=True)
            g_data.add_argument(
                "-d", "--data", metavar="DATA", type=hex_bytes,
                help="program memory with DATA as hex bytes")
            g_data.add_argument(
                "-f", "--file", metavar="FILENAME", type=argparse.FileType("rb"),
                help="program memory with contents of FILENAME")

        p_program_page = p_operation.add_parser(
            "program-page", help="program memory page using PAGE PROGRAM command")
        add_program_arguments(p_program_page)

        def add_page_argument(parser):
            parser.add_argument(
                "-P", "--page-size", metavar="SIZE", type=length, required=True,
                help="program memory region using SIZE byte pages")

        p_program = p_operation.add_parser(
            "program", help="program a memory region using PAGE PROGRAM command")
        add_page_argument(p_program)
        add_program_arguments(p_program)

        def add_erase_arguments(parser, kind):
            parser.add_argument(
                "addresses", metavar="ADDRESS", type=address, nargs="+",
                help="erase %s(s) starting at address ADDRESS" % kind)

        p_erase_sector = p_operation.add_parser(
            "erase-sector", help="erase memory using SECTOR ERASE command")
        add_erase_arguments(p_erase_sector, "sector")

        p_erase_block = p_operation.add_parser(
            "erase-block", help="erase memory using BLOCK ERASE command")
        add_erase_arguments(p_erase_block, "block")

        p_erase_chip = p_operation.add_parser(
            "erase-chip", help="erase memory using CHIP ERASE command")

        p_erase_program = p_operation.add_parser(
            "erase-program", help="modify a memory region using SECTOR ERASE and "
                                  "PAGE PROGRAM commands")
        p_erase_program.add_argument(
            "-S", "--sector-size", metavar="SIZE", type=length, required=True,
            help="erase memory in SIZE byte sectors")
        add_page_argument(p_erase_program)
        add_program_arguments(p_erase_program)

        p_protect = p_operation.add_parser(
            "protect", help="query and set block protection using READ/WRITE STATUS "
                            "REGISTER commands")
        p_protect.add_argument(
            "bits", metavar="BITS", type=bits, nargs="?",
            help="set SR.BP[3:0] to BITS")

        p_verify = p_operation.add_parser(
            "verify", help="read memory using READ command and verify contents")
        add_program_arguments(p_verify)

    @staticmethod
    def _show_progress(done, total, status):
        if sys.stdout.isatty():
            sys.stdout.write("\r\033[0K")
            if done < total:
                sys.stdout.write("{}/{} bytes done".format(done, total))
                if status:
                    sys.stdout.write("; {}".format(status))
            sys.stdout.flush()

    async def interact(self, device, args, ecp5_iface):
        #await ecp5_iface.lower.pulse_trst()

        await ecp5_iface.enter_spi_mode()
        await ecp5_iface.wakeup()

        if args.operation in ("program-page", "program",
                              "erase-sector", "erase-block", "erase-chip",
                              "erase-program"):
            status = await ecp5_iface.read_status()
            if status & MSK_PROT:
                self.logger.warning("block protect bits are set to %s, program/erase command "
                                    "might not succeed", "{:04b}"
                                    .format((status & MSK_PROT) >> 2))

        if args.operation == "identify":
            manufacturer_id, device_id = \
                await ecp5_iface.read_manufacturer_device_id()
            manufacturer_name = jedec_mfg_name_from_bytes([manufacturer_id]) or "unknown"
            self.logger.info("JEDEC manufacturer %#04x (%s) device %#04x",
                              manufacturer_id, manufacturer_name, device_id)

        if args.operation in ("read", "fast-read"):
            if args.operation == "read":
                data = await ecp5_iface.read(args.address, args.length,
                                              callback=self._show_progress)
            if args.operation == "fast-read":
                data = await ecp5_iface.fast_read(args.address, args.length,
                                                   callback=self._show_progress)

            if args.file:
                args.file.write(data)
            else:
                print(data.hex())

        if args.operation in ("program-page", "program", "erase-program"):
            if args.data is not None:
                data = args.data
            if args.file is not None:
                data = args.file.read()

            if args.operation == "program-page":
                await ecp5_iface.write_enable()
                await ecp5_iface.page_program(args.address, data)
            if args.operation == "program":
                await ecp5_iface.program(args.address, data, args.page_size,
                                          callback=self._show_progress)
            if args.operation == "erase-program":
                await ecp5_iface.erase_program(args.address, data, args.sector_size,
                                                args.page_size, callback=self._show_progress)

        if args.operation == "verify":
            if args.data is not None:
                gold_data = args.data
            if args.file is not None:
                gold_data = args.file.read()

            flash_data = await ecp5_iface.read(args.address, len(gold_data))
            if gold_data == flash_data:
                self.logger.info("verify PASS")
            else:
                for offset, (gold_byte, flash_byte) in enumerate(zip(gold_data, flash_data)):
                    if gold_byte != flash_byte:
                        different_at = args.address + offset
                        break
                self.logger.error("first differing byte at %#08x (expected %#04x, actual %#04x)",
                                  different_at, gold_byte, flash_byte)
                raise GlasgowAppletError("verify FAIL")

        if args.operation in ("erase-sector", "erase-block"):
            for address in args.addresses:
                await ecp5_iface.write_enable()
                if args.operation == "erase-sector":
                    await ecp5_iface.sector_erase(address)
                if args.operation == "erase-block":
                    await ecp5_iface.block_erase(address)

        if args.operation == "erase-chip":
            await ecp5_iface.write_enable()
            await ecp5_iface.chip_erase()

        if args.operation == "protect":
            status = await ecp5_iface.read_status()
            if args.bits is None:
                self.logger.info("block protect bits are set to %s",
                                 "{:04b}".format((status & MSK_PROT) >> 2))
            else:
                status = (status & ~MSK_PROT) | ((args.bits << 2) & MSK_PROT)
                await ecp5_iface.write_enable()
                await ecp5_iface.write_status(status)


# -------------------------------------------------------------------------------------------------
