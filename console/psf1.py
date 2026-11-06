import struct, gzip

PSF1_MAGIC = b'\x36\x04'
SEP, TERM = 0xFFFE, 0xFFFF

def load(path):
    d = gzip.open(path,'rb').read() if path.endswith('.gz') else open(path,'rb').read()
    assert d[:2] == PSF1_MAGIC, "not PSF1"
    mode, charsize = d[2], d[3]
    nglyphs = 512 if (mode & 0x01) else 256
    has_tab = bool(mode & 0x06)
    off = 4
    glyphs = [bytearray(d[off+i*charsize: off+(i+1)*charsize]) for i in range(nglyphs)]
    off += nglyphs*charsize
    # unicode table: list per glyph of (list of single cps, list of sequences)
    singles = [[] for _ in range(nglyphs)]
    seqs    = [[] for _ in range(nglyphs)]
    if has_tab:
        vals = struct.unpack(f'<{(len(d)-off)//2}H', d[off: off + ((len(d)-off)//2)*2])
        gi, cur, inseq = 0, [], False
        for v in vals:
            if v == TERM:
                if inseq and cur: seqs[gi].append(cur)
                elif cur: singles[gi].extend(cur)
                gi += 1; cur = []; inseq = False
                if gi >= nglyphs: break
            elif v == SEP:
                if inseq and cur: seqs[gi].append(cur)
                elif cur: singles[gi].extend(cur)
                cur = []; inseq = True
            else:
                cur.append(v)
    return dict(mode=mode, charsize=charsize, nglyphs=nglyphs,
                glyphs=glyphs, singles=singles, seqs=seqs)

def save(f, path):
    out = bytearray(PSF1_MAGIC)
    out += bytes([f['mode'], f['charsize']])
    for g in f['glyphs']:
        g = bytes(g)[:f['charsize']].ljust(f['charsize'], b'\x00')
        out += g
    if f['mode'] & 0x06:
        for i in range(f['nglyphs']):
            for cp in f['singles'][i]:
                out += struct.pack('<H', cp)
            for s in f['seqs'][i]:
                out += struct.pack('<H', SEP)
                for cp in s: out += struct.pack('<H', cp)
            out += struct.pack('<H', TERM)
    data = bytes(out)
    if path.endswith('.gz'):
        with gzip.GzipFile(path, 'wb', mtime=0) as fh: fh.write(data)
    else:
        open(path,'wb').write(data)
    return len(data)

def cpmap(f):
    m = {}
    for i, cps in enumerate(f['singles']):
        for cp in cps: m.setdefault(cp, i)
    return m
