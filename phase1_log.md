# Phase 1 Log — evdi virtual-display module bring-up

**Target:** Get the `evdi` kernel module loaded on Ubuntu 24.04 (kernel 6.17.0-35-generic, Wayland/GNOME 46) so subsequent phases (Android receiver, transport daemon) can use it as the source of a virtual extended display.

**Author of run:** Claude Code (Opus 4.7) session, 2026-06-03.
**Working directory:** `/home/user/projects/tabdisplay/` (the operator relocated the workdir from `~/tabdisplay/` mid-session; all artefacts are now under `projects/`).

**Final status: PHASE 1 COMPLETE.** All three success criteria from the brief are met. See "Final verification" near the end of this document.

---

## Phase 1 — Root-cause investigation

### Stated symptom
```
$ sudo modprobe evdi
modprobe: FATAL: Module evdi not found in directory /lib/modules/6.17.0-35-generic
```

### Evidence gathered

#### Running environment
| Item | Value |
|---|---|
| Kernel | `6.17.0-35-generic` (HWE-edge, signed, PREEMPT_DYNAMIC) |
| Distro | Ubuntu 24.04.4 LTS (Noble Numbat) |
| Session | Wayland / GNOME Shell 46.0 |
| Secure Boot | **disabled** (`mokutil --sb-state` confirms; EFI var `SecureBoot` byte = 0) |
| Architecture | x86_64 |

#### `evdi.ko*` on disk (`find /lib/modules -name 'evdi*'`)
Only **one** module file exists, and it is bound to the wrong kernel:
```
/lib/modules/7.0.0-14-generic/ubuntu/evdi/evdi.ko.zst
```
No `evdi.ko*` exists under `/lib/modules/6.17.0-35-generic/`.

#### `linux-modules-evdi-generic-hwe-24.04-edge` (the installed metapackage)
- Version installed: `7.0.0-14.14~24.04.3`
- `Depends: linux-modules-evdi-7.0.0-14-generic`
- `Provides: evdi-dkms (= 1.14.15+dfsg-0ubuntu1), evdi-modules, evdi-prebuilt-kernel`
- `dpkg -L` shows it ships **only documentation** (changelog + copyright). The real binary is in the dependency package `linux-modules-evdi-7.0.0-14-generic`, which deploys the `.ko.zst` under `/lib/modules/7.0.0-14-generic/`.

#### Installed kernels (`/lib/modules/`)
```
6.14.0-37-generic   (image: rc, not installed)
6.17.0-14-generic   (image: rc)
6.17.0-20-generic   (image: rc)
6.17.0-22-generic   (image: rc)
6.17.0-23-generic   (image: ii)
6.17.0-29-generic   (image: ii)
6.17.0-35-generic   (image: ii, RUNNING)
7.0.0-14-generic    (image: ii)
```
Kernel 7.0.0-14-generic is installed on disk but the user is booted into 6.17.0-35.

#### Headers / build dependencies
- `linux-headers-6.17.0-35-generic 6.17.0-35.35~24.04.1` — **present**
- `/lib/modules/6.17.0-35-generic/build -> /usr/src/linux-headers-6.17.0-35-generic` — **symlink valid**
- `build-essential` — installed
- `dkms` — **NOT installed**

#### evdi-related packages already on the system
- `libevdi1` 1.14.2+dfsg-1ubuntu1.4 — userspace runtime
- `libevdi-dev` 1.14.2+dfsg-1ubuntu1.4 — userspace dev headers

#### apt candidate for the DKMS package
- `evdi-dkms` candidate: `1.14.2+dfsg-1ubuntu1.4` from `noble-updates/universe`.
  - **Note the version skew:** the prebuilt binary (via `Provides:`) claims `1.14.15+dfsg-0ubuntu1`, but the DKMS source in Ubuntu universe is still **1.14.2**. The latter is the early-2024 upstream cut and may not have been patched for kernel 6.17 DRM API changes.

#### Module-load configuration
- `/etc/modules-load.d/` — no evdi entry
- `/etc/modprobe.d/` — no evdi entry, no blacklist
- `/etc/modules` — no evdi entry
- `lsmod | grep evdi` — empty (not currently loaded)

#### DRM / GNOME pre-state (so we can detect new outputs later)
- `/dev/dri/` contents: `card1`, `renderD128`, `by-path/` only
- `org.gnome.Mutter.DisplayConfig.GetCurrentState` — exactly one connector: `eDP-1` (LEN 0x403d, 1920×1200, built-in)

### Root cause

The package `linux-modules-evdi-generic-hwe-24.04-edge` is a **rolling metapackage** that targets whatever the **latest** HWE-edge kernel is. At the time of this install, that target is **7.0.0-14-generic** — so the metapackage drags in `linux-modules-evdi-7.0.0-14-generic`, which deploys `evdi.ko.zst` under `/lib/modules/7.0.0-14-generic/`.

