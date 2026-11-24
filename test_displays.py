import ctypes
import math
import queue
import re
import sys
import threading
import tkinter as tk
import winreg
from ctypes import wintypes
from tkinter import messagebox
from typing import NamedTuple


class Monitor(NamedTuple):
    device: str
    bounds: tuple[int, int, int, int]
    primary: bool
    edid: bytes | None = None


class DisplayDevice(ctypes.Structure):
    _fields_ = (
        ("cb", wintypes.DWORD),
        ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", wintypes.WCHAR * 128),
        ("DeviceKey", wintypes.WCHAR * 128),
    )


class MonitorInfoEx(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    )


class PhysicalMonitor(ctypes.Structure):
    _fields_ = (
        ("handle", wintypes.HANDLE),
        ("description", wintypes.WCHAR * 128),
    )


def request_windows_pnp_rescan():
    if sys.platform != "win32":
        raise RuntimeError("Plug and Play rescanning is only available on Windows.")

    config_manager = ctypes.WinDLL("CfgMgr32", use_last_error=True)
    locate_root = config_manager.CM_Locate_DevNodeW
    locate_root.argtypes = [
        ctypes.POINTER(wintypes.ULONG),
        wintypes.LPCWSTR,
        wintypes.ULONG,
    ]
    locate_root.restype = wintypes.ULONG
    reenumerate = config_manager.CM_Reenumerate_DevNode
    reenumerate.argtypes = [wintypes.ULONG, wintypes.ULONG]
    reenumerate.restype = wintypes.ULONG

    root_device = wintypes.ULONG()
    result = locate_root(ctypes.byref(root_device), None, 0)
    if result:
        raise OSError(
            f"CM_Locate_DevNodeW failed with Configuration Manager code "
            f"0x{result:04X}."
        )

    result = reenumerate(root_device, 0x00000001)
    if result:
        hint = (
            " This operation requires SeLoadDriverPrivilege; try running the "
            "app as administrator."
            if result == 0x33
            else ""
        )
        raise OSError(
            f"CM_Reenumerate_DevNode failed with Configuration Manager code "
            f"0x{result:04X}.{hint}"
        )


def get_monitors():
    if sys.platform != "win32":
        raise RuntimeError("This application uses Windows display enumeration.")

    user32 = ctypes.windll.user32
    enumerate_display_devices = user32.EnumDisplayDevicesW
    enumerate_display_devices.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(DisplayDevice),
        wintypes.DWORD,
    ]
    enumerate_display_devices.restype = wintypes.BOOL

    monitors = []
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HANDLE,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )

    get_monitor_info = user32.GetMonitorInfoW
    get_monitor_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfoEx)]
    get_monitor_info.restype = wintypes.BOOL

    def collect_monitor(handle, _dc, _rect_pointer, _data):
        info = MonitorInfoEx()
        info.cbSize = ctypes.sizeof(info)
        if not get_monitor_info(handle, ctypes.byref(info)):
            return False
        rect = info.rcMonitor
        edid = get_monitor_edid(info.szDevice, enumerate_display_devices)
        monitors.append(
            Monitor(
                info.szDevice,
                (rect.left, rect.top, rect.right, rect.bottom),
                bool(info.dwFlags & 1),
                edid,
            )
        )
        return True

    callback = callback_type(collect_monitor)
    enumerate_monitors = user32.EnumDisplayMonitors
    enumerate_monitors.argtypes = [
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        callback_type,
        wintypes.LPARAM,
    ]
    enumerate_monitors.restype = wintypes.BOOL
    if not enumerate_monitors(None, None, callback, 0):
        raise ctypes.WinError()
    return monitors


def query_ddcci_capabilities(device_name):
    dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)
    get_monitor_info = ctypes.windll.user32.GetMonitorInfoW
    get_monitor_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfoEx)]
    get_monitor_info.restype = wintypes.BOOL

    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HANDLE,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )
    matching_handles = []

    def collect_monitor(handle, _dc, _rect, _data):
        info = MonitorInfoEx()
        info.cbSize = ctypes.sizeof(info)
        if get_monitor_info(handle, ctypes.byref(info)) and info.szDevice == device_name:
            matching_handles.append(handle)
        return True

    callback = callback_type(collect_monitor)
    enumerate_monitors = ctypes.windll.user32.EnumDisplayMonitors
    enumerate_monitors.argtypes = [
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        callback_type,
        wintypes.LPARAM,
    ]
    enumerate_monitors.restype = wintypes.BOOL
    if not enumerate_monitors(None, None, callback, 0):
        raise ctypes.WinError()
    if not matching_handles:
        raise RuntimeError(f"Display {device_name} is no longer connected.")

    get_count = dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR
    get_count.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    get_count.restype = wintypes.BOOL
    count = wintypes.DWORD()
    if not get_count(matching_handles[0], ctypes.byref(count)):
        raise ctypes.WinError()
    if not count.value:
        return "Windows reports no physical monitor for this display."

    physical_monitors = (PhysicalMonitor * count.value)()
    get_physical = dxva2.GetPhysicalMonitorsFromHMONITOR
    get_physical.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(PhysicalMonitor),
    ]
    get_physical.restype = wintypes.BOOL
    if not get_physical(
        matching_handles[0], count.value, physical_monitors
    ):
        raise ctypes.WinError()

    get_length = dxva2.GetCapabilitiesStringLength
    get_length.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_length.restype = wintypes.BOOL
    get_capabilities = dxva2.CapabilitiesRequestAndCapabilitiesReply
    get_capabilities.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_char),
        wintypes.DWORD,
    ]
    get_capabilities.restype = wintypes.BOOL
    destroy_physical = dxva2.DestroyPhysicalMonitors
    destroy_physical.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(PhysicalMonitor),
    ]
    destroy_physical.restype = wintypes.BOOL

    results = []
    try:
        for index, physical in enumerate(physical_monitors):
            lines = [f"Physical monitor {index + 1}: {physical.description}"]
            length = wintypes.DWORD()
            if not get_length(physical.handle, ctypes.byref(length)):
                error_code = ctypes.get_last_error() & 0xFFFFFFFF
                lines.append(
                    "DDC/CI capabilities unavailable "
                    f"(Windows status 0x{error_code:08X})."
                )
                results.append("\n".join(lines))
                continue
            if not length.value:
                lines.append("Monitor returned an empty DDC/CI capabilities string.")
                results.append("\n".join(lines))
                continue

            buffer = ctypes.create_string_buffer(length.value)
            if not get_capabilities(
                physical.handle,
                ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)),
                length.value,
            ):
                error_code = ctypes.get_last_error() & 0xFFFFFFFF
                lines.append(
                    "DDC/CI capabilities read failed "
                    f"(Windows status 0x{error_code:08X})."
                )
                results.append("\n".join(lines))
                continue

            capabilities = buffer.value.decode("ascii", errors="replace")
            lines.append(f"Capabilities: {capabilities}")
            vcp_match = re.search(r"\bvcp\(([^)]*)\)", capabilities, re.IGNORECASE)
            if vcp_match:
                codes = re.findall(r"[0-9A-Fa-f]{2}", vcp_match.group(1))
                lines.append(
                    "Supported VCP feature codes: "
                    + (", ".join(f"0x{code.upper()}" for code in codes) or "none")
                )
            results.append("\n".join(lines))
    finally:
        destroy_physical(count.value, physical_monitors)
    return "\n\n".join(results)


