#!/usr/bin/env python3
"""Build the minimal Apple Device Tree that the SWEET xnu needs to boot.

Only what the early boot path actually reads (see NOTES.md); every MMIO
address comes from the sweet live FDT. Usage:  mkadt.py OUT.adt [--dump] [--qemu] [--highmem]

--board guamna builds the Moto G Play 2021 (SM4250) tree instead; see
build_guamna() and docs/PORTING_GUAMNA.md. Its addresses are a STUB until
tools/fdt-survey.py has been run on guamna's live FDT.
"""
import hashlib, os, struct, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adt  # noqa: E402

# Kernel-managed RAM: above every Qualcomm XPU carve-out (see NOTES.md)
PHYS_BASE = 0xb0000000
MEM_SIZE = 0x4d000000
# Panic log buffer, carved from the TOP of the dram range and exposed to XNU as
# /pram + /chosen/embedded-panic-log-size. Without it, pe_init.c's
# check_for_panic_log() leaves gPanicBase == 0, debug_log_init() bails out twice
# and the kernel hangs right after "kdp_core zlib memory" — reproduced on QEMU
# and independently on ios-fajita.
PANIC_LOG_SIZE = 0x100000
# --highmem (phone only): the whole upper DDR bank. The live FDT memory node is
# <0x80000000 2G>, <0x1_00000000 0xfcc00000>; every fixed reserved-memory
# carve-out is in the low bank and the dynamic pools are limited below 4G.
HIGH_PHYS_BASE = 0x100000000
HIGH_MEM_SIZE = 0xfcc00000
# /arm-io children are relative to this; it must not be 0 (serial_init bails)
SOC_BASE = 0x01000000

GICD, GICD_SIZE = 0x17a00000, 0x10000
GICR, GICR_SIZE = 0x17a60000, 0x100000
FB, FB_SIZE = 0x9c000000, 0x1700000
SWEET_ECID = 0x5357454554000001  # 'SWEET' + 1, synthetic
FB_W, FB_H, FB_STRIDE = 1080, 2400, 1080 * 4
# Internal UFS host (ufshc@1d84000 "ufs_mem"); QEMU virt has none
UFS, UFS_SIZE = 0x01d84000, 0x3000
TIMEBASE = 19200000
VTIMER_INTID = 19
# QEMU virt (-M virt,gic-version=3 -cpu cortex-a76): GICD/GICR and the generic timer's CNTFRQ
QEMU_GICD, QEMU_GICR, QEMU_TIMEBASE = 0x08000000, 0x080a0000, 62500000
QEMU_VTIMER_INTID = 27

BOOT_MANIFEST_HASH = hashlib.sha384(b'sweet boot manifest').digest()

# The iOS userland is the iPhone X (iPhone10,3 / D22AP) IPSW. MobileGestalt
# derives product type, device class, home-button-less UI, artwork etc. from
# the root identity and /product of the real D22AP device tree. That tree is
# an Apple artifact (kept out of git): extract it from the IPSW with
#   tools/remotezip.py URL DeviceTree.d22ap.im4p DIR
#   ipsw img4 im4p extract DIR/Firmware/all_flash/DeviceTree.d22ap.im4p -o d22ap.adt
D22AP_ADT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../research/ipsw-x/d22ap.adt')


def d22ap_product(path=D22AP_ADT):
    """/product properties of the real D22AP tree, without the syscfg
    placeholders iBoot would fill in (flag bit set) and the node bookkeeping."""
    if not os.path.exists(path):
        print(f'warning: {path} missing, no /product node', file=sys.stderr)
        return None
    root, _ = adt.parse(open(path, 'rb').read())
    for c in root.children:
        if dict((k, v) for k, f, v in c.props).get('name') == b'product\0':
            return [(k, v) for k, f, v in c.props if not f and k not in ('name', 'AAPL,phandle')]
    raise SystemExit(f'{path}: no /product node')

u32 = lambda *v: struct.pack('<%dI' % len(v), *v)
u64 = lambda *v: struct.pack('<%dQ' % len(v), *v)
cstr = lambda s: s.encode() + b'\0'


def node(name, props=(), children=()):
    n = adt.Node()
    n.props = [('name', 0, cstr(name))] + [(k, 0, v) for k, v in props]
    n.children = list(children)
    return n