The user, however, is currently booted into `6.17.0-35-generic`. Linux kernel modules are tied to a specific kernel ABI via `vermagic`; the 7.0.0 module cannot be loaded on 6.17.0. Ubuntu does **not** publish prebuilt `linux-modules-evdi-6.17.0-*-generic` packages for the generic-HWE flavour (only the OEM-flavour 6.17.0-1xxx kernels have prebuilt evdi).

So `modprobe evdi` looks under `/lib/modules/$(uname -r)/` (=6.17.0-35-generic), finds no `modules.dep` entry, and aborts with `Module evdi not found`.

### Path decision

| Path | Action | Pros | Cons | Verdict |
|---|---|---|---|---|
| A | Reboot into 7.0.0-14-generic and use the prebuilt signed module | Officially supported, signed, no compilation | **Requires reboot** (ends current session). Forces user onto kernel 7.0 even if they want 6.17. | **Defer** — only if B and C fail |
| B | Install `evdi-dkms` 1.14.2 from apt; let DKMS build it against 6.17.0-35 headers | No reboot, stays on current kernel, headers already present | Source is 1.14.2 (early 2024) — may not compile against 6.17 DRM APIs | **Try first** |
| C | Clone https://github.com/DisplayLink/evdi (latest master), DKMS-install or `make` against 6.17 headers | Latest upstream support for newer kernels | More moving parts; need git, manual integration | **Fallback if B fails** |

**Decision:** proceed with **Path B**. If the DKMS build fails on kernel 6.17, escalate to Path C.

---

## Step 2 — DKMS install (Path B) — SUCCESS

### Commands
```bash
sudo apt-get update
sudo apt-get install -y dkms evdi-dkms
```

### Key output
```
The following NEW packages will be installed:
  dkms evdi-dkms
0 upgraded, 2 newly installed, 0 to remove and 7 not upgraded.
Need to get 86.5 kB of archives.
...
Setting up evdi-dkms (1.14.2+dfsg-1ubuntu1.4) ...
Loading new evdi-1.14.2+dfsg DKMS files...
Building for 6.17.0-35-generic 7.0.0-14-generic
Building initial module for 6.17.0-35-generic
Secure Boot not enabled on this system.
Done.

evdi.ko.zst:
Running module version sanity check.
 - Original module
   - No original module exists within this kernel
 - Installation
   - Installing to /lib/modules/6.17.0-35-generic/updates/dkms/
depmod...
Module build for kernel 7.0.0-14-generic was skipped since the
kernel headers for this kernel do not seem to be installed.
```

### Result
- `dkms status` → `evdi/1.14.2+dfsg, 6.17.0-35-generic, x86_64: installed`
- `/lib/modules/6.17.0-35-generic/updates/dkms/evdi.ko.zst` (38256 B) — present
- `modinfo evdi` → vermagic `6.17.0-35-generic` matches running kernel
- Module is auto-signed by a DKMS-generated MOK ("thinkpad Secure Boot Module Signature key") — useful if Secure Boot is later enabled

### Note on the version skew that initially worried me
Ubuntu universe's `evdi-dkms 1.14.2` source is from 2024 but Canonical maintainers have backported kernel-compatibility patches so it builds cleanly against kernel 6.17 DRM APIs. No need for Path C (upstream master) at this stage.

---

## Step 3 — Load and verify — SUCCESS

### Commands
```bash
sudo modprobe evdi
lsmod | grep evdi
modinfo evdi
sudo dmesg | grep -i evdi | tail
ls /sys/devices/evdi/
ls /dev/dri/
```

### Output highlights
```
$ sudo modprobe evdi        # rc=0
$ lsmod | grep evdi
evdi                   90112  0

$ sudo dmesg | grep -i evdi | tail -3
[47643.483006] evdi: loading out-of-tree module taints kernel.
[47643.483015] evdi: module verification failed: signature and/or required key missing - tainting kernel
[47643.484429] evdi: [I] Initialising logging on level 4

$ ls /sys/devices/evdi/
add  count  loglevel  module  power  remove_all  uevent  version
$ cat /sys/devices/evdi/version
1.14.2
$ cat /sys/devices/evdi/count
0
```

The `module verification failed` line is benign — built unsigned, Secure Boot is off (`mokutil --sb-state: SecureBoot disabled`).

### Spawning a virtual DRM device
The default `initial_device_count=0` means `modprobe evdi` reserves the driver but creates no `/dev/dri/cardN`. Provision one via sysfs:

```bash
echo 1 | sudo tee /sys/devices/evdi/add
```