def get_monitor_edid(device_name, enumerate_display_devices):
    display = DisplayDevice()
    display.cb = ctypes.sizeof(display)
    if not enumerate_display_devices(device_name, 0, ctypes.byref(display), 0):
        return None

    device_id = display.DeviceID.split("\\")
    if len(device_id) < 4 or device_id[0].upper() != "MONITOR":
        return None

    try:
        display_key_path = rf"SYSTEM\CurrentControlSet\Enum\DISPLAY\{device_id[1]}"
        driver_suffix = rf"{device_id[2]}\{device_id[-1]}".casefold()
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, display_key_path) as key:
            for index in range(winreg.QueryInfoKey(key)[0]):
                instance = winreg.EnumKey(key, index)
                try:
                    with winreg.OpenKey(key, instance) as instance_key:
                        driver = winreg.QueryValueEx(instance_key, "Driver")[0]
                        if str(driver).casefold() != driver_suffix:
                            continue
                        with winreg.OpenKey(
                            instance_key, "Device Parameters"
                        ) as parameters:
                            edid = winreg.QueryValueEx(parameters, "EDID")[0]
                        break
                except OSError:
                    continue
            else:
                return None
    except OSError:
        return None
    return edid if isinstance(edid, bytes) and edid else None


def parse_edid(edid):
    if len(edid) < 128:
        return ["EDID data is incomplete (fewer than 128 bytes)."]

    lines = []
    header_valid = edid[:8] == b"\x00\xff\xff\xff\xff\xff\xff\x00"
    lines.append(f"EDID header: {'valid' if header_valid else 'invalid'}")

    manufacturer_code = (edid[8] << 8) | edid[9]
    manufacturer = "".join(
        chr(((manufacturer_code >> shift) & 0x1F) + 64)
        if ((manufacturer_code >> shift) & 0x1F)
        else "?"
        for shift in (10, 5, 0)
    )
    product_code = edid[10] | (edid[11] << 8)
    serial_number = int.from_bytes(edid[12:16], "little")
    week = edid[16]
    year = 1990 + edid[17]
    lines.extend(
        (
            f"Manufacturer ID: {manufacturer}",
            f"Product code: 0x{product_code:04X} ({product_code})",
            f"Numeric serial: {serial_number}",
            (
                f"Manufacture date: {year}, week {week}"
                if 1 <= week <= 53
                else f"Manufacture date: {year} (week unspecified)"
            ),
            f"EDID version: {edid[18]}.{edid[19]}",
        )
    )

    if edid[20] & 0x80:
        lines.append("Input: digital")
        depth_codes = {
            0: "undefined",
            1: "6 bits per color",
            2: "8 bits per color",
            3: "10 bits per color",
            4: "12 bits per color",
            5: "14 bits per color",
            6: "16 bits per color",
            7: "reserved",
        }
        lines.append(f"Color bit depth: {depth_codes[(edid[20] >> 4) & 7]}")
        interface_codes = {
            0: "undefined",
            1: "DVI",
            2: "HDMI-a",
            3: "HDMI-b",
            4: "MDDI",
            5: "DisplayPort",
        }
        lines.append(
            "Digital interface: "
            + interface_codes.get(edid[20] & 0x0F, "reserved/unknown")
        )
    else:
        signal_level = (edid[20] >> 5) & 3
        sync_flags = (
            "blank-to-black setup" if edid[20] & 0x10 else "no blank-to-black setup",
            "separate sync supported" if edid[20] & 0x08 else "composite sync only",
            "sync on green supported" if edid[20] & 0x04 else "no sync on green",
            "serration supported" if edid[20] & 0x02 else "no serration",
        )
        lines.append(
            f"Input: analog; signal level code {signal_level}; "
            + ", ".join(sync_flags)
        )

    width_cm, height_cm = edid[21], edid[22]
    lines.append(f"Physical image size: {width_cm} x {height_cm} cm")
    lines.append(
        "Gamma: undefined"
        if edid[23] == 0xFF
        else f"Gamma: {edid[23] / 100 + 1:.2f}"
    )
    features = edid[24]
    power = ("standby", "suspend", "active off")
    lines.append(
        "Feature support: "
        + (", ".join(name for bit, name in zip((7, 6, 5), power)
                     if features & (1 << bit))
           or "no DPMS states reported")
        + (", sRGB" if features & 0x04 else "")
        + (", preferred timing in descriptor 1" if features & 0x02 else "")
        + (", GTF supported" if features & 0x01 else "")
    )

    chromaticity = tuple(
        (
            (edid[27 + index] << 2)
            | ((edid[25 + index // 4] >> (6 - 2 * (index % 4))) & 0x03)
        )
        / 1024
        for index in range(8)
    )
    lines.append(
        "Chromaticity (x,y): "
        + ", ".join(
            f"{name}=({chromaticity[index]:.3f},{chromaticity[index + 1]:.3f})"
            for name, index in (("R", 0), ("G", 2), ("B", 4), ("W", 6))
        )
    )

    established_timings = []
    timing_tables = (
        (
            35,
            (
                "720x400@70",
                "720x400@88",
                "640x480@60",
                "640x480@67",
                "640x480@72",
                "640x480@75",
                "800x600@56",
                "800x600@60",
            ),
        ),
        (
            36,
            (
                "800x600@72",
                "800x600@75",
                "832x624@75",
                "1024x768@87i",
                "1024x768@60",
                "1024x768@70",
                "1024x768@75",
                "1280x1024@75",
            ),
        ),
    )
    for byte_index, names in timing_tables:
        for bit, name in enumerate(names):
            if edid[byte_index] & (1 << (7 - bit)):
                established_timings.append(name)
    if edid[37] & 0x80:
        established_timings.append("1152x870@75")
    lines.append(
        "Established timings: "
        + (", ".join(established_timings) if established_timings else "none")
    )

    standard_timings = []
    for index in range(8):
        x_resolution, aspect = edid[38 + index * 2 : 40 + index * 2]
        if x_resolution == 0x01 and aspect == 0x01:
            continue
        width = (x_resolution + 31) * 8
        aspect_ratio = aspect >> 6
        if aspect_ratio == 0:
            ratio = "16:10" if edid[19] >= 3 else "1:1"
            height = round(width * (10 / 16 if edid[19] >= 3 else 1))
        elif aspect_ratio == 1:
            ratio, height = "4:3", round(width * 3 / 4)
        elif aspect_ratio == 2:
            ratio, height = "5:4", round(width * 4 / 5)
        else:
            ratio, height = "16:9", round(width * 9 / 16)
        refresh = (aspect & 0x3F) + 60
        standard_timings.append(f"{width}x{height}@{refresh}Hz ({ratio})")
    lines.append(
        "Standard timings: "
        + (", ".join(standard_timings) if standard_timings else "none")
    )

    for descriptor_index, start in enumerate(range(54, 126, 18), 1):
        descriptor = edid[start : start + 18]
        pixel_clock = int.from_bytes(descriptor[:2], "little") * 10_000
        if pixel_clock:
            horizontal_active = descriptor[2] | ((descriptor[4] & 0xF0) << 4)
            horizontal_blank = descriptor[3] | ((descriptor[4] & 0x0F) << 8)
            vertical_active = descriptor[5] | ((descriptor[7] & 0xF0) << 4)
            vertical_blank = descriptor[6] | ((descriptor[7] & 0x0F) << 8)
            horizontal_offset = descriptor[8] | ((descriptor[11] & 0xC0) << 2)
            horizontal_sync = descriptor[9] | ((descriptor[11] & 0x30) << 4)
            vertical_offset = (descriptor[10] >> 4) | ((descriptor[11] & 0x0C) << 2)
            vertical_sync = (descriptor[10] & 0x0F) | ((descriptor[11] & 0x03) << 4)
            refresh = (
                pixel_clock
                / max(1, (horizontal_active + horizontal_blank)
                      * (vertical_active + vertical_blank))
            )
            lines.append(
                f"Detailed timing {descriptor_index}: "
                f"{horizontal_active}x{vertical_active} at ~{refresh:.2f}Hz, "
                f"{pixel_clock / 1_000_000:.3f}MHz pixel clock, "
                f"H blank/offset/sync={horizontal_blank}/{horizontal_offset}/"
                f"{horizontal_sync}, V blank/offset/sync={vertical_blank}/"
                f"{vertical_offset}/{vertical_sync}, "
                f"interlaced={bool(descriptor[17] & 0x80)}"
            )
            continue

        if descriptor[0:2] != b"\x00\x00" or descriptor[2] != 0:
            lines.append(f"Descriptor {descriptor_index}: unrecognized descriptor")
            continue
        tag = descriptor[3]
        payload = descriptor[5:18]
        if tag in (0xFC, 0xFF, 0xFE):
            text = payload.split(b"\n", 1)[0].decode("ascii", errors="replace").strip()
            name = {0xFC: "Display name", 0xFF: "Text serial", 0xFE: "Text"}.get(tag)
            lines.append(f"{name}: {text or '(empty)'}")
        elif tag == 0xFD:
            lines.append(
                f"Range limits: vertical {payload[0]}-{payload[1]}Hz, "
                f"horizontal {payload[2]}-{payload[3]}kHz, "
                f"max pixel clock {payload[4] * 10}MHz"
            )
        else:
            lines.append(f"Descriptor {descriptor_index}: tag 0x{tag:02X}")

    extension_count = edid[126]
    available_blocks = min(extension_count + 1, len(edid) // 128)
    lines.append(
        f"Extension blocks: {extension_count} declared, "
        f"{max(0, available_blocks - 1)} available"
    )
    for block_index in range(1, available_blocks):
        block = edid[block_index * 128 : (block_index + 1) * 128]
        extension_name = {
            0x02: "CEA/CTA-861",
            0x10: "Video Timing Extension",
            0x40: "Display Information Extension",
            0xF0: "Block map",
        }.get(block[0], "unknown")
        lines.append(
            f"Extension {block_index}: tag 0x{block[0]:02X} ({extension_name}), "
            f"revision {block[1]}, checksum "
            f"{'valid' if sum(block) % 256 == 0 else 'INVALID'}"
        )
        if block[0] == 0x02:
            lines.extend(parse_cta_extension(block))

    for block_index in range(available_blocks):
        block = edid[block_index * 128 : (block_index + 1) * 128]
        lines.append(
            f"Block {block_index} checksum: "
            f"{'valid' if sum(block) % 256 == 0 else 'INVALID'}"
        )
    if len(edid) > available_blocks * 128:
        lines.append(
            f"Unparsed trailing data: {len(edid) - available_blocks * 128} bytes"
        )
    return lines


def parse_cta_extension(block):
    lines = []
    data_end = block[2] if block[2] else 127
    data_end = min(max(data_end, 4), 127)
    lines.append(f"  CTA flags: 0x{block[3]:02X}")
    lines.append(
        "  CTA capabilities: "
        + (f"underscan={bool(block[3] & 0x80)}, " if block[3] else "")
        + f"basic audio={bool(block[3] & 0x40)}, "
        f"YCbCr 4:4:4={bool(block[3] & 0x20)}, "
        f"YCbCr 4:2:2={bool(block[3] & 0x10)}"
    )
    index = 4
    while index < data_end:
        header = block[index]
        tag, length = header >> 5, header & 0x1F
        payload = block[index + 1 : min(index + 1 + length, data_end)]
        if tag == 1:
            audio_formats = []
            for offset in range(0, len(payload) - 2, 3):
                first, second, _third = payload[offset : offset + 3]
                format_code = (first >> 3) & 0x0F
                channels = (first & 7) + 1
                sample_rates = [
                    name for bit, name in enumerate(
                        ("32", "44.1", "48", "88.2", "96", "176.4", "192")
                    )
                    if second & (1 << bit)
                ]
                audio_formats.append(
                    f"format {format_code}, {channels}ch, "
                    f"{'/'.join(sample_rates)}kHz"
                )
            lines.append(
                "  Audio formats: "
                + (", ".join(audio_formats) if audio_formats else "none")
            )
        elif tag == 2:
            lines.append(
                "  CTA video data block (SVD): "
                + (", ".join(
                    f"VIC {value & 0x7F}"
                    + (" (native)" if value & 0x80 else "")
                    for value in payload
                ) or "empty")
            )
        elif tag == 3 and len(payload) >= 3:
            oui = int.from_bytes(payload[:3], "little")
            lines.append(f"  Vendor-specific data block: OUI 0x{oui:06X}")
        elif tag == 4 and payload:
            lines.append(
                f"  Speaker allocation block: 0x{payload[0]:02X}"
            )
        else:
            lines.append(
                f"  CTA data block tag {tag}, {len(payload)} byte(s): "
                f"{payload.hex(' ').upper() or '(empty)'}"
            )
        index += 1 + length
    return lines


def format_edid_report(monitor):
    lines = [
        f"Display: {monitor.device}",
        f"Current desktop mode: "
        f"{monitor.bounds[2] - monitor.bounds[0]} x "
        f"{monitor.bounds[3] - monitor.bounds[1]}",
        "DDC/EDID source: Windows monitor EDID registry data",
        "EDID includes monitor identification and advertised timings. Live "
        "DDC/CI capabilities are queried below; controls are not changed.",
    ]
    if monitor.edid is None:
        lines.extend(
            (
                "",
                "EDID unavailable from Windows. The monitor, adapter, or cable "
                "may not expose DDC data.",
            )
        )
        return "\n".join(lines)

    lines.append("")
    lines.extend(parse_edid(monitor.edid))
    lines.extend(("", f"Raw EDID ({len(monitor.edid)} bytes):"))
    lines.extend(
        f"{offset:04X}: {monitor.edid[offset:offset + 16].hex(' ').upper()}"
        for offset in range(0, len(monitor.edid), 16)
    )
    return "\n".join(lines)


def line_segment(width, height, angle, offset):
    cosine = math.cos(angle)
    sine = math.sin(angle)
    points = []

    if abs(sine) > 1e-9:
        for x in (0, width):
            y = (offset - x * cosine) / sine
            if 0 <= y <= height:
                points.append((x, y))

    if abs(cosine) > 1e-9:
        for y in (0, height):
            x = (offset - y * sine) / cosine
            if 0 <= x <= width:
                points.append((x, y))

    unique_points = []
    for point in points:
        if not any(
            abs(point[0] - other[0]) < 0.01
            and abs(point[1] - other[1]) < 0.01
            for other in unique_points
        ):
            unique_points.append(point)
    if len(unique_points) < 2:
        return None
    return unique_points[0], unique_points[1]


PATTERN_NAMES = (
    "Moiré",
    "VGA color bars",
    "Grayscale ramp",
    "Vertical pixel-clock stripes",
    "Horizontal pixel-clock stripes",
    "Checkerboard",
    "Geometry and convergence grid",
    "VGA sync and overscan",
    "Solid color fields",
)


class TestPatternWindow:
    def __init__(self, window, bounds, advance):
        self.window = window
        self.left, self.top, self.right, self.bottom = bounds
        self.advance = advance
        self.pattern_index = 0
        self.canvas = tk.Canvas(
            window,
            background="black",
            highlightthickness=0,
            borderwidth=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self.redraw)
        self.canvas.bind("<Button-1>", self.advance)
        window.overrideredirect(True)
        window.geometry(
            f"{self.right - self.left}x{self.bottom - self.top}"
            f"{self.left:+d}{self.top:+d}"
        )
        window.attributes("-topmost", True)

    def move_to(self, bounds):
        self.left, self.top, self.right, self.bottom = bounds
        self.window.geometry(
            f"{self.right - self.left}x{self.bottom - self.top}"
            f"{self.left:+d}{self.top:+d}"
        )

    def redraw(self, event=None):
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if width <= 1 or height <= 1:
            return

        self.canvas.delete("all")
        draw_pattern = (
            self.draw_moire,
            self.draw_color_bars,
            self.draw_grayscale,
            self.draw_vertical_stripes,
            self.draw_horizontal_stripes,
            self.draw_checkerboard,
            self.draw_geometry,
            self.draw_sync_overscan,
            self.draw_solid_fields,
        )[self.pattern_index]
        draw_pattern(width, height)

    def set_pattern(self, index):
        self.pattern_index = index % len(PATTERN_NAMES)
        self.redraw()

    def draw_moire(self, width, height):
        canvas = self.canvas
        spacing = 8.0
        grids = ((-0.0175, spacing), (0.0175, spacing + 0.08))

        for rotation, pitch in grids:
            for angle in (rotation, rotation + math.pi / 2):
                cosine = math.cos(angle)
                sine = math.sin(angle)
                minimum = min(0, width * cosine) + min(0, height * sine)
                maximum = max(0, width * cosine) + max(0, height * sine)
                offset = math.floor(minimum / pitch) * pitch
                while offset <= maximum:
                    segment = line_segment(width, height, angle, offset)
                    if segment is not None:
                        start, end = segment
                        canvas.create_line(
                            *start,
                            *end,
                            fill="#111111",
                            width=1,
                        )
                    offset += pitch


    def draw_color_bars(self, width, height):
        canvas = self.canvas
        bars = ("#ffffff", "#ffff00", "#00ffff", "#00ff00",
                "#ff00ff", "#ff0000", "#0000ff")
        bar_width = width / len(bars)
        for index, color in enumerate(bars):
            canvas.create_rectangle(
                index * bar_width, 0, (index + 1) * bar_width, height * 0.72,
                fill=color, outline=color,
            )
        lower = ("#0000ff", "#000000", "#ff00ff", "#000000",
                 "#00ffff", "#000000", "#ffffff")
        for index, color in enumerate(lower):
            canvas.create_rectangle(
                index * bar_width, height * 0.72,
                (index + 1) * bar_width, height,
                fill=color, outline=color,
            )

    def draw_grayscale(self, width, height):
        canvas = self.canvas
        for index in range(16):
            level = round(index * 255 / 15)
            color = f"#{level:02x}{level:02x}{level:02x}"
            x0 = index * width / 16
            x1 = (index + 1) * width / 16
            canvas.create_rectangle(x0, 0, x1, height, fill=color, outline=color)

    def draw_vertical_stripes(self, width, height):
        canvas = self.canvas
        for x in range(0, width, 2):
            canvas.create_line(x, 0, x, height, fill="white")

    def draw_horizontal_stripes(self, width, height):
        canvas = self.canvas
        for y in range(0, height, 2):
            canvas.create_line(0, y, width, y, fill="white")

    def draw_checkerboard(self, width, height):
        canvas = self.canvas
        cell = max(2, min(width, height) // 32)
        for row, y in enumerate(range(0, height, cell)):
            for column, x in enumerate(range(0, width, cell)):
                if (row + column) % 2 == 0:
                    canvas.create_rectangle(
                        x, y, x + cell, y + cell, fill="white", outline="white"
                    )

    def draw_geometry(self, width, height):
        canvas = self.canvas
        spacing = max(16, min(width, height) // 24)
        for x in range(0, width, spacing):
            canvas.create_line(x, 0, x, height, fill="#666666")
        for y in range(0, height, spacing):
            canvas.create_line(0, y, width, y, fill="#666666")

        cx, cy = width / 2, height / 2
        canvas.create_line(cx, 0, cx, height, fill="white", width=2)
        canvas.create_line(0, cy, width, cy, fill="white", width=2)
        for fraction in (0.2, 0.4, 0.6, 0.8):
            radius_x = width * fraction / 2
            radius_y = height * fraction / 2
            canvas.create_oval(
                cx - radius_x, cy - radius_y,
                cx + radius_x, cy + radius_y,
                outline="white",
            )
        size = min(width, height) * 0.04
        for x, y in ((0, 0), (width, 0), (0, height), (width, height)):
            canvas.create_oval(
                x - size, y - size, x + size, y + size,
                outline="white", width=2,
            )

    def draw_sync_overscan(self, width, height):
        canvas = self.canvas
        canvas.create_rectangle(0, 0, width, height, fill="black", outline="black")
        for inset, color in ((0.02, "#444444"), (0.05, "white"), (0.1, "#888888")):
            dx, dy = width * inset, height * inset
            canvas.create_rectangle(
                dx, dy, width - dx, height - dy, outline=color, width=2
            )
        cx, cy = width / 2, height / 2
        canvas.create_line(cx, 0, cx, height, fill="white")
        canvas.create_line(0, cy, width, cy, fill="white")

        patch_width = width * 0.16
        patch_height = height * 0.12
        for index in range(4):
            x0 = width * 0.08 + index * patch_width
            for column in range(16):
                if column % 2 == 0:
                    x = x0 + column * patch_width / 16
                    canvas.create_rectangle(
                        x, height * 0.08, x + patch_width / 16,
                        height * 0.08 + patch_height,
                        fill="white", outline="white",
                    )
                    canvas.create_rectangle(
                        x, height * 0.8, x + patch_width / 16,
                        height * 0.8 + patch_height,
                        fill="white", outline="white",
                    )
        for index in range(4):
            y0 = height * 0.24 + index * patch_height
            for row in range(16):
                if row % 2 == 0:
                    y = y0 + row * patch_height / 16
                    canvas.create_rectangle(
                        width * 0.08, y, width * 0.08 + patch_width,
                        y + patch_height / 16,
                        fill="white", outline="white",
                    )
                    canvas.create_rectangle(
                        width * 0.76, y, width * 0.76 + patch_width,
                        y + patch_height / 16,
                        fill="white", outline="white",
                    )

    def draw_solid_fields(self, width, height):
        colors = ("black", "white", "#ff0000", "#00ff00", "#0000ff")
        color_width = width / len(colors)
        for index, color in enumerate(colors):
            x0 = index * color_width
            self.canvas.create_rectangle(
                x0, 0, (index + 1) * color_width, height,
                fill=color, outline=color,
            )


class MonitorSelector:
    def __init__(self, root):
        self.root = root
        self.monitors = None
        self.test_windows = {}
        self.test_toplevels = {}
        self.active_devices = set()
        self.pattern_index = 0
        self.status = tk.StringVar(value="Detecting displays...")
        self.monitor_items = {}
        self.selected_monitor_devices = set()
        self.focused_device = None
        self.ddc_ci_cache = {}
        self.ddc_ci_pending = set()
        self.ddc_ci_results = queue.Queue()
        self.rescan_results = queue.Queue()
        self.rescan_pending = False
        self.rescan_message = ""

        root.title("LCD / VGA Display Test")
        root.resizable(True, True)
        root.minsize(620, 420)
        root.protocol("WM_DELETE_WINDOW", root.destroy)

        frame = tk.Frame(root, padx=16, pady=14)
        frame.pack(fill="both", expand=True)
        tk.Label(
            frame,
            text="Click a display to select it for testing",
            anchor="center",
            font=("Segoe UI", 13, "bold"),
        ).pack(fill="x", pady=(0, 10))

        self.display_canvas = tk.Canvas(
            frame,
            background="#151922",
            highlightthickness=0,
            borderwidth=0,
        )
        self.display_canvas.pack(fill="both", expand=True)
        self.display_canvas.bind("<Configure>", self.draw_display_map)
        self.display_canvas.bind("<Button-1>", self.toggle_display)

        details_frame = tk.LabelFrame(
            frame,
            text="Display DDC / EDID information",
            padx=6,
            pady=6,
        )
        details_frame.pack(fill="x", pady=(8, 8))
        self.details_text = tk.Text(
            details_frame,
            height=9,
            wrap="none",
            state="disabled",
            font=("Consolas", 9),
        )
        details_scrollbar = tk.Scrollbar(
            details_frame, orient="vertical", command=self.details_text.yview
        )
        horizontal_scrollbar = tk.Scrollbar(
            details_frame, orient="horizontal", command=self.details_text.xview
        )
        self.details_text.configure(
            yscrollcommand=details_scrollbar.set,
            xscrollcommand=horizontal_scrollbar.set,
        )
        self.details_text.grid(row=0, column=0, sticky="nsew")
        details_scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal_scrollbar.grid(row=1, column=0, sticky="ew")
        details_frame.grid_columnconfigure(0, weight=1)
        details_frame.grid_rowconfigure(0, weight=1)

        buttons = tk.Frame(frame)
        buttons.pack(fill="x")
        tk.Button(buttons, text="Refresh", command=self.refresh_displays).pack(
            side="left"
        )
        tk.Button(
            buttons,
            text="Rescan hardware",
            command=self.request_hardware_rescan,
        ).pack(side="left", padx=(6, 0))
        tk.Button(buttons, text="Select all", command=self.select_all).pack(
            side="left", padx=(6, 0)
        )
        tk.Button(buttons, text="Clear", command=self.clear_selection).pack(
            side="left", padx=(6, 0)
        )
        tk.Button(buttons, text="Start tests", command=self.start_tests).pack(
            side="right"
        )
        tk.Label(frame, textvariable=self.status, anchor="w").pack(
            fill="x", pady=(8, 0)
        )

        root.bind_all("<space>", self.on_space)
        root.bind_all("<Escape>", self.on_escape)
        self.refresh_displays()
        self.request_hardware_rescan()
        self.root.after(2000, self.poll_displays)
        self.root.after(100, self.poll_ddcci_results)

    def selected_devices(self):
        return set(self.selected_monitor_devices)

    def refresh_displays(self):
        try:
            monitors = get_monitors()
        except (OSError, RuntimeError) as error:
            self.status.set(f"Display detection failed: {error}")
            return

        display_status = (
            f"{len(monitors)} display(s) detected. "
            "The list refreshes automatically every 2 seconds."
            if monitors
            else "No displays detected. Connect a display and refresh."
        )
        if self.rescan_message:
            display_status += f"  |  {self.rescan_message}"
        self.status.set(display_status)
        if monitors == self.monitors:
            if self.active_devices:
                self.sync_test_windows()
            return

        selected = self.selected_devices()
        first_scan = self.monitors is None
        self.monitors = monitors
        current_devices = {monitor.device for monitor in monitors}
        self.selected_monitor_devices = selected & current_devices
        if first_scan:
            self.selected_monitor_devices.update(
                monitor.device for monitor in monitors if monitor.primary
            )
        if self.focused_device not in current_devices:
            self.focused_device = next(
                iter(self.selected_monitor_devices), None
            )
        self.draw_display_map()
        self.update_details()

        if self.active_devices:
            self.sync_test_windows()

    def draw_display_map(self, _event=None):
        canvas = self.display_canvas
        canvas.delete("all")
        self.monitor_items.clear()
        if not self.monitors:
            canvas.create_text(
                max(1, canvas.winfo_width() / 2),
                max(1, canvas.winfo_height() / 2),
                text="No displays detected",
                fill="#d5d9e2",
                font=("Segoe UI", 16),
            )
            return

        canvas.update_idletasks()
        canvas_width = max(1, canvas.winfo_width())
        canvas_height = max(1, canvas.winfo_height())
        min_x = min(monitor.bounds[0] for monitor in self.monitors)
        min_y = min(monitor.bounds[1] for monitor in self.monitors)
        max_x = max(monitor.bounds[2] for monitor in self.monitors)
        max_y = max(monitor.bounds[3] for monitor in self.monitors)
        desktop_width = max(1, max_x - min_x)
        desktop_height = max(1, max_y - min_y)

        padding = 44
        available_width = max(1, canvas_width - 2 * padding)
        available_height = max(1, canvas_height - 2 * padding)
        scale = min(
            available_width / desktop_width,
            available_height / desktop_height,
        )
        map_width = desktop_width * scale
        map_height = desktop_height * scale
        origin_x = (canvas_width - map_width) / 2
        origin_y = (canvas_height - map_height) / 2

        for monitor in self.monitors:
            left, top, right, bottom = monitor.bounds
            x0 = origin_x + (left - min_x) * scale
            y0 = origin_y + (top - min_y) * scale
            x1 = origin_x + (right - min_x) * scale
            y1 = origin_y + (bottom - min_y) * scale
            self.draw_monitor_tile(monitor, x0, y0, x1, y1)

    def draw_monitor_tile(self, monitor, x0, y0, x1, y1):
        canvas = self.display_canvas
        selected = monitor.device in self.selected_monitor_devices
        width, height = x1 - x0, y1 - y0
        inset = min(7, max(2, min(width, height) * 0.035))
        sx0, sy0 = x0 + inset, y0 + inset
        sx1, sy1 = x1 - inset, y1 - inset
        border = "#5ce1a5" if selected else "#798399"
        face = "#05070b"

        canvas.create_rectangle(
            x0, y0, x1, y1,
            fill=face,
            outline=border,
            width=3 if selected else 2,
            tags=("monitor", monitor.device),
        )
        screen_width = max(1, sx1 - sx0)
        screen_height = max(1, sy1 - sy0)
        band_colors = ("#f4f4f4", "#ffdf00", "#00d7e8", "#13d45b",
                       "#ed33ce", "#f02c39", "#3155ed")
        band_width = screen_width / len(band_colors)
        preview_height = screen_height * 0.54
        for index, color in enumerate(band_colors):
            canvas.create_rectangle(
                sx0 + index * band_width,
                sy0,
                sx0 + (index + 1) * band_width + 1,
                sy0 + preview_height,
                fill=color,
                outline=color,
                tags=("monitor", monitor.device),
            )

        lower_colors = ("#253b78", "#191b20", "#643160", "#202126",
                        "#18717a", "#25262a", "#a9abb1")
        lower_top = sy0 + preview_height
        lower_height = max(1, sy1 - lower_top)
        for index, color in enumerate(lower_colors):
            canvas.create_rectangle(
                sx0 + index * band_width,
                lower_top,
                sx0 + (index + 1) * band_width + 1,
                sy1,
                fill=color,
                outline=color,
                tags=("monitor", monitor.device),
            )

        monitor_width = monitor.bounds[2] - monitor.bounds[0]
        monitor_height = monitor.bounds[3] - monitor.bounds[1]
        label = f"{monitor_width} x {monitor_height}"
        if monitor.primary:
            label += "  *"
        label_font_size = max(7, min(15, int(min(width, height) * 0.075)))
        canvas.create_text(
            (sx0 + sx1) / 2,
            (sy0 + sy1) / 2,
            text=label,
            fill="#ffffff",
            font=("Segoe UI", label_font_size, "bold"),
            tags=("monitor", monitor.device),
        )

        if height > width * 0.55:
            stand_top = y1
            stand_width = min(width * 0.24, 28)
            center_x = (x0 + x1) / 2
            stand_bottom = min(
                y1 + stand_width * 0.55,
                self.display_canvas.winfo_height() - 3,
            )
            canvas.create_polygon(
                center_x - stand_width / 2, stand_top,
                center_x + stand_width / 2, stand_top,
                center_x + stand_width * 0.7, stand_bottom,
                center_x - stand_width * 0.7, stand_bottom,
                fill="#555e70",
                outline=border,
                tags=("monitor", monitor.device),
            )
        self.monitor_items[monitor.device] = (x0, y0, x1, y1)

    def toggle_display(self, event):
        for device, (x0, y0, x1, y1) in self.monitor_items.items():
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                self.focused_device = device
                if device in self.selected_monitor_devices:
                    self.selected_monitor_devices.remove(device)
                else:
                    self.selected_monitor_devices.add(device)
                self.draw_display_map()
                self.update_details()
                break

    def update_details(self):
        monitor = next(
            (
                item for item in (self.monitors or [])
                if item.device == self.focused_device
            ),
            None,
        )
        if monitor is None:
            report = (
                "Click a display preview to select it and inspect its "
                "DDC/EDID information."
            )
        else:
            report = format_edid_report(monitor)
            if monitor.device in self.ddc_ci_cache:
                report += (
                    "\n\nLive DDC/CI capabilities:\n"
                    + self.ddc_ci_cache[monitor.device]
                )
            else:
                report += (
                    "\n\nLive DDC/CI capabilities:\n"
                    "Querying monitor capabilities..."
                )
                if monitor.device not in self.ddc_ci_pending:
                    self.ddc_ci_pending.add(monitor.device)
                    threading.Thread(
                        target=self.fetch_ddcci_capabilities,
                        args=(monitor.device,),
                        daemon=True,
                    ).start()
        self.details_text.configure(state="normal")
        self.details_text.delete("1.0", tk.END)
        self.details_text.insert("1.0", report)
        self.details_text.configure(state="disabled")

    def fetch_ddcci_capabilities(self, device):
        try:
            result = query_ddcci_capabilities(device)
        except (OSError, RuntimeError) as error:
            result = f"DDC/CI query failed: {error}"
        self.ddc_ci_results.put((device, result))

    def poll_ddcci_results(self):
        if not self.root.winfo_exists():
            return
        while True:
            try:
                device, result = self.ddc_ci_results.get_nowait()
            except queue.Empty:
                break
            self.ddc_ci_pending.discard(device)
            self.ddc_ci_cache[device] = result
            if device == self.focused_device:
                self.update_details()
        self.poll_hardware_rescan_results()
        self.root.after(100, self.poll_ddcci_results)

    def request_hardware_rescan(self):
        if self.rescan_pending:
            return
        self.rescan_pending = True
        self.status.set(
            "Requesting a Windows Plug and Play hardware rescan..."
        )
        threading.Thread(
            target=self.run_hardware_rescan,
            daemon=True,
        ).start()

    def run_hardware_rescan(self):
        try:
            request_windows_pnp_rescan()
            result = (
                True,
                "Windows accepted the hardware rescan. Display and EDID data "
                "will refresh automatically.",
            )
        except (OSError, RuntimeError) as error:
            result = (False, f"Hardware rescan failed: {error}")
        self.rescan_results.put(result)

    def poll_hardware_rescan_results(self):
        while True:
            try:
                succeeded, message = self.rescan_results.get_nowait()
            except queue.Empty:
                break
            self.rescan_pending = False
            self.refresh_displays()
            self.rescan_message = (
                message
                if succeeded
                else f"{message} Automatic display refresh is still active."
            )
            self.refresh_displays()

    def poll_displays(self):
        if not self.root.winfo_exists():
            return
        self.refresh_displays()
        self.root.after(2000, self.poll_displays)

    def select_all(self):
        self.selected_monitor_devices = {
            monitor.device for monitor in (self.monitors or [])
        }
        if self.selected_monitor_devices and self.focused_device is None:
            self.focused_device = next(iter(self.selected_monitor_devices))
        self.draw_display_map()
        self.update_details()

    def clear_selection(self):
        self.selected_monitor_devices.clear()
        self.draw_display_map()
        self.update_details()

    def start_tests(self):
        self.refresh_displays()
        self.active_devices = self.selected_devices()
        if not self.active_devices:
            messagebox.showwarning(
                "No display selected",
                "Select at least one detected display before starting.",
                parent=self.root,
            )
            return

        self.pattern_index = 0
        self.root.withdraw()
        self.sync_test_windows()

    def sync_test_windows(self):
        monitors_by_device = {monitor.device: monitor for monitor in self.monitors}

        for device in self.active_devices - monitors_by_device.keys():
            window = self.test_toplevels.pop(device, None)
            self.test_windows.pop(device, None)
            if window is not None:
                window.destroy()

        for device in self.active_devices & monitors_by_device.keys():
            monitor = monitors_by_device[device]
            pattern_window = self.test_windows.get(device)
            if pattern_window is not None:
                pattern_window.move_to(monitor.bounds)
                continue

            window = tk.Toplevel(self.root)
            pattern_window = TestPatternWindow(
                window, monitor.bounds, self.advance_pattern
            )
            pattern_window.set_pattern(self.pattern_index)
            self.test_windows[device] = pattern_window
            self.test_toplevels[device] = window

    def advance_pattern(self, _event=None):
        if not self.test_windows:
            return
        self.pattern_index = (self.pattern_index + 1) % len(PATTERN_NAMES)
        for pattern_window in self.test_windows.values():
            pattern_window.set_pattern(self.pattern_index)

    def on_space(self, _event):
        if self.test_windows:
            self.advance_pattern()
            return "break"

    def on_escape(self, _event):
        if not self.active_devices:
            self.root.destroy()
            return "break"

        for window in self.test_toplevels.values():
            window.destroy()
        self.test_toplevels.clear()
        self.test_windows.clear()
        self.active_devices.clear()
        self.refresh_displays()
        self.root.deiconify()
        return "break"


def main():
    if sys.platform == "win32":
        ctypes.windll.user32.SetProcessDPIAware()
    root = tk.Tk()
    MonitorSelector(root)
    root.mainloop()


if __name__ == "__main__":
    main()
