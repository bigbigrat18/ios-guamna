# Porting ios-sweet to guamna (Moto G Play 2021, SM4250)

Branch `guamna-port` of `bigbigrat18/ios-guamna`. Reference board: sweet
(Redmi Note 10 Pro, SM7150). **The GUAMNA kernel builds in CI** (run
41332581911, Xcode 16 on macos-15) and contains only ARMv8.0 instructions.
Nothing has run on a guamna yet (see *What was checked*).

## 1. What actually differs

| | sweet (SM7150) | guamna (SM4250 / "bengal") | Impact |
| --- | --- | --- | --- |
| CPU | 6x A55 + 2x A76, DynamIQ, ARMv8.2 | 4x A53 + 4x A73, big.LITTLE, **ARMv8.0** | No LSE atomics, PAN, RCpc, UAO. Compiler must target A53 |
| Page granule | 16K | **4K only** (A53 has no 16K granule: `ID_AA64MMFR0_EL1.TGran16 = 0`) | 4K-page XNU, like BCM2837 (RPi 3, also A53) |
| MPIDR | Aff1 = core (0x000-0x700) | Aff1 = cluster, Aff0 = core (0x000-0x003, 0x100-0x103) | SMP topology checks |
| GIC | GICv3 @ 0x17a00000 | GICv3 @ 0x0f200000 / GICR 0x0f300000 (live FDT) | Address only, same IRQ path |
| Timer | virt PPI 3 → INTID 19 | same (live FDT) | none |
| RAM | 6 GB, XNU uses the empty upper bank above 4G | 3 GB in two banks below 4G, carve-outs scattered | XNU gets 1.25 GB @ 0xb0000000 (largest contiguous free range) |
| Storage | UFS 2.1 | **eMMC 5.1 on SDHC1 (sdhci-msm v5)** | New driver (patch 0083) |
| Power | RPMh (TCS writes) | **RPM over GLINK** (SMD-RPM messages) | New, harder code, but not needed for first boots |
| PMIC | PM6150 + PM6150L | PM6125 + PMI632 | Keys/battery addresses |
| Display | 1080x2400 @ 0x9c000000 | 720x1600, splash address TBD | Framebuffer geometry, iPhone X canvas scaling |
| Debug UART | none (wired as SPI) | GENI SE4 @ 0x04a90000 per brief, but not enabled in the live FDT | Possible serial console later |
| Touch | Goodix GT9896, I²C | **SPI** on `spi@4a88000`; FDT lists Focaltech, Ilitek, Himax and Chipone (panel-dependent) | New driver; find the populated chip first |
| Volume up | PM6150L GPIO2 | PMIC GPIO 5 (`gpio-keys`) | new key mapping |

### Live FDT facts (LineageOS, 2026-11-01)

- DRAM banks `0x40000000-0x7e580000` and `0x80000000-0x100000000`.
  Last fixed carve-outs are Motorola's `mmi_annotate`/`tzlog`/`wdog_cpuctx`/`ramoops`
  (`0xaef9d800-0xaf0bf800`, no-map) → XNU RAM `0xb0000000` + `0x50000000`.
  More RAM later needs XNU to accept a hole (or proof that those Motorola
  regions are not XPU-protected).
- ABL put LineageOS's ramdisk at `0x45596000`, just below `hyp_region`
  (`0x45700000`). Our payload ramdisk is ~20 MB+: check where ABL places it
  (the shim prints `INITRD lo/hi`) before trusting it not to hit a carve-out.
- IMEM restart reason `0x0c12565c`, PS_HOLD `0x0440b000` (shim reset path uses both now).
- TLMM `0x00400000` (+`0xc00000`), GCC `0x01400000`, SPMI core/obsrvr/cnfg
  `0x01c40000`/`0x03e00000`/`0x01c0a000`, APCS `0x0f111000`, USB DWC3 `0x04e00000`,
  QUSB2 `0x01613000`, splash FB `0x5c000000`.
- The tree also has a UFS host (`ufshc@4804000`); guamna boots from eMMC, so ignore it.

## 2. Inventory: every SM7150 / sweet hardware reference

Status: **done** = handled on this branch, **TODO** = still sweet-only.

### tools/

