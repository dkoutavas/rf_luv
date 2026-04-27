#!/usr/bin/env bash
# Software reset of the entire USB host controller on leap (PCI 0000:00:14.0,
# xhci_hcd). Use as a "step 2" recovery when per-device rtl-usb-reset can't
# unwedge a dongle that streams no samples.
#
# Affects every USB device on bus 02 (currently: the RTL-SDR dongles + the
# internal Bluetooth radio). Network is on PCIe Ethernet, so SSH/Tailscale stay
# up throughout. Takes ~10s of total disruption.
#
# Run from any host that can reach leap over Tailscale:
#     bash ops/rtl-tcp/usb-controller-reset.sh
#
# Asks for sudo password on leap (only rtl-usb-reset is NOPASSWD; full
# controller unbind/bind isn't, by design).

set -euo pipefail

PCI_DEV="0000:00:14.0"
DRV="/sys/bus/pci/drivers/xhci_hcd"

echo "==> resetting USB controller $PCI_DEV (xhci_hcd) on leap"
ssh -t scanner "sudo bash -c '
    set -e
    echo \"-- unbind\"
    echo $PCI_DEV > $DRV/unbind
    sleep 3
    echo \"-- bind\"
    echo $PCI_DEV > $DRV/bind
    sleep 5
    echo \"-- lsusb (rtl)\"
    lsusb | grep -i rtl || echo \"(no RTL dongles found)\"
    echo \"-- enumerated serials\"
    for d in /sys/bus/usb/devices/*/serial; do
        s=\$(cat \$d 2>/dev/null) || continue
        v=\$(cat \$(dirname \$d)/idVendor 2>/dev/null) || continue
        p=\$(cat \$(dirname \$d)/idProduct 2>/dev/null) || continue
        [ \"\$v\" = \"0bda\" ] && [ \"\$p\" = \"2838\" ] && echo \"  \$d -> \$s\"
    done
'"
echo "==> done"
