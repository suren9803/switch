#!/usr/bin/env python3
"""Add an ad-hoc code signature to a Mach-O file (no certificate, no Apple tooling).

    python sign_adhoc.py <mach-o file> [--identifier NAME]

Why this exists: the shell is built on a Mac and gets `codesign -s -` there, but the 41 MB
Guest.framework/Guest dynamic library is produced on the player's own machine by
dm/tools/guest_dylib.py, which only writes segments and load commands. iOS requires every embedded
Mach-O to carry a signature, and signing tools (i4Tools, Sideloadly) refuse a bundle whose
framework has none — i4Tools reports "error 96" and never gets to the app itself.

What this writes, the same three things `codesign -s -` writes:
  * an LC_CODE_SIGNATURE load command (placed in the padding after the existing load commands,
    so nothing in the file moves and every segment offset stays valid)
  * a CodeDirectory: sha256 of every 4 KiB page of the file up to the signature, ad-hoc flagged
  * a SuperBlob wrapping it, appended at the end (__LINKEDIT, which guest_dylib.py puts last,
    is grown to cover it)

The result is a well-formed unsigned app component: iOS accepts it as ad-hoc signed, and any
sideloading tool can replace the signature with a real one.
"""
import struct
import sys

PAGE = 0x1000
CSMAGIC_EMBEDDED_SIGNATURE = 0xFADE0CC0
CSMAGIC_CODEDIRECTORY = 0xFADE0C02
LC_SEGMENT_64 = 0x19
LC_CODE_SIGNATURE = 0x1D
CS_ADHOC = 0x2
PAGESIZE_SHIFT = 12
VERSION = 0x20400
HASH_SIZE = 32
HASH_TYPE_SHA256 = 2


def sign(path, identifier=None):
    b = bytearray(open(path, 'rb').read())
    magic, cpu, sub, ftype, ncmds, sizeofcmds, flags, _ = struct.unpack_from('<IiiIIIII', b, 0)
    if magic != 0xFEEDFACF:
        raise SystemExit('not a 64-bit Mach-O: %#x' % magic)
    if identifier is None:
        identifier = path.replace('\\', '/').split('/')[-1] or 'adhoc'

    # Walk the load commands once: refuse if already signed, remember where __LINKEDIT is so its
    # size can be grown to cover the appended signature.
    off = 32
    linkedit_at = None
    for _ in range(ncmds):
        cmd, size = struct.unpack_from('<II', b, off)
        if cmd == LC_CODE_SIGNATURE:
            raise SystemExit('already has a code signature')
        if cmd == LC_SEGMENT_64:
            segname = bytes(b[off + 8:off + 24]).split(b'\0')[0]
            if segname == b'__LINKEDIT':
                linkedit_at = off
        off += size
    cmds_end = off
    if cmds_end + 16 > PAGE:
        raise SystemExit('not enough room for LC_CODE_SIGNATURE (cmds end %d)' % cmds_end)

    # Signature data goes right after the file, padded to 16 bytes as codesign does.
    dataoff = (len(b) + 15) & ~15
    b.extend(b'\0' * (dataoff - len(b)))

    # CodeDirectory size first (its length field feeds the SuperBlob length).
    ident = identifier.encode() + b'\0'
    ident_off = 0x58
    hash_off = (ident_off + len(ident) + 15) & ~15
    n_code = (dataoff + PAGE - 1) // PAGE
    cd_len = hash_off + n_code * HASH_SIZE

    # Now the load command, with the real signature size.
    datasize = 12 + 8 + cd_len            # SuperBlob header + one index entry + CodeDirectory
    struct.pack_into('<IIII', b, cmds_end, LC_CODE_SIGNATURE, 16, dataoff, datasize)
    struct.pack_into('<I', b, 0x10, ncmds + 1)
    struct.pack_into('<I', b, 0x14, sizeofcmds + 16)

    # Grow __LINKEDIT so it covers the signature (codesign does the same). This has to happen
    # BEFORE the page hashes are computed: the __LINKEDIT load command lives in the first page,
    # so touching it afterwards invalidates that page's hash.
    final_len = dataoff + datasize
    if linkedit_at is not None:
        # segment_command_64: +24 vmaddr, +32 vmsize, +40 fileoff, +48 filesize
        addr, vmsize, fileoff, filesize = struct.unpack_from('<QQQQ', b, linkedit_at + 24)
        new_filesize = final_len - fileoff
        new_vmsize = (new_filesize + PAGE - 1) & ~(PAGE - 1)
        struct.pack_into('<Q', b, linkedit_at + 48, new_filesize)
        struct.pack_into('<Q', b, linkedit_at + 32, new_vmsize)

    # Page hashes over [0, dataoff) — this now includes the new load command and the __LINKEDIT
    # adjustment, which is why both are written before hashing.
    import hashlib
    hashes = []
    for i in range(n_code):
        chunk = bytes(b[i * PAGE:min((i + 1) * PAGE, dataoff)])
        hashes.append(hashlib.sha256(chunk).digest())
    if len(hashes[-1]) != HASH_SIZE:
        raise SystemExit('internal: bad page hash')

    cd = bytearray(cd_len)
    struct.pack_into('<IIIIIIIII', cd, 0,
                     CSMAGIC_CODEDIRECTORY, cd_len, VERSION, CS_ADHOC,
                     hash_off, ident_off, 0, n_code, dataoff)
    cd[0x24] = HASH_SIZE
    cd[0x25] = HASH_TYPE_SHA256
    cd[0x26] = 0
    cd[0x27] = PAGESIZE_SHIFT
    struct.pack_into('<I', cd, 0x28, 0)          # spare2
    struct.pack_into('<I', cd, 0x2C, 0)          # scatterOffset
    struct.pack_into('<I', cd, 0x30, 0)          # teamOffset
    struct.pack_into('<I', cd, 0x34, 0)          # spare3
    struct.pack_into('<Q', cd, 0x38, 0)          # codeLimit64
    struct.pack_into('<Q', cd, 0x40, 0)          # execSegBase
    struct.pack_into('<Q', cd, 0x48, 0)          # execSegLimit
    struct.pack_into('<Q', cd, 0x50, 0)          # execSegFlags
    cd[ident_off:ident_off + len(ident)] = ident
    for i, h in enumerate(hashes):
        cd[hash_off + i * HASH_SIZE:hash_off + (i + 1) * HASH_SIZE] = h

    # SuperBlob: header, index (one entry, type 0 = CodeDirectory), then the blob
    blob = bytearray(12 + 8 + cd_len)
    struct.pack_into('>III', blob, 0, CSMAGIC_EMBEDDED_SIGNATURE, len(blob), 1)
    struct.pack_into('>II', blob, 12, 0, 20)
    blob[20:20 + cd_len] = cd

    b.extend(blob)

    open(path, 'wb').write(b)
    return {'identifier': identifier, 'pages': n_code, 'dataoff': dataoff,
            'datasize': len(blob), 'total': len(b)}


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    ident = None
    args = sys.argv[1:]
    if '--identifier' in args:
        i = args.index('--identifier')
        ident = args[i + 1]
        del args[i:i + 2]
    r = sign(args[0], ident)
    print('signed %s: %d pages, signature %d bytes at %d, file now %d'
          % (r['identifier'], r['pages'], r['datasize'], r['dataoff'], r['total']))