| File | sweet-specific content | Status |
| --- | --- | --- |
| `tools/mkadt.py` | GIC 0x17a00000/0x17a60000, FB 0x9c000000 1080x2400, UFS 0x01d84000, RAM 0x1_00000000 (upper bank) / 0xb0000000, 8 CPUs with MPIDR `core << 8`, `qcom,kryo470`, `qcom,sm7150` | **done**: `--board guamna` → `build_guamna()` (sweet path untouched) |
| `tools/mkpayload.py` | none: RAM comes from the ADT; 16K page / 32 MB alignment are multiples of the 4K granule | works as is |
| `tools/qemu-sweet.py` | `-cpu cortex-a76`, 16K `PAGE` | TODO: a `cortex-a53` QEMU run is the cheapest test of the 4K/ARMv8.0 kernel |
| `tools/sweet-log-pull.sh` | reads the last 1025 4 KiB blocks of `userdata` | works: the eMMC driver keeps the same on-disk layout |
| `tools/sweet-root-tweaks.py`, `mkiosroot.sh` | none (userland) | as is |
| `tools/fdt-survey.py` | new | **done**: reads guamna's live FDT, prints the real addresses |

### shim/

| File | sweet-specific content | Status |
| --- | --- | --- |
| `shim/console.h`, `mmu.h`, `console.c` | FB 0x9c000000 1080x2400, font scale 3 | **done**: `BOARD=guamna` → 720x1600 @ 0x5c000000 (**unverified**), scale 2 |
| `shim/shim.c` | GICD/GICR, IMEM restart reason 0x146aa65c, PS_HOLD 0x0c264000, UFS probe | **done**: guamna GIC; reset uses PSCI only (IMEM/PS_HOLD TODO); a first-boot probe screen replaces the UFS probe |
| `shim/mmu.c` | 16K granule, 32 MB blocks | TODO (diagnostic path only, unreachable after `reset()`). Must become 4K before it is ever called on guamna |
| `shim/ufs.c` | UFS host @ 0x01d84000 | not called for guamna |
| `shim/loader.c` | `PAGE_16K` alignment of ADT/boot_args | fine (16K-aligned is 4K-aligned) |
| `shim/build.sh` | stock DTB path, mkbootimg header v2 offsets | `BOARD=guamna` adds `-DBOARD_GUAMNA -mcpu=cortex-a53`. Check the offsets against `unpack_bootimg` of your LineageOS `boot.img` |

### xnu/patches/ (hardware-relevant ones)

| Patch | What is SM7150-specific | Status on guamna |
| --- | --- | --- |
| 0004 machine config | `-march=armv8.2-a+crypto`, `__ARM_16K_PG__`, `__ARM_PAN_AVAILABLE__`, `SWEET.h` GIC/timer | **0082**: new `GUAMNA` config + `GUAMNA.h` |
| 0006/0007/0008/0037/0057 `MASTER.arm64.sweet` | doconf picks `MASTER.arm64.<machine>` | **0082**: `MASTER.arm64.guamna` (copy, keep in sync) |
| 0010/0012 early framebuffer | FB literal 0x9c000000, 1080x2400 stride 4320, **16K-granule bootstrap table layout** in `start.s` and `arm_vm_init.c` | **0082**: compiled out (`SWEET_EARLY_FB`). Console starts at `serial_init` from `/sweet-fbcon`. Porting it to 4K tables is TODO |
| 0017 timer PPI | INTID 19 | same value, from `GUAMNA.h` |
| 0019/0020/0026 staging scripts | run only for `MACHINE_CONFIG == SWEET` | **done**: also GUAMNA |
| 0028/0030/0034/0047/0069 UFS | `bsd/dev/sweet_ufs.c`: UFS host + root + persistent log + calls the input/USB drivers | **0082** compiles it out; **0083** replaces it with `guamna_emmc.c` |
| 0060/0063 keys + touch | SPMI @ 0x0c440000/0x0e600000/0x0c40a000, Goodix GT9896 on QUP SE7 @ 0x00a84000, TLMM north 0x03900000 | compiled out. TODO: guamna touch IC/bus from fdt-survey, PM6125 PON |
| 0062 RPMh + battery | RPMh RSC @ 0x18220000, cmd-db @ 0x0c3f000c, PM6150 fuel gauge | rpmh compiled out; SweetBattery falls back to static 100 % (`sweet_spmi_*` stubs). TODO: RPM (section 5) |
| 0065/0078 SMP | MPIDR Aff1 = core, `> 0x700` checks | **0082**: guamna MPIDR check (Aff1 = cluster). The SGI path already sends Aff1 |
| 0072-0077/0081 USB | DWC3 @ 0x0a600000, QUSB2 @ 0x088e2000, QSCRATCH, GCC @ 0x00100000 | compiled out. TODO: bengal USB addresses |
| 0046/0061/0079 display | 1125x2436 canvas **cropped** to 1080x2400 | TODO: on 720x1600 the crop shows the middle 720 px of a 1125 px UI. Needs a downscale (or present 750x1334 and accept the iPhone X userland mismatch) |
| 0059 identity, 0016 PE | `xiaomi,sweet` compatible | kept in guamna's root `compatible`, so `SweetPlatformExpert` still matches |

