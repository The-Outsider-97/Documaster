"""Native Writer vector shapes in PDF and Word DrawingML.

These tests do not depend on SLAI Writer, only on the declarative namespaced
shape extension and the existing third-party writer output modules.
"""
from __future__ import annotations

import io
import unittest

from slai.docmodel.writers.vector_shapes import (SHAPE_KEY, add_docx_shape,
                                                  make_flowable, supported)
from slai.docmodel.writers.common import PageSetup


def sample(kind="star"):
    return {"id":"b1","type":"paragraph","text":"",
            "extensions":{SHAPE_KEY:{
                "version":1,"kind":kind,"width_mm":60,"height_mm":40,
                "rotation_deg":15,"opacity":1,
                "fill":{"enabled":True,"color":"#aabbcc","opacity":.75},
                "outline":{"enabled":True,"color":"#112233","opacity":1,
                           "width_pt":1.5,"style":"dashed"},
                "corners_mm":[0,0,0,0],
                "text":{"content":"hello shadow","color":"#123456","font_size_pt":12,
                        "bold":False,"italic":False,"align":"center","padding_mm":2,
                        "spans":[{"type":"format","start":0,"end":5,"bold":True}]},
                "shadows":[{"enabled":True,"color":"#777777","opacity":.5,
                            "offset_x_mm":5,"offset_y_mm":5,"blur_mm":0,"spread_mm":0}]
            }}}


class NativeShapes(unittest.TestCase):
    def test_ten_shapes_supported(self):
        for kind in ("rectangle","ellipse","line","triangle","diamond",
                     "pentagon","hexagon","star","arrow","plus"):
            self.assertTrue(supported(sample(kind)),kind)

    def test_pdf_is_vector_and_text_searchable(self):
        try:
            from reportlab.platypus import SimpleDocTemplate
        except ImportError:
            self.skipTest("reportlab unavailable")
        target=io.BytesIO()
        flow=make_flowable(sample(),180,PageSetup())
        SimpleDocTemplate(target).build([flow])
        value=target.getvalue()
        self.assertTrue(value.startswith(b"%PDF"))
        try:
            from pypdf import PdfReader
        except ImportError:
            return
        reader=PdfReader(io.BytesIO(value))
        self.assertIn("hello",reader.pages[0].extract_text())

    def test_word_contains_editable_wordprocessing_shape_and_text(self):
        try:
            from docx import Document
        except ImportError:
            self.skipTest("python-docx unavailable")
        doc=Document()
        messages=add_docx_shape(doc,sample(),PageSetup())
        self.assertFalse(messages)
        raw=io.BytesIO()
        doc.save(raw)
        raw.seek(0)
        imported=Document(raw)
        xml=imported.element.body.xml
        self.assertIn("wps:wsp",xml)
        self.assertIn('prst="star5"',xml)
        self.assertIn("hello shadow",xml)

    def test_word_floating_anchor(self):
        try:
            from docx import Document
        except ImportError:
            self.skipTest("python-docx unavailable")
        block=sample("rectangle")
        block["extensions"][SHAPE_KEY]["layout"]={
            "mode":"floating","anchor":"block","target":"b-other",
            "x_mm":10,"y_mm":15,"wrap":"square","layer":"content"}
        doc=Document()
        add_docx_shape(doc,block,PageSetup())
        xml=doc.element.body.xml
        self.assertIn("wp:anchor",xml)
        self.assertIn("wp:wrapSquare",xml)
