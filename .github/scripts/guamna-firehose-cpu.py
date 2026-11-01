#!/usr/bin/env python3
"""GUAMNA CI: make blacktop/darwin-xnu-build compile libfirehose_kernel's arm64
slice with -mcpu=cortex-a53. xcodebuild builds it for arm64-apple-macos, whose
Apple clang default CPU (apple-m1) emits LSE/LDAPR, UNDEFINED on the A53/A73.
The command line cannot take a conditional setting, so build.sh appends an
OTHER_CFLAGS[arch=arm64] copy of the target's own flags to its xcconfig.
Usage: guamna-firehose-cpu.py dxb/build.sh"""
import sys
p = sys.argv[1]
t = open(p).read()
anchor = 's|$(SDKROOT)/usr/local/include|$(FAKEROOT_DIR)/usr/local/include|g\' "${SRCROOT}/xcodeconfig/libfirehose_kernel.xcconfig"\n'
assert t.count(anchor) == 1, 'anchor not found'
add = ('        # GUAMNA (ARMv8.0): arm64 slice with the kernel\'s -mcpu, keeping the target\'s own flags\n'
       '        grep \'^OTHER_CFLAGS *=\' "${SRCROOT}/xcodeconfig/libfirehose_kernel.xcconfig" | '
       'sed \'s/^OTHER_CFLAGS/OTHER_CFLAGS[arch=arm64]/; s/$/ -mcpu=cortex-a53/\' '
       '>> "${SRCROOT}/xcodeconfig/libfirehose_kernel.xcconfig"\n'
       '        grep -n \'OTHER_CFLAGS\' "${SRCROOT}/xcodeconfig/libfirehose_kernel.xcconfig"\n')
t = t.replace(anchor, anchor + add)
open(p, 'w').write(t)
print('injected')
