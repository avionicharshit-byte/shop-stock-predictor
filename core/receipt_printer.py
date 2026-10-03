"""Print the order list on a Bluetooth thermal receipt printer, 2 inch or 3 inch paper.

Plain ESC/POS text, sent over Bluetooth Low Energy through BlueZ (Linux), using busctl.
BlueZ must run with Experimental = true in /etc/bluetooth/main.conf, for ConnectDevice.
"""
import re
import subprocess
import textwrap
import time

import pandas as pd

# characters that fit on one line of each paper width
PAPER_WIDTHS = {"2 inch (58 mm)": 32, "3 inch (80 mm)": 48}
CONNECT_ATTEMPTS = 3
# the "transparent serial" write channel that PSF588 / SR588 style receipt printers expose
WRITE_CHANNEL = "49535343-8841-43f4-a8d4-ecbe34729bb3"
ADAPTER = "/org/bluez/hci0"
CHUNK_BYTES = 180
FALLBACK_CHUNK_BYTES = 20  # always fits, used when the printer does not report its packet size
# names and services that receipt printers show while they advertise
PRINTER_NAME_HINTS = ("PSF", "SR588", "MPT", "POS", "PRINTER", "RPP", "PT-")
PRINTER_SERVICES = ("000018f0-0000-1000-8000-00805f9b34fb", "e7810a71-73ae-499d-8c15-faa9aef0c3f2")
ADDRESS_SHAPE = r"([0-9A-F]{2}:){5}[0-9A-F]{2}"
INIT, CENTRE, LEFT, BOLD_ON, BOLD_OFF = b"\x1b@", b"\x1ba\x01", b"\x1ba\x00", b"\x1bE\x01", b"\x1bE\x00"


class PrinterProblem(OSError):
    """The printer could not be reached, worded for the shopkeeper."""


def _text(line: str) -> bytes:
    return line.encode("ascii", "replace") + b"\n"  # these printers only know plain English letters


def order_receipt(shop_name: str, address: str, order: pd.DataFrame, date: pd.Timestamp, line_width: int) -> bytes:
    out = INIT + CENTRE + BOLD_ON + _text(shop_name or "Order list") + BOLD_OFF
    for line in textwrap.wrap(address, line_width):
        out += _text(line)
    out += _text(f"Order list, {date:%d %b %Y}") + LEFT + _text("-" * line_width)
    for number, row in enumerate(order.itertuples(), 1):
        quantity = f"{row.Quantity:g} pcs"
        # the item name wraps, the quantity sits at the right end of its last line
        *first_lines, last_line = textwrap.wrap(f"{number}. {row.Item}", line_width - len(quantity) - 1)
        for line in first_lines:
            out += _text(line)
        out += _text(last_line.ljust(line_width - len(quantity)) + quantity)
    return out + _text("-" * line_width) + _text(f"{len(order)} items") + b"\n\n\n"


