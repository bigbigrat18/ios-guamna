#!/usr/bin/env python3
"""Count instructions a Cortex-A53/A73 (ARMv8.0) cannot execute in an arm64
dyld shared cache (main file + .01, .02, ... sub-caches) or a Mach-O.

  isa-scan.py dyld_shared_cache_arm64 [--top N]     report (Markdown)

No disassembler: every 32-bit word in the executable mappings is matched
against the encodings below (Arm ARM, A64). Data embedded in text can produce
a few false hits; compiled-in use shows up as thousands. ldaxr/stlxr counts
give the scale of ordinary ARMv8.0 atomics for comparison. Hits are attributed
to the dylib whose Mach-O header precedes them (main cache image list).
"""
import os
import struct
import sys

import numpy as np

# name: (mask, value, exclusions as (mask, value))
PATTERNS = {
    'LSE CAS/CASP (v8.1)': [(0x3fa07c00, 0x08a07c00), (0xbfa07c00, 0x08207c00)],
    'LSE LD<op>/ST<op>/SWP (v8.1)': [(0x3f200c00, 0x38200000)],
    'RCpc LDAPR (v8.3)': [(0x3ffffc00, 0x38bfc000)],
    'RDM SQRDMLAH/SH (v8.1)': [(0xbf20d400, 0x2e008400), (0xbf00d400, 0x2f00d000), (0xff20d400, 0x7e008400)],
    'FP16 scalar (v8.2)': [(0xffe00000, 0x1ee00000), (0xffc00000, 0x1fc00000)],
    'FP16 vector (v8.2)': [(0x9f60c400, 0x0e400400)],
    'Dot product (v8.2+)': [(0x9fe0fc00, 0x0e809400), (0x9fc0f400, 0x0f80e000)],
    'FJCVTZS (v8.3)': [(0xfffffc00, 0x1e7e0000)],
}
# Encodings inside the masks above that ARE ARMv8.0: FCVT from half (FEAT_FP
# base), and the LD<op> space overlaps nothing else with bit 21 set and bits
# 11:10 = 00 for V = 0.
EXCLUDE = {
    'LSE LD<op>/ST<op>/SWP (v8.1)': [(0x3ffffc00, 0x38bfc000)],     # LDAPR, counted on its own
    'FP16 scalar (v8.2)': [(0xfffe7c00, 0x1ee24000), (0xfffe7c00, 0x1ee2c000)],
}
BASELINE = {'ldaxr/ldxr (v8.0)': [(0x3fff7c00, 0x085f7c00)], 'stlxr/stxr (v8.0)': [(0x3fe07c00, 0x08007c00)]}


def mappings(blob):
    if not blob.startswith(b'dyld_v1'):
        return None
    off, count = struct.unpack_from('<II', blob, 0x10)
    out = []
    for i in range(count):
        addr, size, foff, maxp, initp = struct.unpack_from('<QQQII', blob, off + 32 * i)
        out.append((addr, size, foff, initp))
    return out


def images(blob):
    """(address, path) of every dylib, from the main cache header."""
    hdr_size = struct.unpack_from('<I', blob, 0x10)[0]      # mappingOffset = header size
    if hdr_size >= 0x1c8:
        off, count = struct.unpack_from('<II', blob, 0x1c0)
    else:
        off, count = struct.unpack_from('<II', blob, 0x18)
    res = []
    for i in range(count):
        addr, _, _, poff, _ = struct.unpack_from('<QQQII', blob, off + 32 * i)
        end = blob.index(b'\0', poff)
        res.append((addr, blob[poff:end].decode(errors='replace')))
    res.sort()
    return res


def match(words, pats):
    hit = np.zeros(words.shape, dtype=bool)
    for m, v in pats:
        hit |= (words & np.uint32(m)) == np.uint32(v)
    return hit


def main():
    base = sys.argv[1]
    top = int(sys.argv[sys.argv.index('--top') + 1]) if '--top' in sys.argv else 15
    files = [base] + sorted(p for p in (os.path.join(os.path.dirname(base), f) for f in os.listdir(os.path.dirname(base) or '.'))
                            if p.startswith(base + '.') and not p.endswith('.symbols') and p[len(base) + 1:].isdigit())
    main_blob = open(base, 'rb').read()
    imgs = images(main_blob) if mappings(main_blob) else []
    img_addrs = np.array([a for a, _ in imgs], dtype=np.uint64)

    totals = {k: 0 for k in list(PATTERNS) + list(BASELINE)}
    per_image = {}
    examples = []
    n_words = 0
    for path in files:
        blob = main_blob if path == base else open(path, 'rb').read()
        maps = mappings(blob)
        for addr, size, foff, initp in maps or []:
            if not initp & 4 or size == 0:
                continue
            words = np.frombuffer(blob, dtype='<u4', count=size // 4, offset=foff)
            n_words += words.size
            for name, pats in list(PATTERNS.items()) + list(BASELINE.items()):
                hit = match(words, pats)
                if name in EXCLUDE:
                    hit &= ~match(words, EXCLUDE[name])
                idx = np.nonzero(hit)[0]
                totals[name] += idx.size
                if name in BASELINE or idx.size == 0:
                    continue
                vaddrs = np.uint64(addr) + idx.astype(np.uint64) * np.uint64(4)
                owners = np.searchsorted(img_addrs, vaddrs, side='right') - 1 if img_addrs.size else np.full(idx.size, -1)
                for o in np.unique(owners):
                    key = imgs[o][1] if o >= 0 else '?'
                    per_image.setdefault(key, {}).setdefault(name, 0)
                    per_image[key][name] += int((owners == o).sum())
                for k in idx[:2]:
                    examples.append((name, addr + 4 * int(k), int(words[k])))

    print(f'# ARMv8.0 check: {os.path.basename(base)} ({len(files)} files, {n_words:,} words in executable mappings)\n')
    print('| instruction group | words matching |')
    print('|---|---|')
    for k, v in totals.items():
        print(f'| {k} | {v:,} |')
    print('\n## Images with the most non-ARMv8.0 hits\n')
    ranked = sorted(per_image.items(), key=lambda kv: -sum(kv[1].values()))
    print('| image | hits | by group |')
    print('|---|---|---|')
    for name, d in ranked[:top]:
        print(f'| {name} | {sum(d.values()):,} | ' + ', '.join(f'{k.split(" (")[0]} {v}' for k, v in sorted(d.items(), key=lambda kv: -kv[1])) + ' |')
    for want in ('libsystem_kernel', 'libsystem_platform', 'libsystem_pthread', 'libdispatch', 'libobjc', 'CoreFoundation', 'libc++.1'):
        for name, d in per_image.items():
            if want in name.rsplit('/', 1)[-1]:
                print(f'| {name} | {sum(d.values()):,} | ' + ', '.join(f'{k.split(" (")[0]} {v}' for k, v in d.items()) + ' |')
                break
    print('\n## Examples\n```')
    for name, a, w in examples[:30]:
        print(f'{a:#x}  {w:08x}  {name}')
    print('```')


if __name__ == '__main__':
    main()
