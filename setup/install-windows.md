# Windows-Side Setup

## 1. USB Driver (Zadig) — DO THIS FIRST WHEN DONGLE ARRIVES

1. Download Zadig: https://zadig.akeo.ie
2. Plug in RTL-SDR Blog V3
3. Open Zadig → Options → List All Devices
4. Select **Bulk-In, Interface (Interface 0)** from the dropdown
   - It should show the current driver as `RTL2832UUSB` or similar
   - **DO NOT** select "RTL2832U" (that's the wrong interface)
5. Set the target driver to **WinUSB** (not libusb or libusbK)
6. Click **Replace Driver**
7. Wait for confirmation

> ⚠️ If you accidentally replace the wrong device's driver, Zadig can undo it.
> The RTL-SDR will no longer work as a DVB-T TV tuner after this — that's expected.

## 2. SDR++ (Primary Spectrum Analyzer)

- Download: https://github.com/AlexandreRouma/SDRPlusPlus/releases
- Get the Windows x64 release (`.zip`, not the nightly)
- Extract anywhere, run `sdrpp.exe`
- First launch:
  1. Source → RTL-SDR → select your device
  2. Set sample rate to **2.048 MHz**
  3. Gain: start at **30 dB**, adjust from there
  4. Tune to **~100 MHz** to hear FM radio (instant test)
  5. For HF/shortwave: Source → RTL-SDR → Direct Sampling → **Q-branch**

### SDR++ Tips
- **Snap to frequency**: right-click the frequency display
- **Recording**: use the Recorder module to save IQ files (`.wav` or raw IQ)
- **Noise blanker**: enable if you see pulsed interference
- **FFT settings**: bump FFT size to 65536 for finer frequency resolution (at cost of time resolution — same tradeoff as audio spectrograms)

## 3. SDR# (Secondary, Optional)

- Download: https://airspy.com/download/
- Useful for its plugin ecosystem (frequency scanner, digital signal plugins)
- Install the community plugins pack for extra decoders

## 4. usbipd-win (USB Passthrough to WSL)

Only needed if you want to run SDR tools directly in WSL:

```powershell
# PowerShell (Admin)
winget install usbipd

# After plugging in dongle:
usbipd list                              # find RTL2832U bus ID
usbipd bind --busid <BUSID>              # make available (one-time)
usbipd attach --wsl --busid <BUSID>      # attach to WSL

# In WSL, verify:
lsusb | grep RTL
# Should show: Realtek Semiconductor Corp. RTL2832U DVB-T
```

> Note: You need to re-attach after every unplug or Windows restart.
> High sample rates (>2 MS/s) may experience sample drops over usbipd.

## 5. rtl_tcp (Network Bridge — Recommended Hybrid Approach)

Instead of usbipd, you can run `rtl_tcp` on Windows to stream samples over TCP,
then connect SDR tools in WSL to it over localhost:

1. Download rtl-sdr release for Windows: https://ftp.osmocom.org/binaries/windows/rtl-sdr/
   (or grab pre-built from the RTL-SDR Blog drivers page)
2. Run:
   ```cmd
   rtl_tcp -a 127.0.0.1 -p 1234 -s 2048000
   ```
3. In WSL, tools connect to `127.0.0.1:1234`:
   ```bash
   # Example: record 30 seconds of IQ data from rtl_tcp
   rtl_sdr -d tcp:127.0.0.1:1234 -f 1090000000 -s 2048000 -n 61440000 recording.iq
   ```

This avoids all USB passthrough complexity while keeping processing in WSL.

## 6. VB-Cable (Optional — Audio Piping)

For routing demodulated audio from SDR++ to WSL decoders (e.g., multimon-ng):

1. Download VB-Cable: https://vb-audio.com/Cable/
2. In SDR++: set audio output to VB-Cable
3. In WSL: use PulseAudio integration or save to file and process

Alternatively, SDR++ can save demodulated audio to `.wav` files which you process in WSL.
