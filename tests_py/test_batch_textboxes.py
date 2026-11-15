"""Self-contained regression tests for editable PDF text and downloadable conversion bundles."""
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from pypdf import PdfReader, PdfWriter

from slai.errors import ValidationFailed
from slai.docmodel.convert import ConvertOptions, convert_file
from slai.services import JobControl
from slai.output_bundle import artifacts, bundle
from slai.pdfops import textboxes
from slai.pdfops.render import render_page


class EditableTextBoxTests(unittest.TestCase):
    def test_saved_pdf_keeps_identity_content_and_position_after_moving(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'edited.pdf'
            writer = PdfWriter()
            writer.add_blank_page(300, 500)
            ident = textboxes.add(writer, 0, [0.1, 0.15, 0.6, 0.3], 'Movable text', 15, '#1256ab')
            writer.write(path)
            first = PdfReader(path)
            saved = textboxes.listing(first)
            self.assertEqual(saved[0]['id'], ident)
            self.assertEqual(saved[0]['text'], 'Movable text')
            self.assertAlmostEqual(saved[0]['rect'][0], 0.1)
            ann = first.pages[0]['/Annots'][0].get_object()
            self.assertIn('/N', ann['/AP'])
            self.assertGreater(len(ann['/AP']['/N'].get_object().get_data()), 20)
            from PIL import Image
            bitmap, _, _ = render_page(path, 0, width_px=600)
            image = Image.open(io.BytesIO(bitmap)).convert('RGB')
            region = image.crop((60, 150, 360, 300))
            self.assertGreater(sum(1 for r, g, b in region.getdata() if min(r, g, b) < 140), 20,
                               'Saved text must be visibly rendered, not only stored as metadata')

            writer = PdfWriter(clone_from=first)
            self.assertTrue(textboxes.move(writer, ident, [0.25, 0.4, 0.75, 0.55]))
            self.assertTrue(textboxes.edit(writer, ident, 'Edited again'))
            writer.write(path)
            second = PdfReader(path)
            moved = textboxes.listing(second)[0]
            self.assertEqual(moved['text'], 'Edited again')
            for actual, expected in zip(moved['rect'], [0.25, 0.4, 0.75, 0.55]):
                self.assertAlmostEqual(actual, expected, places=4)
            self.assertEqual(moved['color'], '#1256ab')
            self.assertEqual(moved['size'], 15)

    def test_rotated_cropbox_coordinates_and_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'turned.pdf'
            w = PdfWriter()
            p = w.add_blank_page(300, 500)
            p.cropbox.lower_left = (20, 40)
            p.cropbox.upper_right = (280, 460)
            p.rotate(90)
            ident = textboxes.add(w, 0, [0.2, 0.1, 0.65, 0.25], 'Rotated page', 12)
            w.write(path)
            listed = textboxes.listing(PdfReader(path))[0]
            self.assertEqual(listed['id'], ident)
            for a, b in zip(listed['rect'], [0.2, 0.1, 0.65, 0.25]):
                self.assertAlmostEqual(a, b, places=5)
            w2 = PdfWriter(clone_from=PdfReader(path))
            textboxes.move(w2, ident, [0.05, 0.7, 0.5, 0.85])
            w2.write(path)
            for a, b in zip(textboxes.listing(PdfReader(path))[0]['rect'], [0.05, 0.7, 0.5, 0.85]):
                self.assertAlmostEqual(a, b, places=5)

    def test_appearance_is_drawn_inside_display_rect_under_all_page_rotations(self):
        from PIL import Image
        for angle in (0, 90, 180, 270):
            with self.subTest(rotation=angle), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'appearance.pdf'
                writer = PdfWriter()
                page = writer.add_blank_page(350, 500)
                if angle:
                    page.rotate(angle)
                textboxes.add(writer, 0, [0.2, 0.1, 0.7, 0.3], 'Red label', 16, '#ee0000')
                writer.write(path)
                png, _, _ = render_page(path, 0, width_px=700)
                img = Image.open(io.BytesIO(png)).convert('RGB')
                x0, y0, x1, y1 = int(img.width * .2), int(img.height * .1), int(img.width * .7), int(img.height * .3)
                region = img.crop((x0, y0, x1, y1))
                reds = sum(1 for r, g, b in region.getdata() if r > 150 and g < 100 and b < 100)
                self.assertGreater(reds, 15, f'Rotated {angle}° text not painted inside its visible box')

    def test_invalid_and_foreign_text_cannot_be_moved(self):
        w = PdfWriter()
        w.add_blank_page(300, 500)
        ident = textboxes.add(w, 0, [0.1, 0.1, 0.5, 0.4], 'Valid')
        with self.assertRaises(ValidationFailed):
            textboxes.move(w, 'foreign-annotation', [0.1, 0.2, 0.5, 0.4])
        with self.assertRaises(ValidationFailed):
            textboxes.move(w, ident, [float('nan'), 0, 0.5, 0.5])
        with self.assertRaises(ValidationFailed):
            textboxes.add(w, 0, [0, 0, 1, 1], '')


class BundleTests(unittest.TestCase):
    def test_image_to_pdf_and_multipage_pdf_to_separate_images(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); out_dir = root / 'output'; out_dir.mkdir()
            original = root / 'drawing.png'
            Image.new('RGB', (120, 80), '#dd3344').save(original)
            service = SimpleNamespace(settings=SimpleNamespace(max_pdf_pages=20))
            pdf = convert_file(service, original, original.name, 'png', ConvertOptions(target='pdf'), out_dir, JobControl())
            self.assertTrue(pdf['verification']['ok'])
            self.assertEqual(len(PdfReader(pdf['output']['path']).pages), 1)
            multipage = root / 'twopage.pdf'
            writer = PdfWriter(); writer.add_blank_page(300, 400); writer.add_blank_page(400, 300)
            writer.write(multipage)
            pictures = convert_file(service, multipage, multipage.name, 'pdf', ConvertOptions(target='png'), out_dir, JobControl())
            self.assertEqual(len(pictures['output']['extras']), 1)
            with Image.open(pictures['output']['path']) as first, Image.open(pictures['output']['extras'][0]) as second:
                self.assertEqual(first.format, 'PNG')
                self.assertEqual(second.format, 'PNG')
                self.assertNotEqual(first.size, second.size)

    def test_same_format_can_be_explicitly_retained_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'notes.txt'; source.write_bytes(b'Same format without lossy conversion\n')
            out_dir = root / 'out'; out_dir.mkdir()
            report = convert_file(None, source, source.name, 'txt',
                                  ConvertOptions.from_dict({'target': 'txt', 'retain_same_format': True}), out_dir, JobControl())
            self.assertTrue(report['verification']['ok'])
            self.assertEqual(Path(report['output']['path']).read_bytes(), source.read_bytes())
            with self.assertRaises(ValidationFailed):
                convert_file(None, source, source.name, 'txt', ConvertOptions(target='txt'), out_dir)

    def test_zip_keeps_distinct_names_and_supporting_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            left = root / 'one'; right = root / 'two'; left.mkdir(); right.mkdir()
            (left / 'report.txt').write_text('A', encoding='utf-8')
            (right / 'report.txt').write_text('B', encoding='utf-8')
            (left / 'appendix.txt').write_text('extra', encoding='utf-8')
            outs = {
                'a': SimpleNamespace(id='a', path=left / 'report.txt', name='report.txt', extras=['appendix.txt']),
                'b': SimpleNamespace(id='b', path=right / 'report.txt', name='report.txt', extras=[]),
            }
            ws = SimpleNamespace(output=lambda key: outs[key], output_dir=lambda: root,
                                 register_output=lambda p, fmt: SimpleNamespace(path=p, format=fmt, name=p.name))
            z = bundle(ws, ['a', 'b'])
            with zipfile.ZipFile(z.path) as archive:
                names = archive.namelist()
                self.assertEqual(len(names), len(set(names)))
                self.assertEqual(archive.read('report.txt'), b'A')
                self.assertEqual(archive.read('report (1).txt'), b'B')
                self.assertIn('extra', [archive.read(n).decode('utf-8') for n in names])

    def test_outside_extra_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); (base / 'file.txt').write_text('yes')
            out = SimpleNamespace(path=base / 'file.txt', name='file.txt', extras=['../escaped.txt'])
            with self.assertRaises(ValidationFailed):
                artifacts(out)


if __name__ == '__main__':
    unittest.main()