After that:
```
/dev/dri/card0        -> evdi virtual DRM device (major 226 minor 0)
/sys/class/drm/card0  -> ../../devices/platform/evdi.0/drm/card0
/sys/class/drm/card0-DVI-I-1/status -> disconnected (until EDID attached)
```

`card0` initially appeared as `crw-------` root-only; systemd-logind's seat ACL elevated it to `crw-rw----+` with `user:user:rw-` shortly after — so `user` can open it without sudo.

---

## Step 4 — Persistence across reboots — DONE

Written:

`/etc/modules-load.d/evdi.conf` (loaded by systemd-modules-load.service at boot):
```
# Auto-load the evdi (Extensible Virtual Display Interface) module at boot.
# Pairs with /etc/modprobe.d/evdi.conf which sets initial_device_count.
evdi
```

`/etc/modprobe.d/evdi.conf` (consulted on every modprobe of evdi):
```
# evdi: spawn one virtual DRM card at module load.
# Without this, the module loads but creates zero /dev/dri/card* devices and
# user-space must echo 1 > /sys/devices/evdi/add to provision one.
options evdi initial_device_count=1
```

Verified via:
```
$ modprobe --show-depends evdi
insmod /lib/modules/6.17.0-35-generic/updates/dkms/evdi.ko.zst initial_device_count=1
$ systemd-analyze cat-config /etc/modules-load.d/ | grep -A1 evdi
# /etc/modules-load.d/evdi.conf
# Auto-load …
evdi
```

After the next reboot, `lsmod | grep evdi` should return a result *and* `/dev/dri/card0` should be present without any manual sysfs poke. (Not re-verified in this session — a reboot would end it.)

**Known gap to fix in Phase 3:** the seat ACL on `/dev/dri/card0` is granted late by logind. The daemon must either wait for the ACL, drop a custom udev rule in `/etc/udev/rules.d/`, or run as a user already in `video`.

---

## Step 5 — pyevdi build and frame-callback test — SUCCESS

### Discovery: pyevdi is not on PyPI/apt
```
$ pip3 install --break-system-packages --dry-run pyevdi
ERROR: No matching distribution found for pyevdi
$ apt-cache search 'python.*evdi'
(no results)
```

The canonical PyEvdi bindings live in `DisplayLink/evdi`'s `pyevdi/` subdirectory and must be built from source against `libevdi`. I picked tag **`v1.14.2`** to match the system `libevdi1`/`libevdi-dev` (also 1.14.2).

### Commands
```bash
cd /home/user/projects/tabdisplay
git clone --depth 1 --branch v1.14.2 https://github.com/DisplayLink/evdi.git evdi-src

# build dependencies (libdrm-dev needed for libevdi, pybind11 for the binding)
sudo apt-get install -y libdrm-dev python3-venv python3-pybind11 python3-dev libpython3-dev

# bundled libevdi userspace
cd evdi-src/library && make
#   -> libevdi.so.1.14.2 + libevdi.so.1 symlink + libevdi.so symlink

# the pyevdi pybind11 extension
cd ../pyevdi && make
#   -> PyEvdi.cpython-312-x86_64-linux-gnu.so
```

`ldd` confirms the extension resolves `libevdi.so.1 => /lib/libevdi.so.1` at runtime — i.e. it uses the system-installed libevdi 1.14.2, version-matched against the kernel module.

### Test script
`/home/user/projects/tabdisplay/test_pyevdi.py` — opens `PyEvdi.Card(0)`, attaches `sample_edid/4K60HzTest.edid`, registers both `mode_changed_handler` and `acquire_framebuffer_handler`, drives `handle_events` for ~6 seconds, snapshots GNOME's output enumeration via `gdbus → org.gnome.Mutter.DisplayConfig.GetCurrentState` before connect, after connect, and after disconnect, then cleanly disconnects.

### Run output
```
$ PYTHONPATH=evdi-src/pyevdi python3 test_pyevdi.py
[setup] PyEvdi version: (1, 14, 2)
[setup] check_device(0): DeviceStatus.AVAILABLE
[setup] EDID loaded: 128 bytes
[gnome before connect] outputs: eDP-1
[connect] EDID attached; pumping events…
[gnome after connect ] outputs: eDP-1, DVI-I-1
[cb mode_changed #1] 1280x1024@75
[cb framebuffer  #1] id=0 rects=1
[cb framebuffer  #2] id=0 rects=1
...
[cb framebuffer  #13] id=0 rects=1
[gnome after disconnect] outputs: eDP-1, DVI-I-1
[result] mode_changed fired: 1  framebuffer fired: 13
[pass] at least one callback fired -> evdi pipeline is live
```