def build(seed=None, qemu=False, highmem=False):
    """qemu=True: same tree, but with QEMU virt's GIC addresses and CNTFRQ (tools/qemu-sweet.py)."""
    phys_base, mem_size = (HIGH_PHYS_BASE, HIGH_MEM_SIZE) if highmem else (PHYS_BASE, MEM_SIZE)
    # Carve the panic log off the top so the kernel never allocates over it.
    panic_base = phys_base + mem_size - PANIC_LOG_SIZE
    mem_size -= PANIC_LOG_SIZE
    gicd, gicr, timebase = (QEMU_GICD, QEMU_GICR, QEMU_TIMEBASE) if qemu else (GICD, GICR, TIMEBASE)
    seed = seed if seed is not None else os.urandom(64)
    phandle = iter(range(1, 1000))
    ph = lambda: ('AAPL,phandle', u32(next(phandle)))

    chosen = node('chosen', [
        ph(),
        ('dram-base', u64(phys_base)),
        ('dram-size', u64(mem_size)),
        ('random-seed', seed),
        ('debug-enabled', u32(1)),
        ('embedded-panic-log-size', u32(PANIC_LOG_SIZE)),
        ('firmware-version', cstr('sweet-shim')),
        ('system-firmware-version', cstr('sweet-shim')),
        # iBoot's SHA-384 of the boot manifest; /sbin/mount -P 1 exits unless
        # it exists (bind mounts /private/preboot/<hex>/usr/standalone/firmware,
        # tolerating a missing directory). Fixed stand-in value.
        ('boot-manifest-hash', BOOT_MANIFEST_HASH),
        # libimg4 (cryptexd) aborts "failed to get crypto algorithm" without
        # it; real A11+ device trees say sha2-384.
        ('crypto-hash-method', cstr('sha2-384')),
        # MobileGestalt's screen class: display-scale (u32, default 0) comes
        # from /chosen, the pixel size from IOMobileFramebuffer. Scale 0 makes
        # SpringBoard's FBSDisplayMode "infxinf@0x" invalid. 1080x2400 @3x =
        # 360x800 points (like iPhone X class devices).
        ('display-scale', u32(3)),
        # XNU derives IOPlatformUUID (gethostuuid) from these on arm; without
        # them CFPreferences cannot use kCFPreferencesCurrentHost (no ByHost
        # files), so e.g. DataMigration never records LastSystemVersion and
        # migrates on every boot. Synthetic ECID (not the phone's); chip-id
        # of the A11 that the iPhone X userland expects.
        ('unique-chip-id', struct.pack('<Q', SWEET_ECID)),
        ('chip-id', u32(0x8015)),
        # Security status as on a dev-fused, undemoted board (tuple 0,0,1,1):
        # MobileGestalt reads these from /chosen, and mobileactivationd's
        # DeviceType then takes its native "Hactivation" branch (state
        # Activated without an activation record; no SEP, no baseband here).
        ('certificate-production-status', u32(0)),
        ('effective-production-status-ap', u32(0)),
        ('certificate-security-mode', u32(1)),
        ('effective-security-mode-sep', u32(1)),
    ], [node('memory-map', [ph()]),
        # Local boot policy (kern_csr.c): SIP relaxed like `csrutil disable`
        # (lp-sip0: untrusted kexts, unrestricted FS, task_for_pid, kernel
        # debugger, dtrace, nvram), lp-sip1: unauthenticated (unsealed) root.
        # The iOS shared cache is in no trust cache, so dyld can only map it
        # with CSR_ALLOW_UNRESTRICTED_FS (vm_unix.c shared region checks).
        node('asmb', [ph(), ('lp-sip0', u64(0x6f)), ('lp-sip1', u32(1))])])

    # SWEET SMP: all eight cores in physical-core order (0-5 Cortex-A55,
    # 6-7 Cortex-A76), one homogeneous cluster. The kernel defaults to one
    # and uses cpus=N to select an initial prefix (N <= 8).
    # QEMU retains its original single-CPU tree: its MPIDRs differ from sweet.
    cpu_nodes = []
    for core in range(1 if qemu else 8):
        cpu_nodes.append(node(f'cpu{core}', [
            ph(),
            ('device_type', cstr('cpu')),
            ('compatible', cstr('qcom,kryo470') + cstr('ARM,v8')),
            ('reg', u32(core << 8)),  # actual MPIDR Aff1:Aff0, NOT logical ID
            ('cpu-id', u32(core)),   # descriptive: XNU assigns IDs by DT order
            ('cluster-id', u32(0)),  # descriptive; see ml_parse_cpu_topology
            ('cluster-type', u32(0)),  # homogeneous CLUSTER_TYPE_SMP, not AMP
            ('die-cluster-id', u32(0)),
            ('cluster-core-id', u32(core)),
            ('l2-cache-id', u32(core)),  # distinct per-core L2 nodes in live FDT
            ('l3-cache-id', u32(0)),     # shared L3
            ('state', cstr('running' if core == 0 else 'stopped')),
            ('timebase-frequency', u32(timebase)),
            ('clock-frequency', u32(2304000000 if core >= 6 else 1804800000)),
            ('bus-frequency', u32(100000000)),
            ('memory-frequency', u32(1866000000)),
            ('peripheral-frequency', u32(timebase)),
            ('fixed-frequency', u32(timebase)),
        ]))
    cpus = node('cpus', [ph(), ('#address-cells', u32(1)),
                         ('#size-cells', u32(0))], cpu_nodes)

    gic = node('gic', [
        ph(),
        ('compatible', cstr('arm,gic-v3')),
        ('interrupt-controller', b''),
        ('virtual-timer-intid', u32(QEMU_VTIMER_INTID if qemu else VTIMER_INTID)),
        ('reg', u64(gicd - SOC_BASE, GICD_SIZE, gicr - SOC_BASE, GICR_SIZE)),
    ])
    arm_io = node('arm-io', [
        ph(),
        ('device_type', cstr('sweet-io')),
        ('compatible', cstr('sweet-io') + cstr('arm-io')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
        ('ranges', u64(0, SOC_BASE, 0x100000000 - SOC_BASE)),
    ], [gic,
        # No hardware: watchdogd checks IODeviceTree:/arm-io/wdt for no-pmu and
        # otherwise crashes trying to set the PMU boot stage (customer builds
        # ignore the -wdt_no_pmu boot-arg).
        node('wdt', [ph(), ('no-pmu', u32(1))]),
        # Main display: IOMobileFramebuffer.framework counts /arm-io children
        # with this device_type and reads the canvas size from the node
        # (the presented iPhone X size, see SweetFramebuffer.cpp);
        # the kernel's AppleCLCD matches the compatible string.
        node('disp0', [
            ph(),
            ('device_type', cstr('display-subsystem')),
            ('compatible', cstr('sweet,display')),
            ('canvas-width', u32(1125)),
            ('canvas-height', u32(2436)),
        ])])

    fbcon = node('sweet-fbcon', [
        ph(),
        ('reg', u64(FB, FB_SIZE)),
        ('width', u32(FB_W)),
        ('height', u32(FB_H)),
        ('stride', u32(FB_STRIDE)),
    ])

    # bsd/dev/sweet_ufs.c: the UFS host ABL leaves running (absolute physical reg).
    ufs = node('sweet-ufs', [
        ph(),
        ('compatible', cstr('qcom,ufshc')),
        ('reg', u64(UFS, UFS_SIZE)),
    ])

    # pmap_compute_max_asids requires the node even when no overrides are set.
    # An absent pmap-max-asids property selects XNU's own MAX_ASIDS default.
    # No effaceable storage: keybagd then reads /private/var/keybags/systembag.kb
    # as a plain plist instead of decrypting it with the effaceable key.
    defaults = node('defaults', [ph(), ('no-effaceable-storage', u32(1))])

    # MobileGestalt HomeButtonType = IODeviceTree:/buttons home-button-type
    # (D22AP: 2, no Home button; absent = 0 = Home-button device).
    buttons = node('buttons', [
        ph(),
        ('button-names', cstr('volup') + cstr('voldown') + cstr('hold')),
        ('home-button-type', u32(2)),
        ('opposed-power-vol-buttons', u32(1)),
    ])

    product_props = d22ap_product()
    product = [node('product', [ph()] + product_props)] if product_props else []

    # Panic log region. /pram reg = <phys size>; it sits just above the
    # dram-size we expose, so XNU maps it but never allocates into it.
    pram = node('pram', [ph(), ('reg', u64(panic_base) + u64(PANIC_LOG_SIZE))])

    # Identity of the iPhone X whose userland runs here: compatible[0] is
    # hw.model/hw.target (D22AP), model is hw.machine/hw.product
    # (iPhone10,3). 'xiaomi,sweet' stays for SweetPlatformExpert matching.
    return node('device-tree', [
        ph(),
        ('compatible', cstr('D22AP') + cstr('iPhone10,3') + cstr('xiaomi,sweet') + cstr('qcom,sm7150') + cstr('AppleARM')),
        ('model', cstr('iPhone10,3')),
        ('target-type', cstr('D22')),
        ('target-sub-type', cstr('D22AP')),
        ('manufacturer', cstr('Apple Inc.')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
    ], [chosen, defaults, cpus, arm_io, fbcon, buttons, pram] + product + ([] if qemu else [ufs]))


# --------------------------------------------------------------------------
# guamna: Motorola Moto G Play 2021, Qualcomm SM4250 (Snapdragon 460, "bengal")
#
# STUB. Status of every value:
#   USER      given by the porter (UART, GCC)
#   MAINLINE  from Linux arch/arm64/boot/dts/qcom/sm6115.dtsi (SM4250 is the
#             same die), recalled, NOT read from this phone
#   TODO      unknown; None means "not emitted" and is reported on stderr
# Replace MAINLINE/TODO values with the output of
#   tools/fdt-survey.py guamna-live.dtb
# (live FDT: `adb root && adb pull /sys/firmware/fdt guamna-live.dtb`).
# A wrong MMIO address does not brick anything (XNU runs from RAM via
# `fastboot boot`), but an XPU-protected one hangs the phone until a
# forced reboot (hold Power + Volume down).
# --------------------------------------------------------------------------
GUAMNA = dict(
    # RAM handed to XNU: one contiguous range that avoids every FIXED
    # reserved-memory carve-out (TZ, hyp, XBL/AOP, SMEM, PIL regions). bengal
    # DDR starts at 0x40000000 and all 3 GiB sit below 4 GiB, so unlike sweet
    # there is no carve-out-free upper bank. TODO: take the largest free range
    # that fdt-survey.py reports. This placeholder (2 GiB at 0x60000000)
    # assumes the PIL/splash carve-outs end below 0x60000000.
    phys_base=0x60000000,                      # TODO
    mem_size=0x80000000,                       # TODO
    soc_base=0x00400000,                       # /arm-io base; below TLMM, must be != 0
    gicd=(0x0f200000, 0x10000),                # MAINLINE intc reg[0]
    gicr=(0x0f300000, 0x100000),               # MAINLINE intc reg[1], stride 0x20000
    timebase=19200000,                         # MAINLINE
    vtimer_intid=19,                           # MAINLINE <GIC_PPI 3 ...> = 16 + 3, same as sweet
    # ABL continuous-splash framebuffer (reserved-memory cont_splash_region).
    fb=(0x5c000000, 0x00f00000),               # TODO (MAINLINE-ish guess)
    fb_w=720, fb_h=1600, fb_stride=720 * 4,    # panel is 720x1600; stride TODO (check ABL pads)
    # Storage: eMMC 5.1 on SDHC1 (sdhci-msm v5). reg = hc, cqhci.
    emmc=(0x04744000, 0x1000),                 # MAINLINE sdhc_1 "hc"
    emmc_cqhci=(0x04745000, 0x1000),           # MAINLINE sdhc_1 "cqhci"
    uart=(0x04a90000, 0x4000),                 # USER: QUPv3 SE4 GENI UART (debug UART)
    gcc=(0x01400000, 0x1f0000),                # USER
    # TLMM: mainline sm6115 has three tiles at 0x00500000 (west),
    # 0x00900000 (south), 0x00d00000 (east). The 0x03000000 in the porting
    # brief does not match that; fdt-survey.py settles it.
    tlmm=(0x00500000, 0x00c00000),             # MAINLINE, conflicts with brief (0x03000000)
    # SPMI PMIC arbiter v5 (PM6125 + PMI632): core, obsrvr, cnfg.
    spmi_core=(0x01c40000, 0x1100),            # MAINLINE
    spmi_obsrvr=(0x03e00000, 0x100000),        # MAINLINE
    spmi_cnfg=(0x01c0a000, 0x26000),           # MAINLINE
    # RPM (not RPMh): GLINK over the RPM message RAM, kicked through the
    # APCS IPC register. Needed for regulator votes; nothing uses it yet.
    rpm_msg_ram=None,                          # TODO (0x045f0000 in mainline?)
    apcs_ipc=None,                             # TODO (APCS glb + 0x8, bit 0?)
    # USB (DWC3 + QUSB2), for the ECM log/network gadget later.
    usb_dwc3=None,                             # TODO (0x04e00000 in mainline?)
    usb_qscratch=None,                         # TODO (0x04ef8800?)
    usb_qusb2=None,                            # TODO (0x01613000?)
    # Kryo 240: cluster 0 = 4x Cortex-A53 (MPIDR 0x000-0x003), cluster 1 =
    # 4x Cortex-A73 (MPIDR 0x100-0x103). Classic big.LITTLE: Aff1 is the
    # cluster, Aff0 the core (sweet's DynamIQ puts the core in Aff1).
    cpus=[(0, c, 1804800000) for c in range(4)] + [(1, c, 1804800000) for c in range(4)],  # TODO freqs
)


def build_guamna(seed=None):
    """Stub ADT for guamna. Same identity/userland nodes as sweet (iPhone X),
    different SoC, CPU topology, memory and storage."""
    g = GUAMNA
    missing = sorted(k for k, v in g.items() if v is None)
    if missing:
        print('guamna: not emitted (TODO): ' + ', '.join(missing), file=sys.stderr)
    print('guamna: STUB addresses; verify with tools/fdt-survey.py before booting', file=sys.stderr)

    phys_base, mem_size = g['phys_base'], g['mem_size']
    panic_base = phys_base + mem_size - PANIC_LOG_SIZE
    mem_size -= PANIC_LOG_SIZE
    seed = seed if seed is not None else os.urandom(64)
    phandle = iter(range(1, 1000))
    ph = lambda: ('AAPL,phandle', u32(next(phandle)))
    soc = g['soc_base']
    reg = lambda r: u64(r[0], r[1])

    chosen = node('chosen', [
        ph(),
        ('dram-base', u64(phys_base)),
        ('dram-size', u64(mem_size)),
        ('random-seed', seed),
        ('debug-enabled', u32(1)),
        ('embedded-panic-log-size', u32(PANIC_LOG_SIZE)),
        ('firmware-version', cstr('guamna-shim')),
        ('system-firmware-version', cstr('guamna-shim')),
        ('boot-manifest-hash', BOOT_MANIFEST_HASH),
        ('crypto-hash-method', cstr('sha2-384')),
        # 720x1600 @2x = 360x800 points. NOTE: SweetFramebuffer presents a
        # 1125x2436 iPhone X canvas and CROPS it to the panel; on a 720-wide
        # panel that needs a downscale path first (docs/PORTING_GUAMNA.md).
        ('display-scale', u32(2)),
        ('unique-chip-id', struct.pack('<Q', 0x4755414d4e410001)),  # 'GUAMNA' + 1, synthetic
        ('chip-id', u32(0x8015)),
        ('certificate-production-status', u32(0)),
        ('effective-production-status-ap', u32(0)),
        ('certificate-security-mode', u32(1)),
        ('effective-security-mode-sep', u32(1)),
    ], [node('memory-map', [ph()]),
        node('asmb', [ph(), ('lp-sip0', u64(0x6f)), ('lp-sip1', u32(1))])])

    cpu_nodes = []
    for i, (cluster, core, hz) in enumerate(g['cpus']):
        cpu_nodes.append(node(f'cpu{i}', [
            ph(),
            ('device_type', cstr('cpu')),
            ('compatible', cstr('qcom,kryo240') + cstr('ARM,v8')),
            ('reg', u32(cluster << 8 | core)),   # MPIDR Aff1:Aff0
            ('cpu-id', u32(i)),
            ('cluster-id', u32(0)),             # one SMP cluster for XNU, as on sweet
            ('cluster-type', u32(0)),
            ('die-cluster-id', u32(0)),
            ('cluster-core-id', u32(i)),
            ('l2-cache-id', u32(cluster)),      # L2 is shared per physical cluster
            ('state', cstr('running' if i == 0 else 'stopped')),
            ('timebase-frequency', u32(g['timebase'])),
            ('clock-frequency', u32(hz)),
            ('bus-frequency', u32(100000000)),
            ('memory-frequency', u32(1866000000)),   # LPDDR4X, descriptive
            ('peripheral-frequency', u32(g['timebase'])),
            ('fixed-frequency', u32(g['timebase'])),
        ]))
    cpus = node('cpus', [ph(), ('#address-cells', u32(1)), ('#size-cells', u32(0))], cpu_nodes)

    gic = node('gic', [
        ph(),
        ('compatible', cstr('arm,gic-v3')),
        ('interrupt-controller', b''),
        ('virtual-timer-intid', u32(g['vtimer_intid'])),
        ('reg', u64(g['gicd'][0] - soc, g['gicd'][1], g['gicr'][0] - soc, g['gicr'][1])),
    ])
    arm_io = node('arm-io', [
        ph(),
        ('device_type', cstr('sweet-io')),
        ('compatible', cstr('sweet-io') + cstr('arm-io')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
        ('ranges', u64(0, soc, 0x100000000 - soc)),
    ], [gic,
        node('wdt', [ph(), ('no-pmu', u32(1))]),
        node('disp0', [
            ph(),
            ('device_type', cstr('display-subsystem')),
            ('compatible', cstr('sweet,display')),
            ('canvas-width', u32(1125)),
            ('canvas-height', u32(2436)),
        ])])

    fbcon = node('sweet-fbcon', [
        ph(),
        ('reg', reg(g['fb'])),
        ('width', u32(g['fb_w'])),
        ('height', u32(g['fb_h'])),
        ('stride', u32(g['fb_stride'])),
    ])

    # Absolute physical regs, read by the guamna drivers (bsd/dev/guamna_emmc.c
    # from patch 0083; the rest are for drivers that do not exist yet). Unlike
    # sweet, drivers should take addresses from here instead of #defines.
    hw = [node('guamna-emmc', [ph(), ('compatible', cstr('qcom,sdhci-msm-v5')),
                               ('reg', reg(g['emmc']) + reg(g['emmc_cqhci']))])]
    for name, key in (('guamna-uart', 'uart'), ('guamna-gcc', 'gcc'), ('guamna-tlmm', 'tlmm'),
                      ('guamna-rpm-msg-ram', 'rpm_msg_ram'), ('guamna-apcs-ipc', 'apcs_ipc'),
                      ('guamna-usb-dwc3', 'usb_dwc3'), ('guamna-usb-qscratch', 'usb_qscratch'),
                      ('guamna-usb-qusb2', 'usb_qusb2')):
        if g[key] is not None:
            hw.append(node(name, [ph(), ('reg', reg(g[key]))]))
    if None not in (g['spmi_core'], g['spmi_obsrvr'], g['spmi_cnfg']):
        hw.append(node('guamna-spmi', [ph(), ('compatible', cstr('qcom,spmi-pmic-arb')),
                                       ('reg', reg(g['spmi_core']) + reg(g['spmi_obsrvr']) +
                                        reg(g['spmi_cnfg']))]))

    defaults = node('defaults', [ph(), ('no-effaceable-storage', u32(1))])
    buttons = node('buttons', [
        ph(),
        ('button-names', cstr('volup') + cstr('voldown') + cstr('hold')),
        ('home-button-type', u32(2)),
        ('opposed-power-vol-buttons', u32(1)),
    ])
    product_props = d22ap_product()
    product = [node('product', [ph()] + product_props)] if product_props else []
    pram = node('pram', [ph(), ('reg', u64(panic_base) + u64(PANIC_LOG_SIZE))])

    # 'xiaomi,sweet' stays: SweetPlatformExpert matches it.
    return node('device-tree', [
        ph(),
        ('compatible', cstr('D22AP') + cstr('iPhone10,3') + cstr('motorola,guamna') +
         cstr('xiaomi,sweet') + cstr('qcom,sm4250') + cstr('AppleARM')),
        ('model', cstr('iPhone10,3')),
        ('target-type', cstr('D22')),
        ('target-sub-type', cstr('D22AP')),
        ('manufacturer', cstr('Apple Inc.')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
    ], [chosen, defaults, cpus, arm_io, fbcon, buttons, pram] + product + hw)


if __name__ == '__main__':
    if '--board' in sys.argv and sys.argv[sys.argv.index('--board') + 1] == 'guamna':
        root = build_guamna()
    else:
        root = build(qemu='--qemu' in sys.argv, highmem='--highmem' in sys.argv)
    blob = adt.serialize(root)
    open(sys.argv[1], 'wb').write(blob)
    if '--dump' in sys.argv:
        adt.dump(adt.parse(blob)[0])
    print(f'{sys.argv[1]}: {len(blob)} bytes', file=sys.stderr)
