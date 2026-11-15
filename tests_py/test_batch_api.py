"""Real HTTP-contract verification for converted batches and movable text boxes."""
import io
import unittest
import zipfile

from pypdf import PdfReader
from support import ApiError, Harness, make_docx, make_pdf, needs_slai


@needs_slai
class BatchAndTextApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()
        cls.pdf_bytes = make_pdf([['Original content']])
        cls.pdf = cls.h.upload('original.pdf', cls.pdf_bytes)
        cls.docx = cls.h.upload('report.docx', make_docx(['Quarterly report text']))

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_same_format_original_is_retained_when_explicitly_requested(self):
        r = self.h.job('POST', '/api/convert', {'docs': [self.pdf['id']],
            'options': {'target': 'pdf', 'retain_same_format': True}})
        rep = r['results'][0]['report']
        self.assertEqual(rep['engine'], 'copy')
        self.assertEqual(self.h.download(rep['output']), self.pdf_bytes)

    def test_independent_conversions_are_zip_downloadable(self):
        first = self.h.job('POST', '/api/convert', {'docs': [self.docx['id']], 'options': {'target': 'pdf'}})
        second = self.h.job('POST', '/api/convert', {'docs': [self.pdf['id']], 'options': {'target': 'txt'}})
        self.assertEqual(first['succeeded'], 1, first)
        self.assertEqual(second['succeeded'], 1, second)
        ids = [first['results'][0]['report']['output']['id'], second['results'][0]['report']['output']['id']]
        zipped = self.h.call('POST', '/api/outputs/zip', {'outputs': ids})
        with zipfile.ZipFile(io.BytesIO(self.h.download(zipped))) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual({n.rsplit('.', 1)[-1] for n in z.namelist()}, {'pdf', 'txt'})
            self.assertEqual(len(z.namelist()), 2)
        with self.assertRaises(ApiError):
            self.h.call('POST', '/api/outputs/zip', {'outputs': [ids[0], ids[0]]})

    def test_multipage_pdf_images_download_as_individual_files_and_zip(self):
        pdf = self.h.upload('two-pages.pdf', make_pdf([['First image page'], ['Second image page']]))
        converted = self.h.job('POST', '/api/convert', {'docs': [pdf['id']], 'options': {'target': 'png'}})
        self.assertEqual(converted['succeeded'], 1, converted)
        out = converted['results'][0]['report']['output']
        self.assertEqual(len(out['extras']), 1)
        self.assertTrue(self.h.download(out).startswith(b'\x89PNG'))
        status, _, second = self.h.raw('GET', f"/api/outputs/{out['id']}/extras/0/download")
        self.assertEqual(status, 200)
        self.assertTrue(second.startswith(b'\x89PNG'))
        zipped = self.h.call('POST', '/api/outputs/zip', {'outputs': [out['id']]})
        with zipfile.ZipFile(io.BytesIO(self.h.download(zipped))) as z:
            self.assertEqual(len(z.namelist()), 2)

    def test_uploaded_image_converts_to_pdf_through_public_api(self):
        from PIL import Image

        source = io.BytesIO()
        Image.new('RGB', (80, 60), '#1275aa').save(source, format='PNG')
        import json
        status, _, payload = self.h.raw('POST', '/api/documents?name=drawing.png&images=1',
                                        source.getvalue(), {'Content-Type': 'application/octet-stream'})
        self.assertEqual(status, 200, payload)
        uploaded = json.loads(payload)['data']
        converted = self.h.job('POST', '/api/convert', {
            'docs': [uploaded['id']], 'options': {'target': 'pdf'}})
        self.assertEqual(converted['succeeded'], 1, converted)
        result = converted['results'][0]['report']
        self.assertTrue(result['verification']['ok'])
        self.assertEqual(len(PdfReader(io.BytesIO(self.h.download(result['output']))).pages), 1)

    def test_saved_pdf_object_can_move_edit_undo_redo_and_download(self):
        base = f"/api/documents/{self.pdf['id']}"
        added = self.h.job('POST', base + '/op', {'op': 'textbox_add', 'page': 1,
            'rect': [0.1, 0.2, 0.5, 0.32], 'text': 'Editable label', 'size': 14, 'color': '#0033aa'})
        ident = added['details']['id']
        self.assertEqual(len(self.h.call('GET', base + '/pdfinfo')['textboxes']), 1)
        moved = self.h.job('POST', base + '/op', {'op': 'textbox_move', 'id': ident, 'rect': [0.3, 0.4, 0.7, 0.52]})
        self.assertTrue(moved['changed'])
        self.assertAlmostEqual(self.h.call('GET', base + '/pdfinfo')['textboxes'][0]['rect'][0], .3)
        self.h.call('POST', base + '/undo', {})
        self.assertAlmostEqual(self.h.call('GET', base + '/pdfinfo')['textboxes'][0]['rect'][0], .1)
        self.h.call('POST', base + '/redo', {})
        self.assertAlmostEqual(self.h.call('GET', base + '/pdfinfo')['textboxes'][0]['rect'][0], .3)
        self.h.job('POST', base + '/op', {'op': 'textbox_edit', 'id': ident, 'text': 'Changed label'})
        saved = self.h.call('POST', base + '/save', {})
        saved_pdf = self.h.download(saved)
        reader = PdfReader(io.BytesIO(saved_pdf))
        from slai.pdfops.textboxes import listing
        self.assertEqual(listing(reader)[0]['text'], 'Changed label')
        self.assertAlmostEqual(listing(reader)[0]['rect'][0], .3)
        reopened = self.h.upload('reopened.pdf', saved_pdf)
        reopen_base = f"/api/documents/{reopened['id']}"
        self.assertEqual(self.h.call('GET', reopen_base + '/pdfinfo')['textboxes'][0]['id'], ident)
        self.h.job('POST', reopen_base + '/op', {'op': 'textbox_move', 'id': ident,
            'rect': [0.35, 0.4, 0.75, 0.52]})
        self.assertAlmostEqual(self.h.call('GET', reopen_base + '/pdfinfo')['textboxes'][0]['rect'][0], .35)
        with self.assertRaises(ApiError):
            self.h.job('POST', base + '/op', {'op': 'textbox_move', 'id': 'not-a-textbox',
                'rect': [0, 0, 0.5, 0.5]})


if __name__ == '__main__':
    unittest.main()