def _busctl(*args: str, timeout: int = 15) -> str:
    done = subprocess.run(["busctl", *args], capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0:
        raise OSError(done.stderr.strip() or "busctl failed")
    return done.stdout


def _connect(address: str) -> str:
    """Opens a Low Energy link and returns the BlueZ path of the device."""
    device = f"{ADAPTER}/dev_{address.replace(':', '_')}"
    # a printer that also speaks classic Bluetooth gets connected the classic way by a plain Connect,
    # and then refuses to print. forgetting it and using ConnectDevice forces Low Energy
    try:
        _busctl("call", "org.bluez", ADAPTER, "org.bluez.Adapter1", "RemoveDevice", "o", device)
    except OSError:
        pass  # it was not known yet
    # the first knock right after forgetting the printer often fails with no reason given, the next one works
    for attempt in range(CONNECT_ATTEMPTS):
        try:
            _busctl("call", "org.bluez", ADAPTER, "org.bluez.Adapter1", "ConnectDevice", "a{sv}", "2",
                    "Address", "s", address, "AddressType", "s", "public", timeout=30)
            return device
        except OSError:
            if attempt == CONNECT_ATTEMPTS - 1:
                raise OSError("it did not answer") from None
            time.sleep(0.8)


def _write_channel(device: str, wait_seconds: float = 15) -> str:
    """The printer's write channel shows up a moment after connecting."""
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        for path in re.findall(rf"{device}/service\w+/char\w+(?=\s|$)", _busctl("tree", "org.bluez"), re.M):
            if WRITE_CHANNEL in _busctl("get-property", "org.bluez", path, "org.bluez.GattCharacteristic1", "UUID"):
                return path
        time.sleep(0.3)
    raise OSError("this printer has no write channel I know")


def _chunk_size(channel: str) -> int:
    try:
        packet = int(_busctl("get-property", "org.bluez", channel, "org.bluez.GattCharacteristic1", "MTU").split()[1])
        return max(FALLBACK_CHUNK_BYTES, min(CHUNK_BYTES, packet - 3))
    except (OSError, ValueError, IndexError):
        return FALLBACK_CHUNK_BYTES


def _bluetoothctl(*args: str) -> str:
    return subprocess.run(["bluetoothctl", *args], capture_output=True, text=True, timeout=10).stdout


def _printer_name(details: str) -> str | None:
    """The name of a device seen in this scan, if it looks like a receipt printer."""
    name = re.search(r"^\s*Name: (.+)$", details, re.M)
    # bluez remembers devices from earlier scans, only the ones heard just now have a signal strength
    if not name or "RSSI:" not in details:
        return None
    is_printer = (any(hint in name[1].upper() for hint in PRINTER_NAME_HINTS)
                  or any(service in details.lower() for service in PRINTER_SERVICES))
    return name[1].strip() if is_printer else None


def find_printers(seconds: float = 8) -> list[tuple[str, str]]:
    """Scans for receipt printers nearby and returns (name, address) pairs, sorted by name."""
    try:
        # bluez stops scanning when the program that asked for it exits, so this one stays open meanwhile
        scan = subprocess.Popen(["bluetoothctl"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, text=True)
    except OSError as error:
        raise PrinterProblem(f"Could not search for printers. Check Bluetooth is switched on. ({error})") from None
    try:
        scan.stdin.write("scan le\n")
        scan.stdin.flush()
        time.sleep(seconds)
        printers = []
        for address in re.findall(rf"^Device ({ADDRESS_SHAPE.replace('(', '(?:')}) ", _bluetoothctl("devices"), re.M):
            name = _printer_name(_bluetoothctl("info", address))
            if name:
                printers.append((name, address))
        return sorted(printers)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PrinterProblem(f"Could not search for printers. Check Bluetooth is switched on. ({error})") from None
    finally:
        try:
            scan.communicate("scan off\nquit\n", timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            scan.kill()


def printer_label(printer: tuple[str, str]) -> str:
    return f"{printer[0]} ({printer[1]})"


def _clean_address(typed: str) -> str:
    address = typed.strip().upper().replace("-", ":")
    if not re.fullmatch(ADDRESS_SHAPE, address):
        raise PrinterProblem("The printer address should look like AA:BB:CC:DD:EE:FF.")
    return address


def _matching_printer(address: str, printers: list[tuple[str, str]]) -> tuple[str, str] | None:
    """The printer with this address, or the only one whose address ends the same way."""
    same_address = [printer for printer in printers if printer[1] == address]
    # the address on a printer's label can be its classic Bluetooth one, which differs from the
    # Low Energy one only in the first three bytes
    same_ending = [printer for printer in printers if printer[1][-8:] == address[-8:]]
    if same_address:
        return same_address[0]
    return same_ending[0] if len(same_ending) == 1 else None


def resolve_printer(typed_address: str, known_printers: list[tuple[str, str]] | None = None) -> tuple[str, str]:
    """Turns what the shopkeeper typed into a printer that is really nearby, scanning if needed."""
    address = _clean_address(typed_address)
    printer = _matching_printer(address, known_printers or [])
    if printer:
        return printer
    printers = find_printers()
    printer = _matching_printer(address, printers)
    if printer:
        return printer
    if not printers:
        raise PrinterProblem("No receipt printer was found nearby. Check the printer is switched on and close by.")
    raise PrinterProblem(f"No printer nearby has the address {address}. I found: "
                         + ", ".join(map(printer_label, printers)) + ". Press Find printers and pick one.")


def send_to_printer(bluetooth_address: str, data: bytes, known_printers: list[tuple[str, str]] | None = None) -> str:
    """Prints and returns the printer that was used, as "NAME (ADDRESS)"."""
    printer = resolve_printer(bluetooth_address, known_printers)
    address = printer[1]
    device = None
    try:
        device = _connect(address)
        channel = _write_channel(device)
        size = _chunk_size(channel)
        for start in range(0, len(data), size):
            chunk = data[start:start + size]
            # "request" makes the printer confirm each chunk, so a full buffer cannot silently drop lines
            _busctl("call", "org.bluez", channel, "org.bluez.GattCharacteristic1", "WriteValue", "aya{sv}",
                    str(len(chunk)), *map(str, chunk), "1", "type", "s", "request")
        time.sleep(1)  # let the last lines print before the link closes
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PrinterProblem(f"Could not print on {printer_label(printer)}. Check the printer is switched on "
                             f"and close by. ({error})") from None
    finally:
        if device:
            subprocess.run(["busctl", "call", "org.bluez", device, "org.bluez.Device1", "Disconnect"], capture_output=True)
    return printer_label(printer)
