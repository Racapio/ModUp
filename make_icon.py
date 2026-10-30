"""Иконка приложения (modup.ico) без сторонних библиотек: скруглённый квадрат с градиентом и буквой M."""
import struct, zlib

def png(size):
    px = []
    r0, g0, b0 = 0x6a, 0xa8, 0xff
    r1, g1, b1 = 0x8a, 0x7b, 0xff
    rad = size * 0.22
    # буква M — толщина ~ size/7
    t = max(2, size // 7)
    m_l, m_r, m_t, m_b = int(size * .24), int(size * .76), int(size * .28), int(size * .74)
    for y in range(size):
        row = []
        for x in range(size):
            # скругление углов
            cx = min(x, size - 1 - x); cy = min(y, size - 1 - y)
            inside = not (cx < rad and cy < rad and (rad - cx) ** 2 + (rad - cy) ** 2 > rad ** 2)
            if not inside:
                row += [0, 0, 0, 0]; continue
            k = (x + y) / (2 * size)
            r, g, b = int(r0 + (r1 - r0) * k), int(g0 + (g1 - g0) * k), int(b0 + (b1 - b0) * k)
            inm = False
            if m_t <= y <= m_b:
                if m_l <= x < m_l + t or m_r - t < x <= m_r:
                    inm = True
                mid = (m_l + m_r) / 2
                depth = (y - m_t) / max(1, (m_b - m_t) * .55)
                if depth <= 1:
                    lx = m_l + (mid - m_l) * depth; rx = m_r - (m_r - mid) * depth
                    if abs(x - lx) < t * .75 or abs(x - rx) < t * .75:
                        inm = True
            if inm:
                r, g, b = 0x0b, 0x0e, 0x12
            row += [r, g, b, 255]
        px.append(bytes([0] + row))
    raw = zlib.compress(b"".join(px), 9)
    def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")

sizes = [16, 32, 48, 256]
imgs = [png(s) for s in sizes]
head = struct.pack("<HHH", 0, 1, len(sizes))
off = 6 + 16 * len(sizes)
ent = b""
for s, d in zip(sizes, imgs):
    ent += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(d), off); off += len(d)
open("modup.ico", "wb").write(head + ent + b"".join(imgs))
open("ui/icon.png", "wb").write(png(64))
print("ok")
