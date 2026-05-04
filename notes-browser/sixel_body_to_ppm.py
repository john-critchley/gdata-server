#!/usr/bin/env python3
"""Decode a SIXEL body string (without ESC P q ... ESC \\) to a PPM image.

Supported subset matches the browser emitter:
- Raster attributes: "1;1;<w>;<h>
- Palette defines: #<n>;2;<r>;<g>;<b>
- Color select: #<n>
- RLE: !<count><char>
- Carriage return: $
- New line (next 6-row band): -
- SIXEL data chars: 63..126
"""

import argparse
from pathlib import Path


def _read_int(text, pos):
    start = pos
    while pos < len(text) and text[pos].isdigit():
        pos += 1
    if pos == start:
        return None, start
    return int(text[start:pos]), pos


def decode_sixel_body(sixel):
    palette = {0: (0, 0, 0)}
    pixels = {}
    width_hint = None
    height_hint = None

    x = 0
    y_band = 0
    color = 0
    max_x = -1
    max_y = -1

    i = 0
    n = len(sixel)
    while i < n:
        ch = sixel[i]

        if ch in ('\n', '\r', '\t', ' '):
            i += 1
            continue

        if ch == '"':
            i += 1
            vals = []
            while i < n:
                if sixel[i].isdigit():
                    v, i = _read_int(sixel, i)
                    vals.append(v)
                    continue
                if sixel[i] == ';':
                    i += 1
                    continue
                break
            if len(vals) >= 4:
                width_hint = vals[2]
                height_hint = vals[3]
            continue

        if ch == '#':
            i += 1
            idx, i2 = _read_int(sixel, i)
            if idx is None:
                i += 1
                continue
            i = i2

            if i < n and sixel[i] == ';':
                # Palette definition, expected form: ;2;r;g;b
                params = []
                while i < n and sixel[i] == ';':
                    i += 1
                    v, i2 = _read_int(sixel, i)
                    if v is None:
                        break
                    params.append(v)
                    i = i2
                if len(params) >= 4 and params[0] == 2:
                    r = int(round(max(0, min(100, params[1])) * 255 / 100.0))
                    g = int(round(max(0, min(100, params[2])) * 255 / 100.0))
                    b = int(round(max(0, min(100, params[3])) * 255 / 100.0))
                    palette[idx] = (r, g, b)
            else:
                color = idx
            continue

        if ch == '!':
            i += 1
            count, i2 = _read_int(sixel, i)
            if count is None or i2 >= n:
                continue
            i = i2
            data_ch = sixel[i]
            i += 1
            val = ord(data_ch) - 63
            for _ in range(count):
                if 0 <= val <= 63:
                    for bit in range(6):
                        if val & (1 << bit):
                            yy = y_band + bit
                            pixels[(x, yy)] = color
                            if x > max_x:
                                max_x = x
                            if yy > max_y:
                                max_y = yy
                x += 1
            continue

        if ch == '$':
            x = 0
            i += 1
            continue

        if ch == '-':
            x = 0
            y_band += 6
            i += 1
            continue

        val = ord(ch) - 63
        if 0 <= val <= 63:
            for bit in range(6):
                if val & (1 << bit):
                    yy = y_band + bit
                    pixels[(x, yy)] = color
                    if x > max_x:
                        max_x = x
                    if yy > max_y:
                        max_y = yy
            x += 1
        i += 1

    width = width_hint if width_hint and width_hint > 0 else (max_x + 1 if max_x >= 0 else 1)
    height = height_hint if height_hint and height_hint > 0 else (max_y + 1 if max_y >= 0 else 1)

    # White background for untouched pixels.
    out = bytearray(width * height * 3)
    for yy in range(height):
        for xx in range(width):
            p = (yy * width + xx) * 3
            out[p] = 255
            out[p + 1] = 255
            out[p + 2] = 255

    for (xx, yy), idx in pixels.items():
        if 0 <= xx < width and 0 <= yy < height:
            p = (yy * width + xx) * 3
            r, g, b = palette.get(idx, (0, 0, 0))
            out[p] = r
            out[p + 1] = g
            out[p + 2] = b

    return width, height, out


def write_ppm(path, width, height, rgb):
    header = f"P6\n{width} {height}\n255\n".encode('ascii')
    Path(path).write_bytes(header + bytes(rgb))


def main():
    parser = argparse.ArgumentParser(description='Decode SIXEL body text to PPM')
    parser.add_argument('input', help='Input file containing SIXEL body text')
    parser.add_argument('output', help='Output PPM path')
    args = parser.parse_args()

    sixel = Path(args.input).read_text(encoding='utf-8')
    w, h, rgb = decode_sixel_body(sixel)
    write_ppm(args.output, w, h, rgb)
    print(f'wrote {args.output} ({w}x{h})')


if __name__ == '__main__':
    main()
