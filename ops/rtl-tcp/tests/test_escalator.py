#!/usr/bin/env python3
"""Tests for the escalator's per-dongle xHCI controller lookup.

Run: python3 -m pytest ops/rtl-tcp/tests/test_escalator.py
"""
import importlib.util
import os
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_OPS_RTL = os.path.dirname(_HERE)

# import the hyphenated script via spec_from_file_location
_spec = importlib.util.spec_from_file_location(
    "rtl_tcp_escalator",
    os.path.join(_OPS_RTL, "rtl-tcp-escalator.py"),
)
esc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(esc)

PCI_ROOT = "devices/pci0000:00/0000:00:08.1"


def _fake_sysfs(root: Path):
    """The Omen's layout: V4, webcam, Bluetooth and a hub on 07:00.3; V3 alone
    on 07:00.4."""
    devs = {
        "1-1": ("0000:07:00.3", "usb1", {"idVendor": "05e3", "bDeviceClass": "09", "product": "USB2.0 Hub"}),
        "1-2": ("0000:07:00.3", "usb1", {"idVendor": "0bda", "serial": "v4-01", "product": "Blog V4"}),
        "1-3": ("0000:07:00.3", "usb1", {"idVendor": "0408", "product": "HP Wide Vision HD Camera"}),
        "1-4": ("0000:07:00.3", "usb1", {"idVendor": "8087", "idProduct": "0029"}),
        "3-2": ("0000:07:00.4", "usb3", {"idVendor": "0bda", "serial": "v3-01"}),
    }
    (root / "bus/usb/devices").mkdir(parents=True)
    (root / "bus/pci/drivers/xhci_hcd").mkdir(parents=True)
    for pci in ("0000:07:00.3", "0000:07:00.4"):
        (root / PCI_ROOT / pci).mkdir(parents=True)
        (root / "bus/pci/drivers/xhci_hcd" / pci).symlink_to(root / PCI_ROOT / pci)
    for name, (pci, bus, attrs) in devs.items():
        d = root / PCI_ROOT / pci / bus / name
        d.mkdir(parents=True)
        for k, v in attrs.items():
            (d / k).write_text(v + "\n")
        (root / "bus/usb/devices" / name).symlink_to(d)


def test_dongle_alone_on_its_controller():
    with tempfile.TemporaryDirectory() as t:
        _fake_sysfs(Path(t))
        assert esc.xhci_for_serial("v3-01", t) == ("0000:07:00.4", [])


def test_shared_controller_lists_the_others_but_not_hubs():
    with tempfile.TemporaryDirectory() as t:
        _fake_sysfs(Path(t))
        pci, others = esc.xhci_for_serial("v4-01", t)
        assert pci == "0000:07:00.3"
        assert others == ["8087:0029", "HP Wide Vision HD Camera"]


def test_absent_dongle():
    with tempfile.TemporaryDirectory() as t:
        _fake_sysfs(Path(t))
        assert esc.xhci_for_serial("v9-99", t) is None


def test_plan_skips_shared_unless_allowed():
    orig = esc.xhci_for_serial
    try:
        esc.xhci_for_serial = lambda serial: ("0000:07:00.3", ["HP Wide Vision HD Camera"])
        cfg = dict(esc.DEFAULTS)
        plan = esc.xhci_plan(cfg, "v4-01")
        assert plan["pci"] == "0000:07:00.3" and "Camera" in plan["skip"]
        cfg["XHCI_BOUNCE_SHARED"] = "1"
        assert esc.xhci_plan(cfg, "v4-01") == {"pci": "0000:07:00.3"}
        esc.xhci_for_serial = lambda serial: None
        assert esc.xhci_plan(cfg, "v4-01") == {"skip": "dongle not on the bus"}
    finally:
        esc.xhci_for_serial = orig
