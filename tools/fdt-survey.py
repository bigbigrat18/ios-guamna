#!/usr/bin/env python3
"""Survey a live Qualcomm FDT for the values a new ios-sweet board port needs.

Pull the merged, live device tree from the phone (Android, adb root):
    adb root && adb pull /sys/firmware/fdt guamna-live.dtb
then:
    tools/fdt-survey.py guamna-live.dtb [--all]

Prints, with absolute physical addresses (bus `ranges` applied):
  - DRAM banks, fixed reserved-memory carve-outs and the free RAM ranges left
    over (the candidates for /chosen dram-base/dram-size in mkadt.py),
  - CPUs (MPIDR), PSCI, timer PPIs and the GIC,
  - the SoC blocks the port touches (SDHCI/eMMC, UFS, GENI UART, GCC, TLMM,
    SPMI, RPM/RPMh, APCS, USB, display, touch, keys, PS_HOLD, IMEM),
  - a GUAMNA = dict(...) snippet for tools/mkadt.py.
Pure standard library, no dtc needed. Device dumps belong outside git.
"""
import re
import struct
import sys

FDT_MAGIC = 0xd00dfeed
FDT_BEGIN_NODE, FDT_END_NODE, FDT_PROP, FDT_NOP, FDT_END = 1, 2, 3, 4, 9