Everything else (crypto, HFS, AMFI, pthread, HID, IOSurface, crash logging…) is
board-independent. It is gated by `ARM64_BOARD_CONFIG_SWEET`, which the GUAMNA
config also defines.

### Interrupt controller, memory map, power

- **GIC:** same GICv3 programming model (Group 1 NS IRQs, TrustZone owns Group 0),
  so `sleh_irq`/`pe_init_fiq` from 0004 carry over unchanged. Only the base
  addresses differ, and they come from the ADT.
- **Memory map:** sweet hides from every carve-out by using the upper DDR bank.
  guamna has no such bank, so `/chosen dram-base/dram-size` must be one gap
  between the fixed `reserved-memory` regions. `fdt-survey.py` computes it. Keep
  the boot image's ramdisk (where the XNU payload sits) outside that range: the
  loader refuses an overlap.
- **Power (RPMh vs RPM):** section 5.

## 3. Storage: UFS → eMMC

### Where UFS is wired in

1. `bsd/conf/files`: `bsd/dev/sweet_ufs.c standard` (0028).
2. `iokit/bsddev/IOKitBSDInit.cpp`: `rd=ufsroot0` → `sweet_ufs_rootdev()` (0028).
3. `bsd/dev/sweet_ufs.c` is more than a UFS driver. It also:
   - owns the persistent console log `sweet_log_append()` (0047), called from
     `osfmk/console/serial_console.c` for every console byte,
   - exports `sweet_log_copy/position()` for the USB live log (0077),
   - is the init hook that starts `sweet_sample_start()`, `sweet_keys_init()`,
     `sweet_touch_init()` and `sweet_usb_probe()` once root storage is up (0054/0060/0072).
4. ADT node `/sweet-ufs` (mkadt.py) gives the host's physical address.
5. Boot-args `rd=ufsroot0 ufsroot= ufsrw= ufscache= ufswait=`.

### What replaces it (patch 0083, `bsd/dev/guamna_emmc.c`)

The same strategy as sweet's UFS driver: ABL already brought the eMMC up
(clocks, 8-bit HS200/HS400, tuning, card selected) to load our boot image, so
the driver **reuses that state and never touches clocks, power or
HOST_CONTROL2**. On sdhci-msm, writes to those raise a power-IRQ handshake that
only a full driver can answer.

| Step | How |
| --- | --- |
| Find the card | CMD13 with the RCA from `emmcrca=`, then 2, 1, 3 (LK-derived Qualcomm bootloaders use 2). CMD7 if the card is in standby |
| Sanity | Refuses if ABL left the CQHCI engine on. EXT_CSD (CMD8): turns `CMDQ_MODE_EN` off and switches `PARTITION_CONFIG` to the user area if needed (CMD6), reads capacity and cache state |
| I/O | Polled CMD18 / CMD25 with Auto-CMD12, ADMA2 (32-bit descriptors; 64-bit if ABL enabled v4 64-bit mode), 1 MiB bounce buffer, cache clean/invalidate around DMA |
| Partition | GPT by name (`emmcroot=`, default `userdata`), 512-byte sectors |
| BSD devices | `emmcroot0` / `remmcroot0`, same ioctls as sweet's |
| Writes | only with `emmcrw=1`, only inside the partition; `DKIOCSYNCHRONIZE` → CMD6 FLUSH_CACHE if the cache is on |
| Log | same `sweet_log_append` ring and same on-disk spot/format, so `sweet-log-pull.sh` works |

