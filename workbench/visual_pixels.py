"""Compare decoded screenshot samples without a color or pixel tolerance."""

from dataclasses import dataclass
import os
import re
import struct
import zlib

from runner.sandbox import MAX_SCREENSHOT, SandboxError, _validated_png


_SIGNATURE = b'\x89PNG\r\n\x1a\n'
_FORMATS = {0: (1, (1, 2, 4, 8)), 2: (3, (8,)), 3: (1, (1, 2, 4, 8)),
            4: (2, (8,)), 6: (4, (8,))}


class VisualPixelError(ValueError):
    """A screenshot is invalid or uses an unsupported pixel representation."""


@dataclass
class _Image:
    width: int
    height: int
    depth: int
    color: int
    channels: int
    compressed: bytes
    palette: tuple = ()
    transparent: bytes = b''

    def rows(self):
        stride = (self.width * self.depth * self.channels + 7) // 8
        expected = self.height * (stride + 1)
        decoder = zlib.decompressobj()
        try:
            raw = decoder.decompress(self.compressed, expected + 1)
        except zlib.error as error:
            raise VisualPixelError('invalid PNG compressed data') from error
        if len(raw) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise VisualPixelError('PNG scanlines do not match the declared dimensions')
        previous = bytearray(stride)
        bytes_per_pixel = max(1, (self.channels * self.depth + 7) // 8)
        for start in range(0, expected, stride + 1):
            row = bytearray(raw[start + 1:start + stride + 1])
            _unfilter(row, previous, raw[start], bytes_per_pixel)
            yield self.rgba(row)
            previous = row

    def rgba(self, row):
        if self.color == 6:
            return row
        result = bytearray(self.width * 4)
        if self.color == 3:
            try:
                return b''.join(self.palette[index] for index in self.samples(row))
            except IndexError as error:
                raise VisualPixelError('PNG references a missing palette entry') from error
        if self.color == 2:
            for channel in range(3):
                result[channel::4] = row[channel::3]
            result[3::4] = b'\xff' * self.width
            if self.transparent:
                for x in range(self.width):
                    if row[x * 3:x * 3 + 3] == self.transparent:
                        result[x * 4 + 3] = 0
            return result
        if self.color == 4:
            gray, alpha = row[::2], row[1::2]
        else:
            levels = (1 << self.depth) - 1
            gray = bytes(value * 255 // levels for value in self.samples(row))
            key = self.transparent
            alpha = bytes(0 if key and value == key[0] else 255 for value in gray)
        for channel in range(3):
            result[channel::4] = gray
        result[3::4] = alpha
        return result

    def samples(self, row):
        if self.depth == 8:
            return iter(row)
        mask = (1 << self.depth) - 1
        return ((row[bit // 8] >> (8 - self.depth - bit % 8)) & mask
                for bit in range(0, self.width * self.depth, self.depth))


def _paeth(left, up, diagonal):
    prediction = left + up - diagonal
    a, b, c = abs(prediction - left), abs(prediction - up), abs(prediction - diagonal)
    if a <= b and a <= c:
        return left
    return up if b <= c else diagonal


def _unfilter(row, previous, mode, channels):
    if mode == 0:
        return
    if mode == 1:
        for i in range(channels, len(row)):
            row[i] = (row[i] + row[i - channels]) & 255
    elif mode == 2:
        for i, value in enumerate(previous):
            row[i] = (row[i] + value) & 255
    elif mode in (3, 4):
        for i, value in enumerate(previous):
            left = row[i - channels] if i >= channels else 0
            if mode == 3:
                predictor = (left + value) // 2
            else:
                predictor = _paeth(left, value, previous[i - channels] if i >= channels else 0)
            row[i] = (row[i] + predictor) & 255
    else:
        raise VisualPixelError('invalid PNG scanline filter')


def _read(data):
    if type(data) is not bytes:
        raise VisualPixelError('expected PNG bytes')
    try:
        _validated_png(data)
    except SandboxError as error:
        raise VisualPixelError(str(error)) from error
    width, height, depth, color, compression, filtering, interlace = struct.unpack_from('>IIBBBBB', data, 16)
    channels, depths = _FORMATS.get(color, (0, ()))
    if depth not in depths or compression or filtering or interlace:
        raise VisualPixelError('unsupported PNG pixel format or interlace')
    palette, transparency = None, None
    compressed = bytearray()
    seen_image = past_image = False
    pos = 33
    while pos < len(data):
        length, chunk = struct.unpack_from('>I4s', data, pos)
        payload = memoryview(data)[pos + 8:pos + 8 + length]
        pos += 12 + length
        if (not re.fullmatch(rb'[A-Za-z]{2}[A-Z][A-Za-z]', chunk) or
                chunk in (b'acTL', b'fcTL', b'fdAT')):
            raise VisualPixelError('invalid PNG chunk or unsupported animation')
        if chunk == b'IDAT':
            if past_image:
                raise VisualPixelError('PNG image data must be contiguous')
            seen_image = True
            compressed.extend(payload)
            continue
        past_image = seen_image
        if chunk == b'PLTE':
            if (past_image or palette is not None or transparency is not None or color in (0, 4)
                    or not length or length % 3 or length > 768 or (color == 3 and length // 3 > 1 << depth)):
                raise VisualPixelError('invalid PNG palette')
            palette = bytes(payload)
        elif chunk == b'tRNS':
            if past_image or transparency is not None:
                raise VisualPixelError('invalid PNG transparency ordering')
            transparency = bytes(payload)
        elif chunk == b'IEND':
            break
        elif not chunk[0] & 32:
            raise VisualPixelError('unexpected critical PNG chunk')
    key = b''
    entries = ()
    if color == 3:
        if palette is None or (transparency is not None and not 1 <= len(transparency) <= len(palette) // 3):
            raise VisualPixelError('invalid indexed PNG palette or transparency')
        alpha = (transparency or b'').ljust(len(palette) // 3, b'\xff')
        entries = tuple(palette[i * 3:i * 3 + 3] + bytes((value,)) for i, value in enumerate(alpha))
    elif transparency is not None:
        expected = {0: 2, 2: 6}.get(color)
        if expected != len(transparency):
            raise VisualPixelError('invalid PNG transparent sample')
        samples = struct.unpack('>' + 'H' * (len(transparency) // 2), transparency)
        levels = (1 << depth) - 1
        if any(sample > levels for sample in samples):
            raise VisualPixelError('PNG transparent sample is out of range')
        key = bytes(sample * 255 // levels for sample in samples)
    return _Image(width, height, depth, color, channels, bytes(compressed), entries, key)


def _chunk(kind, payload):
    return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload))


def normalize(data: bytes, *, max_pixels: int):
    """Encode the decoded samples as a static RGBA PNG without ancillary chunks."""
    image = _read(data)
    if image.width * image.height > max_pixels:
        raise VisualPixelError('PNG exceeds the pixel budget')
    encoder = zlib.compressobj()
    chunks = [encoder.compress(b'\0' + row) for row in image.rows()]
    chunks.append(encoder.flush())
    header = struct.pack('>IIBBBBB', image.width, image.height, 8, 6, 0, 0, 0)
    png = _SIGNATURE + _chunk(b'IHDR', header) + _chunk(b'IDAT', b''.join(chunks)) + _chunk(b'IEND', b'')
    if len(png) > MAX_SCREENSHOT:
        raise VisualPixelError('normalized PNG exceeds the screenshot size limit')
    return png


def _write_mask(path, width, height, compressed):
    header = struct.pack('>IIBBBBB', width, height, 1, 0, 0, 0, 0)
    png = _SIGNATURE + _chunk(b'IHDR', header) + _chunk(b'IDAT', compressed) + _chunk(b'IEND', b'')
    if len(png) > MAX_SCREENSHOT:
        raise VisualPixelError('pixel mask is too large')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(png)
    except BaseException:
        os.unlink(path)
        raise


def compare(before: bytes, after: bytes, *, diff_path=None):
    """Compare exact RGBA samples and optionally write a white-on-black change mask.

    Bounding boxes use zero-based coordinates and exclusive right/bottom edges.
    A size mismatch fails independently of the samples and has no count or mask.
    """
    left = _read(before)
    right = _read(after)
    dimensions = [left.width, left.height], [right.width, right.height]
    result = {'baseline_dimensions': dimensions[0], 'candidate_dimensions': dimensions[1],
              'equal': False, 'changed_pixels': None, 'bounding_box': None}
    if dimensions[0] != dimensions[1] or before == after:
        for _ in left.rows():
            pass
        if dimensions[0] != dimensions[1]:
            for _ in right.rows():
                pass
        else:
            result.update(equal=True, changed_pixels=0)
        return result

    mask_writer = zlib.compressobj() if diff_path is not None else None
    encoded = []
    min_x, min_y, max_x, max_y = left.width, left.height, 0, 0
    count = 0
    for y, (a, b) in enumerate(zip(left.rows(), right.rows())):
        mask_row = bytearray(1 + (left.width + 7) // 8) if mask_writer is not None else None
        if a != b:
            for x, (old, new) in enumerate(zip(memoryview(a).cast('I'), memoryview(b).cast('I'))):
                if old == new:
                    continue
                count += 1
                min_x, max_x = min(min_x, x), max(max_x, x + 1)
                min_y, max_y = min(min_y, y), y + 1
                if mask_row is not None:
                    mask_row[1 + x // 8] |= 1 << (7 - x % 8)
        if mask_writer is not None:
            encoded.append(mask_writer.compress(mask_row))
    result.update(equal=count == 0, changed_pixels=count)
    if count:
        result['bounding_box'] = [min_x, min_y, max_x, max_y]
        if mask_writer is not None:
            encoded.append(mask_writer.flush())
            _write_mask(diff_path, left.width, left.height, b''.join(encoded))
    return result
