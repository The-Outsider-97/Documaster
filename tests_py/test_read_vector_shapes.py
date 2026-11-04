"""Native DrawingML shape import, export and editable SLDC round-trip tests."""
from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from slai.docmodel.read_vector_shapes import parse_shape, shapes_in_paragraph, SHAPE_KEY
from slai.docmodel.writers.vector_shapes import add_docx_shape, _WORD_PRESETS
from slai.docmodel.writers.common import PageSetup


def shape(kind="star"):
    return {
        "version": 1, "kind": kind, "width_mm": 60, "height_mm": 40,
        "rotation_deg": 18.5, "opacity": 1,
        "fill": {"enabled": True, "color": "#11aa33", "opacity": .65},
        "outline": {"enabled": True, "color": "#aa1133", "opacity": .95,
                    "width_pt": 2, "style": "dashed"},
        "corners_mm": [0, 0, 0, 0],
        "text": {"content": "hello shape", "color": "#202020",
                 "font_size_pt": 12, "bold": False, "italic": False,
                 "align": "center", "padding_mm": 2, "fit": "keep",
                 "spans": [{"type": "format", "start": 0, "end": 5, "bold": True}]},
        "shadows": [],
        "layout": {"mode": "inline"}
    }


class DrawingMLRoundTrip(unittest.TestCase):
    def test_all_supported_word_geometry_presets(self):
        try:
            from docx import Document
        except ImportError:
            self.skipTest("python-docx unavailable")
        for kind in _WORD_PRESETS:
            with self.subTest(kind=kind):
                document = Document()
                add_docx_shape(document,{"extensions":{SHAPE_KEY:shape(kind)}},PageSetup())
                found, notes = shapes_in_paragraph(document.paragraphs[-1])
                self.assertEqual(len(found),1,notes)
                self.assertEqual(found[0]["kind"],kind)
                self.assertAlmostEqual(found[0]["width_mm"],60,places=2)
                self.assertAlmostEqual(found[0]["rotation_deg"],18.5,places=1)
                self.assertEqual(found[0]["text"]["content"],"hello shape")
                self.assertEqual(found[0]["text"]["spans"][0]["bold"],True)

    def test_reimport_preserves_floating_anchor_and_zorder(self):
        try:
            from docx import Document
        except ImportError:
            self.skipTest("python-docx unavailable")
        s=shape("pentagon")
        s["layout"]={"mode":"floating","anchor":"page","x_mm":12.5,"y_mm":30,
                     "wrap":"none","layer":"in_front_of_text"}
        s["z_index"]=3
        document=Document()
        add_docx_shape(document,{"extensions":{SHAPE_KEY:s}},PageSetup())
        found,warnings=shapes_in_paragraph(document.paragraphs[-1],25,25)
        self.assertFalse(warnings)
        self.assertEqual(len(found),1)
        layout=found[0]["layout"]
        self.assertEqual(layout["mode"],"floating")
        self.assertAlmostEqual(layout["x_mm"],12.5,places=2)
        self.assertAlmostEqual(layout["y_mm"],30,places=2)
        self.assertEqual(found[0]["z_index"],3)

    def test_document_reader_returns_editable_shape_carriers(self):
        try:
            from docx import Document
            from slai.docmodel.read_docx import read_docx
        except ImportError:
            self.skipTest("python-docx unavailable")
        document=Document()
        add_docx_shape(document,{"extensions":{SHAPE_KEY:shape()}},PageSetup())
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"editable.docx"
            document.save(path)
            result=read_docx(str(path))
        blocks=[b for b in result["blocks"] if SHAPE_KEY in b.get("extensions",{})]
        self.assertEqual(len(blocks),1)
        self.assertEqual(blocks[0]["type"],"paragraph")
        self.assertEqual(blocks[0]["text"],"")
        self.assertEqual(blocks[0]["extensions"][SHAPE_KEY]["kind"],"star")

    def test_unknown_geometry_is_not_invented(self):
        try:
            from docx import Document
        except ImportError:
            self.skipTest("python-docx unavailable")
        document=Document()
        add_docx_shape(document,{"extensions":{SHAPE_KEY:shape()}},PageSetup())
        el=document.paragraphs[-1]._p
        ns={"a":"http://schemas.openxmlformats.org/drawingml/2006/main"}
        found=el.xpath(".//a:prstGeom")
        self.assertTrue(found)
        found[0].set("prst","unsupportedMysteryGeometry")
        shapes,notes=shapes_in_paragraph(document.paragraphs[-1])
        self.assertFalse(shapes)
        self.assertTrue(any("Unsupported" in message for message in notes))