class Node:
    def __init__(self, name, parent=None):
        self.name, self.parent, self.props, self.children = name, parent, {}, []

    @property
    def path(self):
        if self.parent is None:
            return '/'
        p = self.parent.path
        return (p if p != '/' else '') + '/' + self.name

    def str(self, key, default=''):
        v = self.props.get(key)
        return v.rstrip(b'\0').split(b'\0')[0].decode(errors='replace') if v else default

    def strs(self, key):
        v = self.props.get(key)
        return [s.decode(errors='replace') for s in v.rstrip(b'\0').split(b'\0')] if v else []

    def cells(self, key):
        v = self.props.get(key, b'')
        return list(struct.unpack('>%dI' % (len(v) // 4), v[:len(v) // 4 * 4]))

    def u32(self, key, default=None):
        c = self.cells(key)
        return c[0] if c else default

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


def parse(blob):
    magic, totalsize, off_struct, off_strings, _, version = struct.unpack_from('>6I', blob, 0)
    if magic != FDT_MAGIC:
        raise SystemExit('not an FDT (bad magic 0x%08x)' % magic)
    strings = blob[off_strings:]
    pos, root, cur = off_struct, None, None
    while True:
        tok, = struct.unpack_from('>I', blob, pos)
        pos += 4
        if tok == FDT_BEGIN_NODE:
            end = blob.index(b'\0', pos)
            name = blob[pos:end].decode(errors='replace')
            pos = (end + 4) & ~3
            n = Node(name, cur)
            if cur is None:
                root = n
            else:
                cur.children.append(n)
            cur = n
        elif tok == FDT_END_NODE:
            cur = cur.parent
        elif tok == FDT_PROP:
            ln, nameoff = struct.unpack_from('>II', blob, pos)
            pos += 8
            key = strings[nameoff:strings.index(b'\0', nameoff)].decode()
            cur.props[key] = blob[pos:pos + ln]
            pos = (pos + ln + 3) & ~3
        elif tok == FDT_NOP:
            continue
        elif tok == FDT_END:
            return root
        else:
            raise SystemExit('bad FDT token %d at 0x%x' % (tok, pos - 4))


def addr_cells(n):
    return n.u32('#address-cells', 2) if n is not None else 2


def size_cells(n):
    return n.u32('#size-cells', 1) if n is not None else 1


def join(cells):
    v = 0
    for c in cells:
        v = (v << 32) | c
    return v


def translate(node, addr):
    """Bus address in node's parent space -> CPU physical (None if untranslatable)."""
    bus = node.parent
    while bus is not None and bus.parent is not None:
        if 'ranges' not in bus.props:
            return None
        rng = bus.cells('ranges')
        if rng:
            ca, pa, cs = addr_cells(bus), addr_cells(bus.parent), size_cells(bus)
            step = ca + pa + cs
            for i in range(0, len(rng) - step + 1, step):
                child = join(rng[i:i + ca])
                parent = join(rng[i + ca:i + ca + pa])
                size = join(rng[i + ca + pa:i + step])
                if child <= addr < child + size:
                    addr = parent + (addr - child)
                    break
            else:
                return None
        bus = bus.parent
    return addr


def regs(node):
    """[(phys, size, name)] for node's reg, translated to CPU addresses."""
    ac, sc = addr_cells(node.parent), size_cells(node.parent)
    c = node.cells('reg')
    names = node.strs('reg-names')
    out = []
    if ac + sc == 0:
        return out
    for i in range(0, len(c) - (ac + sc) + 1, ac + sc):
        a = join(c[i:i + ac])
        s = join(c[i + ac:i + ac + sc])
        idx = len(out)
        out.append((translate(node, a), s, names[idx] if idx < len(names) else ''))
    return out


def fmt_regs(node):
    parts = []
    for a, s, name in regs(node):
        a_s = '0x%08x' % a if a is not None else '(untranslatable)'
        parts.append('%s%s+0x%x' % (name + '=' if name else '', a_s, s))
    return ' '.join(parts) if parts else '(no reg)'


def enabled(n):
    return n.str('status', 'okay') in ('okay', 'ok')


def interrupts(n):
    return n.cells('interrupts')


# compatible substring -> label. First match wins; order matters.
BLOCKS = [
    ('sdhci', 'SDHCI (eMMC/SD)'), ('ufshc', 'UFS host'), ('ufs-phy', 'UFS PHY'),
    ('geni-debug-uart', 'GENI debug UART'), ('geni-uart', 'GENI UART'),
    ('geni-se-qup', 'QUP wrapper'), ('geni-i2c', 'GENI I2C'), ('geni-spi', 'GENI SPI'),
    ('gcc', 'GCC'), ('tlmm', 'TLMM'), ('pinctrl', 'TLMM'),
    ('spmi-pmic-arb', 'SPMI arbiter'), ('glink-rpm', 'GLINK RPM'), ('rpm-smd', 'RPM SMD'),
    ('rpm-glink', 'RPM GLINK'), ('rpmh-rsc', 'RPMh RSC'), ('apcs', 'APCS'),
    ('msm-imem', 'IMEM'), ('pshold', 'PS_HOLD'), ('mpm', 'MPM'),
    ('dwc3', 'USB DWC3'), ('qusb2', 'USB QUSB2 PHY'), ('usb-hs-phy', 'USB HS PHY'),
    ('mdss', 'Display MDSS'), ('mdp', 'Display MDP'), ('dsi', 'DSI'),
    ('simple-framebuffer', 'simple-framebuffer'), ('gpio-keys', 'gpio-keys'),
    ('pon', 'PMIC PON'), ('smem', 'SMEM'), ('watchdog', 'watchdog'), ('wdt', 'watchdog'),
]
TOUCH_HINTS = ('touch', 'ts@', 'goodix', 'focaltech', 'novatek', 'ilitek', 'himax', 'synaptics',
               'chipone', 'fts', 'nvt', 'hx83')


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    root = parse(open(sys.argv[1], 'rb').read())
    show_all = '--all' in sys.argv

    print('model: %s | compatible: %s' % (root.str('model'), ', '.join(root.strs('compatible'))))
    for n in root.walk():
        if n.name.startswith('chosen'):
            for k in ('linux,initrd-start', 'linux,initrd-end'):
                if k in n.props:
                    print('chosen %s = 0x%x' % (k, join(n.cells(k))))
            if 'bootargs' in n.props:
                # Serial numbers, MACs and similar identifiers: never print them.
                args = re.sub(r'\b([\w.]*(serial|serialno|mac|imei|cid|uid|hwid|ufs_id|emmc_id|sn)[\w.]*)=\S+',
                              r'\1=<redacted>', n.str('bootargs'), flags=re.I)
                print('chosen bootargs = %s' % args[:300])

    # ---- memory ----
    banks = []
    for n in root.children:
        if n.str('device_type') == 'memory' or n.name.split('@')[0] == 'memory':
            banks += [(a, s) for a, s, _ in regs(n) if a is not None and s]
    banks.sort()
    print('\n== DRAM banks')
    for a, s in banks:
        print('  0x%09x - 0x%09x  (%d MiB)' % (a, a + s, s >> 20))

    fixed, dynamic = [], []
    rm = next((c for c in root.children if c.name == 'reserved-memory'), None)
    if rm:
        for c in rm.children:
            if not enabled(c):
                continue
            nomap = 'no-map' in c.props
            if 'reg' in c.props:
                for a, s, _ in regs(c):
                    if a is not None and s:
                        fixed.append((a, s, c.name, nomap))
            else:
                dynamic.append((c.name, join(c.cells('size')) if 'size' in c.props else 0,
                                c.cells('alloc-ranges')))
    fixed.sort()
    print('\n== reserved-memory, fixed (must stay outside XNU RAM)')
    for a, s, name, nomap in fixed:
        print('  0x%09x - 0x%09x  %8d KiB  %-32s%s' % (a, a + s, s >> 10, name, ' no-map' if nomap else ''))
    print('\n== reserved-memory, dynamic (allocated by Linux; irrelevant once XNU owns RAM)')
    for name, size, ar in dynamic:
        print('  %-32s size 0x%x alloc-ranges %s' % (name, size, ' '.join('0x%x' % x for x in ar)))

    free = []
    for a, s in banks:
        start, end = a, a + s
        for ra, rs, _, _ in fixed:
            if ra + rs <= start or ra >= end:
                continue
            if ra > start:
                free.append((start, ra))
            start = max(start, ra + rs)
        if start < end:
            free.append((start, end))
    free = [(a, b) for a, b in free if b - a >= (16 << 20)]
    print('\n== free RAM (DRAM minus fixed carve-outs, >= 16 MiB)')
    for a, b in free:
        print('  0x%09x - 0x%09x  (%d MiB)' % (a, b, (b - a) >> 20))
    best = max(free, key=lambda r: r[1] - r[0], default=None)
    if best:
        # 32 MiB alignment: tools/mkpayload.py puts virt_base on a 32 MB
        # boundary and maps it to phys_base (also a multiple of the 4K-granule
        # 2 MiB L2 block XNU's bootstrap maps with).
        base = (best[0] + 0x1ffffff) & ~0x1ffffff
        size = (best[1] - base) & ~0x1fffff
        print('  -> largest: phys_base=0x%x mem_size=0x%x (%d MiB), base 32 MiB aligned' % (base, size, size >> 20))
        print('     Keep the shim/ramdisk that ABL loads OUTSIDE this range (see chosen initrd above).')

    # ---- CPUs, PSCI, timer, GIC ----
    print('\n== CPUs')
    cpus = next((c for c in root.children if c.name == 'cpus'), None)
    for c in (cpus.children if cpus else []):
        if c.str('device_type') == 'cpu':
            print('  %-8s mpidr=0x%03x %-24s enable-method=%s capacity=%s' % (
                c.name, c.u32('reg', 0), c.str('compatible'), c.str('enable-method'),
                c.u32('capacity-dmips-mhz')))
    for n in root.walk():
        compat = ' '.join(n.strs('compatible'))
        if 'arm,psci' in compat:
            print('  psci: %s method=%s' % (compat, n.str('method')))
        if 'arm,armv8-timer' in compat:
            irq = interrupts(n)
            ppis = [irq[i + 1] for i in range(0, len(irq) - 2, 3)]
            print('  timer PPIs (sec-phys, phys, virt, hyp) = %s -> virtual timer INTID %s; '
                  'clock-frequency=%s' % (ppis, 16 + ppis[2] if len(ppis) > 2 else '?',
                                           n.u32('clock-frequency')))
        if 'arm,gic-v3' in compat or 'arm,gic-400' in compat or 'arm,cortex-a15-gic' in compat:
            print('  GIC: %s %s redistributor-stride=%s' % (compat, fmt_regs(n),
                                                            hex(join(n.cells('redistributor-stride')))
                                                            if 'redistributor-stride' in n.props else '-'))

    # ---- SoC blocks ----
    print('\n== SoC blocks (enabled nodes; --all also shows disabled ones)')
    found = {}
    for n in root.walk():
        compat = ' '.join(n.strs('compatible'))
        label = next((l for k, l in BLOCKS if k in compat), None)
        low = (n.name + ' ' + compat).lower()
        if label is None and any(h in low for h in TOUCH_HINTS) and 'reg' in n.props:
            label = 'touch?'
        if label is None and n.name.startswith(('cont_splash', 'splash', 'dfps_data', 'disp_rdump')):
            label = 'splash framebuffer'
        if label is None or (not enabled(n) and not show_all):
            continue
        found.setdefault(label, []).append(n)
        print('  %-20s %-44s %s%s\n  %-20s   compatible: %s' % (
            label, n.path[-44:], fmt_regs(n), '' if enabled(n) else '  [disabled]', '', compat[:110]))
        if label == 'touch?':
            print('  %-20s   bus address %s on %s (%s)' % ('', [hex(x) for x in n.cells('reg')], n.parent.path,
                                                         ' '.join(n.parent.strs('compatible'))))
        if label == 'gpio-keys':
            for k in n.children:
                print('  %-20s   key %-20s gpios=%s linux,code=%s' % ('', k.name, k.cells('gpios'),
                                                                      k.u32('linux,code')))

    # ---- mkadt snippet ----
    def first(label, name=None):
        for n in found.get(label, []):
            for a, s, rn in regs(n):
                if a is not None and (name is None or rn == name):
                    return '(0x%08x, 0x%x)' % (a, s)
        return 'None'

    gic = next((n for n in root.walk() if 'arm,gic-v3' in ' '.join(n.strs('compatible'))), None)
    gr = regs(gic) if gic else []
    print('\n== mkadt.py GUAMNA values (review every line; first match per block)')
    if best:
        print('    phys_base=0x%x, mem_size=0x%x,' % (base, size))
    if len(gr) >= 2:
        print('    gicd=(0x%08x, 0x%x), gicr=(0x%08x, 0x%x),' % (gr[0][0], gr[0][1], gr[1][0], gr[1][1]))
    print('    emmc=%s, emmc_cqhci=%s,' % (first('SDHCI (eMMC/SD)', 'hc'), first('SDHCI (eMMC/SD)', 'cqhci')))
    print('    uart=%s,' % first('GENI debug UART'))
    print('    gcc=%s, tlmm=%s,  # tlmm: first tile only; span all tiles (west..east)' % (first('GCC'), first('TLMM')))
    print('    spmi_core=%s, spmi_obsrvr=%s, spmi_cnfg=%s,' % (
        first('SPMI arbiter', 'core'), first('SPMI arbiter', 'obsrvr'), first('SPMI arbiter', 'cnfg')))
    print('    usb_dwc3=%s, usb_qusb2=%s,' % (first('USB DWC3'), first('USB QUSB2 PHY')))
    print('    fb=%s,' % first('splash framebuffer'))
    print('  (SDHCI: pick the eMMC controller, usually sdhc_1 with "non-removable"; sdhc_2 is the SD slot.)')


if __name__ == '__main__':
    main()