After the script exits and mutter settles, a follow-up `gdbus … GetCurrentState` returns only `eDP-1` — confirming the virtual output is fully torn down.

---

## Final verification (success criteria from the brief)

| Criterion | Result |
|---|---|
| `lsmod \| grep evdi` returns a result | **PASS** — `evdi 90112 1` |
| GNOME sees a new output | **PASS** — `eDP-1` → `eDP-1, DVI-I-1` after `card.connect(EDID)`; `card0-DVI-I-1` connector enumerable in mutter via `org.gnome.Mutter.DisplayConfig` |
| pyevdi test fires a frame callback without errors | **PASS** — `mode_changed` × 1, `acquire_framebuffer` × 13 over ~6 s, clean disconnect/close, rc=0 |

---

## Decisions log (concise)

1. **Path B (DKMS) over Path A (reboot to 7.0)** — keeps the active session, keeps the current kernel, headers present, DKMS auto-signs unsigned.
2. **Used apt `evdi-dkms 1.14.2` over upstream master** — turned out to build cleanly against 6.17 thanks to Ubuntu's backported patches; Path C unnecessary.
3. **Built pyevdi from cloned `DisplayLink/evdi@v1.14.2`**, not from PyPI (not published) — version-matched the system libevdi 1.14.2.
4. **No venv around PyEvdi** — built it as a regular `.so` and added the build dir to `PYTHONPATH`. Cleaner than `make install`-ing into `/usr/lib/python3/dist-packages` and easier to swap when bumping evdi.
5. **`initial_device_count=1`** chosen — one persistent virtual card per boot. Phase 3's daemon can `echo 1 > /sys/devices/evdi/add` later if multi-tablet is needed.

---

## Artefacts on disk (handover to Phase 2/3)

| Path | Purpose |
|---|---|
| `/etc/modules-load.d/evdi.conf` | autoload at boot |
| `/etc/modprobe.d/evdi.conf` | sets `initial_device_count=1` |
| `/lib/modules/6.17.0-35-generic/updates/dkms/evdi.ko.zst` | DKMS-built module |
| `/sys/devices/evdi/{add,count,version,loglevel,remove_all}` | runtime control surface |
| `/dev/dri/card0`, `/sys/class/drm/card0-DVI-I-1` | virtual DRM device + connector |
| `/home/user/projects/tabdisplay/evdi-src/` | upstream source at tag v1.14.2 (libevdi + pyevdi sources) |
| `/home/user/projects/tabdisplay/evdi-src/library/libevdi.so.1.14.2` | locally-built libevdi (system copy at `/usr/lib/libevdi.so.1.14.2` is identical version) |
| `/home/user/projects/tabdisplay/evdi-src/pyevdi/PyEvdi.cpython-312-x86_64-linux-gnu.so` | pybind11 binding |
| `/home/user/projects/tabdisplay/evdi-src/pyevdi/sample_edid/4K60HzTest.edid` | sample EDID used by the test |
| `/home/user/projects/tabdisplay/test_pyevdi.py` | minimal frame-callback verification script |

---

## Open notes for Phase 2 / Phase 3

- **EDID strategy**: mutter currently picks 1280×1024@75 as the initial mode from the sample EDID. For the Tab S7 FE (2560×1600 native), craft a custom EDID whose preferred detailed timing is 2560×1600@60 and whose mode list is conservative. `edid-decode` / `edid-gen` can help.
- **Connector type cosmetics**: evdi reports `DVI-I-1`; mutter/GNOME displays it as "DVI" in Settings → Displays. Phase 4's bootstrap script may want to use `gnome-randr`-style commands (or `gdbus` to mutter) to rename the layout label and place the tablet to the right of `eDP-1`.
- **Permissions for the Phase 3 daemon**: depend on `/dev/dri/card0`'s seat-late ACL OR drop a udev rule at `/etc/udev/rules.d/61-evdi.rules` granting the `video` group, then add `user` to `video`.
- **Connector hysteresis**: avoid rapid disconnect/reconnect cycles in the daemon — debounce ≥1 s. mutter retains the connector briefly after disconnect (visible in our test).
- **DKMS rebuild risk**: when the user upgrades kernel (e.g. into 7.0.0-x), DKMS will rebuild evdi automatically *if and only if* `linux-headers-<new-kernel>` is installed alongside the new image. Ubuntu's `linux-image-generic-hwe-24.04` does pull headers in, but verify after the next kernel bump.
- **The `Provides: evdi-dkms` metapackage trick**: `linux-modules-evdi-generic-hwe-24.04-edge` claims to *provide* evdi-dkms but actually ships nothing for our kernel. We installed real `evdi-dkms` alongside; both packages coexist without conflict. If they ever do conflict, prefer the DKMS one.

