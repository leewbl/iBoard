import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from workbench.visual_pixels import VisualPixelError, compare


def chunk(name, payload):
    return struct.pack('>I', len(payload)) + name + payload + struct.pack('>I', zlib.crc32(name + payload))


def png(width, height, raw, *, color=2, depth=8, interlace=0, ancillary=b'', split=False):
    header = struct.pack('>IIBBBBB', width, height, depth, color, 0, 0, interlace)
    encoded = zlib.compress(raw)
    streams = (encoded[:4], encoded[4:]) if split else (encoded,)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) + ancillary
            + b''.join(chunk(b'IDAT', payload) for payload in streams) + chunk(b'IEND', b''))


def filtered_rows(rows, filters, channels):
    output = bytearray()
    previous = bytes(len(rows[0]))
    for row, mode in zip(rows, filters):
        output.append(mode)
        for i, value in enumerate(row):
            up = previous[i]
            left = row[i - channels] if i >= channels else 0
            diagonal = previous[i - channels] if i >= channels else 0
            prediction = left + up - diagonal
            paeth = min((left, up, diagonal), key=lambda callback: abs(callback - prediction))
            output.append((value - (0, left, up, (left + up) // 2, paeth)[mode]) % 256)
        previous = row
    return output


class VisualPixelTests(unittest.TestCase):
    def test_compares_decoded_pixels_across_formats_filters_and_chunks(self):
        rgb = [bytes((7 * i + n) % 256 for n in (0, 20, 80, 173, 193, 235)) for i in range(5)]
        rgba = [r[:3] + b'\xff' + r[3:] + b'\xff' for r in rgb]
        first = png(2, 5, filtered_rows(rgb, [0, 1, 2, 3, 4], 3), split=True,
                    ancillary=chunk(b'tEXt', b'comment\x00different encoding'))
        second = png(2, 5, filtered_rows(rgba, [4, 3, 2, 1, 0], 4), color=6)
        with tempfile.TemporaryDirectory() as directory:
            diff = Path(directory) / 'delta.png'
            result = compare(first, second, diff_path=diff)
            self.assertNotEqual(first, second)
            self.assertEqual(result, {'baseline_dimensions': [2, 5], 'candidate_dimensions': [2, 5],
                                      'equal': True, 'changed_pixels': 0, 'bounding_box': None})
            self.assertFalse(diff.exists())

    def test_reports_changed_pixels_and_exact_bounds_and_writes_a_valid_mask(self):
        before = png(3, 2, (b'\x00' + b'\xff\x00\x00\xff' * 3) * 2, color=6)
        after = png(3, 2, b'\x00' + b'\xff\x00\x00\xff' * 2 + b'\xff\x00\x00\x00'
                    + b'\x00' + b'\x00\x00\x00\xff' + b'\xff\x00\x00\xff' * 2, color=6)
        with tempfile.TemporaryDirectory() as directory:
            diff = Path(directory) / 'delta.png'
            result = compare(before, after, diff_path=diff)
            self.assertFalse(result['equal'])
            self.assertEqual(result['changed_pixels'], 2)
            self.assertEqual(result['bounding_box'], [0, 0, 3, 2])
            expected = png(3, 2, b'\x00\x20\x00\x80', color=0, depth=1)
            self.assertTrue(compare(expected, diff.read_bytes())['equal'])
            with self.assertRaises(FileExistsError):
                compare(before, after, diff_path=diff)
            self.assertTrue(compare(expected, diff.read_bytes())['equal'])

    def test_dimensions_alone_fail_and_do_not_create_a_mask(self):
        a = png(1, 1, b'\x00\x00\x00\x00')
        b = png(2, 1, b'\x00' * 7)
        with tempfile.TemporaryDirectory() as directory:
            diff = Path(directory) / 'delta.png'
            self.assertEqual(compare(a, b, diff_path=diff),
                             {'baseline_dimensions': [1, 1], 'candidate_dimensions': [2, 1],
                              'equal': False, 'changed_pixels': None, 'bounding_box': None})
            self.assertFalse(diff.exists())

    def test_indexed_transparency_and_rgb_transparent_samples_are_normalized(self):
        palette = chunk(b'PLTE', b'\xff\x00\x00\x00\xff\x00')
        transparency = chunk(b'tRNS', b'\x00')
        indexed = png(3, 1, b'\x00\x40', color=3, depth=1, ancillary=palette + transparency)
        rgb = png(3, 1, b'\x00\xff\x00\x00\x00\xff\x00\xff\x00\x00',
                  ancillary=chunk(b'tRNS', struct.pack('>HHH', 255, 0, 0)))
        rgba = png(3, 1, b'\x00\xff\x00\x00\x00\x00\xff\x00\xff\xff\x00\x00\x00', color=6)
        self.assertTrue(compare(indexed, rgba)['equal'])
        self.assertTrue(compare(rgb, rgba)['equal'])
        with self.assertRaisesRegex(VisualPixelError, 'missing palette'):
            compare(png(1, 1, b'\x00\x80', color=3, depth=1,
                        ancillary=chunk(b'PLTE', b'\xff\x00\x00')), rgba)

    def test_will_not_compare_corrupt_or_unbounded_png_scanlines(self):
        valid = png(1, 1, b'\x00' * 4)
        bad = {'truncated': valid[:-1], 'scanline overrun': png(1, 1, b'\x00' * 500),
               'scanline underrun': png(1, 1, b'\x00' * 3), 'invalid filter': png(1, 1, b'\x05\x00\x00\x00'),
               'interlaced': png(1, 1, b'\x00' * 4, interlace=1),
               'excessive dimensions': png(10000, 10000, b'\x00'),
               'animation': png(1, 1, b'\x00' * 4, ancillary=chunk(b'acTL', struct.pack('>II', 2, 0)))}
        for name, image in bad.items():
            with self.subTest(name=name), self.assertRaises(VisualPixelError):
                compare(image, image)