Boot-args: `rd=emmcroot0 emmcrw=1 rootrw=1 [emmcroot=userdata] [emmcrca=N] [emmclogsecs=N]`.

Open risks:
- If ABL leaves the card in HS400 with **command queueing enabled on the host**,
  the driver stops (EBUSY) rather than guessing. Disabling CQE properly needs
  the CQHCI halt sequence.
- **ICE** (inline crypto) on SDHC1 must be in bypass for plain writes. It is by
  default, but a FBE-enabled Android may leave keys configured. Watch for
  garbage reads.
- Polled I/O spins on `delay(1)`. Fine for bring-up; the sleep/IRQ path is TODO
  (sweet's `ufswait` shows how).
- Flashing the root: `fastboot flash userdata ios16-root.simg` works the same,
  but guamna's `userdata` is smaller (32 GB device). Size the HFS image
  (`mkiosroot.sh … 16G`) to fit with the 4 MiB log at the end.

## 4. Kernel build flags (SM4250)

| Setting | sweet | guamna | Why |
| --- | --- | --- | --- |
| `MACHINE_CONFIG` | `SWEET` | `GUAMNA` | new config (0082) |
| Board macros | `-DARM64_BOARD_CONFIG_SWEET` | `-DARM64_BOARD_CONFIG_SWEET -DARM64_BOARD_CONFIG_GUAMNA` | reuse all board-independent SWEET patches |
| CPU target | `-march=armv8.2-a+crypto` | **`-mcpu=cortex-a53`** | Apple clang keeps `target-cpu apple-m1` for a macOS-SDK arm64 build unless `-mcpu` is given, and `-march` alone may not stop LSE/RCpc codegen. `cortex-a53` = ARMv8.0 + CRC + crypto, also valid on the A73 |
| Arch slice | `arm64` (not `arm64e`) | `arm64` | no PAC on A53/A73 |
| `__ARM_16K_PG__` | 1 | **undefined** → 4K pages, `T0SZ/T1SZ = 25` (39-bit VA) | A53 has no 16K granule |
| `__ARM_PAN_AVAILABLE__` | 1 | **undefined** | PAN is ARMv8.1 |
| `__ARM_ARCH_8_2__`… | undefined | undefined | keeps `machine_routines_asm.s` off its `casa` path |
| commpage PFZ | `casa` | **LDAXR/STXR loop** when `!__ARM_FEATURE_ATOMICS` (0082) | `casa` is ARMv8.1; with `-mcpu=cortex-a53` it would not even assemble |
| `__ARM64_PMAP_SUBPAGE_L1__` | — | not set | full-page root table, 39-bit user VA |
| `MAX_CPUS` / clusters | 8 / 1 | 8 / 1 | one SMP cluster; `cpus=1` first |
| Early FB console | on | off (`SWEET_EARLY_FB` undefined) | its bootstrap mapping assumes 16K tables |
| Config file | `MASTER.arm64.sweet` | `MASTER.arm64.guamna` | doconf uses the lower-case machine name |

Side note for sweet: if the `apple-m1` default really applies, the sweet kernel
already contains v8.1+ instructions. That is harmless on A55/A76 but worth
knowing. The CI guard below can be pointed at it.

## 5. Power: RPMh (sweet) vs RPM (guamna)

sweet's `sweet_rpmh.c` writes a TCS in the apps RSC: a few MMIO writes per
vote. bengal has no RSC. Regulator and clock votes are **SMD-RPM messages over
GLINK** in RPM message RAM, signalled through an APCS IPC register bit:

1. GLINK RPM transport: TX/RX FIFOs described by a table of contents at the
   end of RPM message RAM, version/feature negotiation, then an intent on the
   `rpm_requests` channel.
2. SMD-RPM request: `{service 'req', msg id, resource type ('ldoa', 'smpa', 'clk0'…), id, KVP 'uv'/'en'}`.
3. Kick: write the APCS IPC bit, poll the RX FIFO for the ack.

That is a real driver (Linux: `qcom_glink_rpm.c` + `smd-rpm.c` +
`qcom_smd-regulator.c`). **It is not needed for the first boots**: ABL leaves
the display and eMMC rails on, and keys come over SPMI, not RPM. Write it when
a peripheral turns out to be unpowered (most likely the touch controller). The
ADT has placeholders `/guamna-rpm-msg-ram` and `/guamna-apcs-ipc` for it.

## 6. CI

Runner: `macos-15` (Xcode 16) by default. GitHub retired `macos-14` (Xcode 15)
on 2026-11-02. Patch **0084** backports `IORPC.kernelContent` so that Xcode 16's
`iig` output compiles against xnu-8796; SWEET builds with it too.

GUAMNA-specific build fixes found by CI:
- **0085**: a macOS-platform kernel's user VA limit (`0x7FFFFE000000`) does not
  fit 4K pages (39-bit user VA, commpage at `0xFFFFFC000`): use the iOS
  `0xFC0000000`.
- **0086**: the macOS large-memory kernel layout (kernel at -2TB) is outside
  the 39-bit TTBR1 range of a 4K-page kernel, so the kernel faulted the moment
  it enabled the MMU (first phone boot: stuck on the shim's "XNU jumping"
  screen). GUAMNA uses the iOS layout (kernel at `0xfffffff007004000`).
- **0087**: with the iOS layout the 128 GB macOS zone-map claim no longer fits
  (second phone boot: XNU console up, then `kmem_range_init: vm_map_locate_space
  failing for claim zones`). GUAMNA uses the iOS 24 GB / 8 GB zone sizes.
- Not a bug: with >1 GB RAM XNU advertises **16 KB VM pages over 4 KB hardware
  pages** (arm_init.c, `PAGE_RATIO` 4), as Apple did on 4K-page SoCs. The
  phone's `vm_page_bootstrap: 79158 free pages` is 79158 × 16 KB = 1.24 GB, and
  iOS userland sees the 16 KB page size it was built for.
- **0088**: the randomized commpage text page is placed at a random 4 KB
  leaf, which is not a 16 KB VM page boundary 3 times out of 4 (third phone
  boot: `write_pte_fast: PTE write is unaligned` from `pmap_create_commpages`,
  backtrace symbolized against the CI kernel). GUAMNA rounds the leaf index
  to PAGE_RATIO. Before that the boot sometimes just stopped silently after
  `SWEET FB: AppleCLCD`.
- **libfirehose_kernel** is built by `xcodebuild` for `arm64-apple-macos`, so
  Apple clang's default `apple-m1` CPU put 12 LSE/LDAPR instructions into the
  kernel. `.github/scripts/guamna-firehose-cpu.py` gives its arm64 slice
  `-mcpu=cortex-a53` through its xcconfig (the command line cannot take
  `[arch=arm64]` settings).

`.github/workflows/xnu.yml` now has a matrix:
- push to `main` (patches or workflow changed): builds SWEET **and** GUAMNA.
  That doubles the macOS minutes.
- `workflow_dispatch`: `machine_config` = `SWEET` | `GUAMNA` | `ALL`.
- Artifacts: `xnu-kernel` / `xnu-buildtree` (sweet, unchanged names),
  `xnu-kernel-guamna` / `xnu-buildtree-guamna`.
- **ARMv8.0 instruction guard** (GUAMNA only): `llvm-objdump` of every code
  section (the kernel's code is in `__TEXT_EXEC`, which `otool -t` skips),
  decoding v8.1+ encodings. Fails on any LSE, RCpc, STLUR or PAN instruction
  (UNDEFINED on the phone), and on a suspiciously small disassembly.
  Current kernel: 2,281,260 instructions, 0 not ARMv8.0.

```sh
gh workflow run xnu.yml -R YOU/ios-sweet -f machine_config=GUAMNA
gh run download RUN_ID -R YOU/ios-sweet -n xnu-kernel-guamna -D xnu/out
```

### Shim in CI (`.github/workflows/shim.yml`)

Builds `shim-boot.img` on Ubuntu. Only the DTB and header geometry of a guamna
`boot.img` are used. By default (`boot_source=lineage`) that is the newest
official LineageOS guamna `boot.img` from download.lineageos.org, checked
against its SHA-256: header v2, 4K pages, kernel/ramdisk/dtb at
`0x8000`/`0x1000000`/`0x1f00000`. With `boot_source=draft`, attach your own image
to a draft release titled `guamna-boot` instead (never commit boot images).

1. Actions → **shim** → Run workflow. `payload=none` gives the probe screen;
   `payload=kernel` packs the latest `xnu-kernel-guamna` build with
   `mkadt --board guamna`.
2. Download the `shim-boot-guamna-*` artifact, then `fastboot boot shim-boot-guamna-none.img`.

**Gotcha (found 2026-11-01):** Motorola's ABL accepts the image (`Booting OKAY`
after its 10 s unlocked-bootloader warning), then **software-reboots about 2 s
later if the boot image header cmdline is just `console=null`**. Bisected with
hybrid images: our shim kernel + empty ramdisk + LineageOS's header cmdline
boots fine. The workflow therefore copies the stock image's header cmdline
(`androidboot.hardware=qcom … earlycon=msm_geni_serial,0x4a90000 …`).

## 7. Bring-up order

1. **Live FDT**: `adb root && adb pull /sys/firmware/fdt guamna-live.dtb`, then
   `tools/fdt-survey.py guamna-live.dtb`. Put the values into `GUAMNA` in
   `tools/mkadt.py` and `shim/console.h`/`shim.c`. Keep the dump out of git.
2. **Shim alone** (no payload): unpack your LineageOS `boot.img` with
   `tools/unpack_bootv2.py` into `dtb/boot_unpacked`, run `BOARD=guamna shim/build.sh`,
   then `fastboot boot shim/out/shim-boot.img`. The probe screen must show
   `tg16=0`, `atom=0`, `pan=0`. If the screen stays black, the FB address or
   stride is wrong.
3. **Kernel CI**: `machine_config=GUAMNA`. Fix compile errors (0082/0083 have not
   been through a macOS build yet), then make sure the ISA guard passes.
4. **QEMU** with `-cpu cortex-a53` (adapt `tools/qemu-sweet.py`): validates the
   4K-page kernel and LL/SC paths without the phone.
5. **RAM-disk root on the phone** (`mkpayload.py --ramdisk`, `rd=md0 cpus=1`): GIC,
   timer, console, scheduler.
6. **eMMC read-only** (`rd=emmcroot0`, no `emmcrw`), then read-write and the log.
7. SMP (`cpus=4` = A53s, then 8), keys (PM6125 PON over SPMI), touch, display scaling, RPM.

## 8. Risks to check before investing heavily

1. **iOS 16 userland ISA.** The iPhone X (A11 = ARMv8.2) userland may be built
   with LSE atomics or other v8.1+ instructions. If it is, every process hits
   UNDEFINED instructions on A53/A73. This is the potential show-stopper. Check
   first: extract a few core dylibs from the shared cache (`libsystem_kernel`,
   `libsystem_platform`, `libdispatch`, `libobjc`, `CoreFoundation`),
   disassemble them, and grep with the same regex the CI guard uses. If LSE
   shows up, the only way out is trapping and emulating those instructions in
   `sleh` (slow, and touches every atomic).
2. **4K pages with an iOS 16 userland.** Binaries are 16K-aligned, which maps
   fine on 4K. But iOS 16 never shipped on 4K hardware (iPadOS 15 on A8 was the
   last), so expect userland assumptions about `vm_page_size == 16K`.
3. **Display scaling** (section 2): SpringBoard on a cropped 720 px window is unusable.
4. **3 GB RAM**: same as the iPhone X, fine. After carve-outs about 2.5 GB is
   left for XNU.

## What was checked

- **CI**: `kernel.development.guamna` compiles and links with all 85 patches
  (run 41332581911), and passes the ARMv8.0 guard. SWEET also builds on
  Xcode 16 with 0084 (run 41329535910).

- The whole series (0001-0083) `git apply`s in CI order onto xnu-8796.141.3.
  0031/0041 only apply after the HFS staging script runs, as in CI.
- `guamna_emmc.c` compiles cleanly (`clang -target arm64-apple-darwin -Wall -Wextra`)
  against stub kernel declarations. That catches typos, not API mismatches.
- The shim compiles warning-free for both boards (`clang -target aarch64-linux-gnu`).
- `fdt-survey.py` runs against a synthetic bengal-shaped DTB.
- The ISA-guard regex matches v8.1+ mnemonics and not v8.0 `ldaxr`/`stlr`/`ldar`.
- **Not checked:** `mkadt.py --board guamna` output (not executed here),
  QEMU, anything on hardware.
