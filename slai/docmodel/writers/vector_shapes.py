"""Render SLDC's inert slai.writer.shape extension as native PDF paths.

This module is a Documaster export adapter, never a document editor. It does
not execute arbitrary SVG/HTML. Only fixed geometric presets are accepted.
PDF supports vector paths and searchable PDF text. Blurred shadows are
approximated by bounded, translucent vector layers, explicitly warned about.
"""
from __future__ import annotations

from html import escape
from math import cos, pi, sin
from typing import Any

SHAPE_KEY = "slai.writer.shape"
_KINDS = frozenset({"rectangle", "ellipse", "line", "triangle", "diamond",
                    "pentagon", "hexagon", "star", "arrow", "plus"})
MM = 72 / 25.4


def supported(block: dict[str, Any]) -> bool:
    shape = (block.get("extensions") or {}).get(SHAPE_KEY)
    return (block.get("type") == "paragraph" and isinstance(shape, dict)
            and shape.get("version") == 1 and shape.get("kind") in _KINDS
            and isinstance(shape.get("width_mm"), (int, float))
            and isinstance(shape.get("height_mm"), (int, float))
            and 2 <= shape["width_mm"] <= 1000
            and 2 <= shape["height_mm"] <= 1000)


def _vertices(kind: str) -> list[tuple[float, float]]:
    if kind == "triangle":
        return [(50, 0), (100, 100), (0, 100)]
    if kind == "diamond":
        return [(50, 0), (100, 50), (50, 100), (0, 50)]
    if kind == "arrow":
        return [(0, 30), (62, 30), (62, 5), (100, 50),
                (62, 95), (62, 70), (0, 70)]
    if kind == "plus":
        return [(35, 0), (65, 0), (65, 35), (100, 35), (100, 65),
                (65, 65), (65, 100), (35, 100), (35, 65), (0, 65), (0, 35), (35, 35)]
    if kind == "star":
        return [(50 + (22 if i % 2 else 50) * cos(-pi / 2 + i * pi / 5),
                 50 + (22 if i % 2 else 50) * sin(-pi / 2 + i * pi / 5))
                for i in range(10)]
    n = 5 if kind == "pentagon" else 6
    return [(50 + 50 * cos(-pi / 2 + i * 2 * pi / n),
             50 + 50 * sin(-pi / 2 + i * 2 * pi / n)) for i in range(n)]


def _path(c: Any, s: dict[str, Any], w: float, h: float) -> Any:
    """Coordinates in local points with +y up, matching ReportLab."""
    k = s["kind"]
    path = c.beginPath()
    xy = lambda xx, yy: (xx * w / 100, h - yy * h / 100)
    if k == "line":
        path.moveTo(*xy(0, 50))
        path.lineTo(*xy(100, 50))
    elif k == "ellipse":
        cx, cy, rx, ry, q = w/2, h/2, w/2, h/2, .55228474983
        path.moveTo(cx + rx, cy)
        path.curveTo(cx + rx, cy + q*ry, cx + q*rx, cy + ry, cx, cy + ry)
        path.curveTo(cx - q*rx, cy + ry, cx - rx, cy + q*ry, cx - rx, cy)
        path.curveTo(cx - rx, cy - q*ry, cx - q*rx, cy - ry, cx, cy - ry)
        path.curveTo(cx + q*rx, cy - ry, cx + rx, cy - q*ry, cx + rx, cy)
        path.close()
    elif k == "rectangle":
        corners = s.get("corners_mm") or [0, 0, 0, 0]
        radii = [min(float(v)*MM, w/2, h/2) for v in corners]
        tl, tr, br, bl = radii
        q = .55228474983
        path.moveTo(tl, h)
        path.lineTo(w-tr, h)
        path.curveTo(w-tr+q*tr,h,w,h-tr+q*tr,w,h-tr)
        path.lineTo(w,br)
        path.curveTo(w,br-q*br,w-br+q*br,0,w-br,0)
        path.lineTo(bl,0)
        path.curveTo(bl-q*bl,0,0,bl-q*bl,0,bl)
        path.lineTo(0,h-tl)
        path.curveTo(0,h-tl+q*tl,tl-q*tl,h,tl,h)
        path.close()
    else:
        verts = _vertices(k)
        path.moveTo(*xy(*verts[0]))
        for p in verts[1:]:
            path.lineTo(*xy(*p))
        path.close()
    return path


def _region(kind: str) -> tuple[float, float, float, float]:
    return {
        "rectangle":(.05,.07,.9,.86), "ellipse":(.18,.18,.64,.64),
        "triangle":(.35,.48,.30,.44), "diamond":(.28,.28,.44,.44),
        "pentagon":(.24,.34,.52,.50), "hexagon":(.2,.18,.6,.64),
        "star":(.33,.37,.34,.27), "arrow":(.1,.36,.48,.28),
        "plus":(.09,.37,.82,.26), "line":(0,0,0,0),
    }[kind]


def _segments(text: str, spans: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    # Shape span indices use UTF-16 code units (browser document coordinates).
    points=[0]
    for char in text:
        points.append(points[-1] + (2 if ord(char) > 0xffff else 1))
    def at(unit: int) -> int:
        return next((i for i, v in enumerate(points) if v >= unit), len(text))
    boundaries={0,len(text)}
    for span in spans:
        if isinstance(span,dict) and span.get("type")=="format":
            boundaries.update((at(int(span.get("start",0))),at(int(span.get("end",0)))))
    ordered=sorted(boundaries)
    runs=[]
    for start,end in zip(ordered,ordered[1:]):
        attrs={}
        for span in spans:
            if isinstance(span,dict) and span.get("type")=="format" and at(int(span.get("start",0)))<=start and at(int(span.get("end",0)))>=end:
                attrs.update({k:span[k] for k in ("bold","italic","underline","color") if k in span})
        runs.append((text[start:end],attrs))
    return runs


def _pdf_text(c: Any, shape: dict[str, Any], width: float, height: float) -> None:
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph
    t=shape.get("text") or {}
    content=t.get("content","")
    if not isinstance(content,str) or not content or shape["kind"]=="line":
        return
    rx,ry,rw,rh=_region(shape["kind"])
    padding=max(0,min(float(t.get("padding_mm",0))*MM, min(width,height)/5))
    x=rx*width+padding
    y=(1-ry-rh)*height+padding
    max_w=max(1,rw*width-2*padding)
    max_h=max(1,rh*height-2*padding)
    fragments=[]
    for value,formatting in _segments(content,t.get("spans") or []):
        val=escape(value).replace("\n","<br/>")
        if formatting.get("bold"):
            val=f"<b>{val}</b>"
        if formatting.get("italic"):
            val=f"<i>{val}</i>"
        if formatting.get("underline"):
            val=f"<u>{val}</u>"
        if isinstance(formatting.get("color"),str):
            val=f'<font color="{escape(formatting["color"])}">{val}</font>'
        fragments.append(val)
    align={"left":0,"center":1,"right":2}.get(t.get("align"),1)
    initial=max(5,min(144,float(t.get("font_size_pt",12))))
    size=initial
    def make(size:float)->tuple[Any,float]:
        style=ParagraphStyle("shape-text",fontName="Helvetica-Bold" if t.get("bold") else "Helvetica",
            fontSize=size,leading=size*1.25,alignment=align,textColor=t.get("color","#222222"),
            splitLongWords=True,spaceAfter=0)
        paragraph=Paragraph("".join(fragments),style)
        _,actual=paragraph.wrap(max_w,max_h)
        return paragraph,actual
    p,actual=make(size)
    if t.get("fit")=="shrink":
        while actual>max_h and size>5:
            size=max(5,size-0.5)
            p,actual=make(size)
    p.drawOn(c,x,y+max(0,(max_h-actual)/2))


def draw_vector(c: Any,shape:dict[str,Any], width:float,height:float) -> None:
    """Paint all shapes, multiple crisp shadows, text and rotation as vector."""
    from reportlab.lib.colors import HexColor
    c.saveState()
    c.translate(width/2,height/2)
    c.rotate(-float(shape.get("rotation_deg",0)))
    c.translate(-width/2,-height/2)
    mirrored=bool(shape.get("flip_horizontal") or shape.get("flip_vertical"))
    if mirrored:
        c.saveState()
        c.translate(width/2,height/2)
        c.scale(-1 if shape.get("flip_horizontal") else 1,
                -1 if shape.get("flip_vertical") else 1)
        c.translate(-width/2,-height/2)
    opacity=max(0,min(1,float(shape.get("opacity",1))))
    base=_path(c,shape,width,height)
    for shadow in reversed(shape.get("shadows") or []):
        if not shadow.get("enabled",True):
            continue
        c.saveState()
        c.translate(float(shadow.get("offset_x_mm",0))*MM,-float(shadow.get("offset_y_mm",0))*MM)
        blur=max(0,min(30,float(shadow.get("blur_mm",0))))*MM
        spread=float(shadow.get("spread_mm",0))*MM
        steps=1 if blur<=0 else 8
        for i in range(steps,0,-1):
            c.saveState()
            size=spread + blur * i / steps * .65
            c.translate(width/2,height/2)
            c.scale(max(.1,1+size*2/width),max(.1,1+size*2/height))
            c.translate(-width/2,-height/2)
            c.setFillColor(HexColor(shadow.get("color","#444444")))
            c.setFillAlpha(opacity*float(shadow.get("opacity",.5))*(1 if steps==1 else .16))
            c.drawPath(base,stroke=0,fill=1)
            c.restoreState()
        c.restoreState()
    fill=shape.get("fill") or {}
    if fill.get("enabled",True) and shape["kind"]!="line":
        c.setFillColor(HexColor(fill.get("color","#f1d24b")))
        c.setFillAlpha(opacity*float(fill.get("opacity",1)))
        c.drawPath(base,stroke=0,fill=1)
    outline=shape.get("outline") or {}
    if outline.get("enabled",True):
        c.setStrokeColor(HexColor(outline.get("color","#222222")))
        c.setStrokeAlpha(opacity*float(outline.get("opacity",1)))
        c.setLineWidth(max(.1,float(outline.get("width_pt",1))))
        style=outline.get("style")
        if style=="dashed":
            c.setDash(5,3)
        elif style=="dotted":
            c.setDash(1,3)
        c.drawPath(base,stroke=1,fill=0)
    c.setFillAlpha(opacity)
    if mirrored:
        c.restoreState()  # Text remains readable while geometry is mirrored.
    _pdf_text(c,shape,width,height)
    c.restoreState()


def make_flowable(block:dict[str,Any], available_width:float, section_setup:Any) -> Any:
    from reportlab.platypus import Flowable
    shape=(block.get("extensions") or {})[SHAPE_KEY]
    w=min(float(shape["width_mm"])*MM,available_width)
    h=float(shape["height_mm"])*MM*w/(float(shape["width_mm"])*MM)
    placement=shape.get("layout") or {}
    floating=(placement.get("mode")=="floating" and placement.get("wrap","none")!="top_bottom")
    class VectorShape(Flowable):
        def __init__(self)->None:
            super().__init__()
            self.width,self.height=w,(.01 if floating else h)
            self._sldc_block_id=block["id"]
            self.hAlign="CENTER"
        def wrap(self,availWidth:float,availHeight:float)->tuple[float,float]:  # noqa:N802
            return self.width,self.height
        def draw(self)->None:
            c=self.canv
            if floating:
                position=shape.get("layout") or {}
                # Absolute coordinates use current page content margins.
                _,page_h=c._pagesize
                want_x=section_setup.left*MM+float(position.get("x_mm",0))*MM
                want_y=page_h-section_setup.top*MM-float(position.get("y_mm",0))*MM-h
                actual_x,actual_y=c.absolutePosition(0,0)
                c.saveState()
                c.translate(want_x-actual_x,want_y-actual_y)
                draw_vector(c,shape,w,h)
                c.restoreState()
            else:
                # Top-and-bottom wrapping participates in ReportLab flow,
                # consistent with Writer's block-granular pagination.
                draw_vector(c,shape,w,h)
    return VectorShape()


# Office Open XML preset geometry mapping (native, editable Word shapes).
_WORD_PRESETS = {
    "rectangle":"rect", "ellipse":"ellipse", "line":"line", "triangle":"triangle",
    "diamond":"diamond", "pentagon":"pentagon", "hexagon":"hexagon",
    "star":"star5", "arrow":"rightArrow", "plus":"plus",
}
_WORD_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_WORD_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_WORD_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_WORD_WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
_WORD_URI = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"


def _xml_color(color:str,opacity:float=1.0)->str:
    color=color[1:] if isinstance(color,str) and len(color)==7 and color.startswith("#") else "333333"
    alpha=round(100000*max(0,min(1,float(opacity))))
    return f'<a:srgbClr val="{color}"><a:alpha val="{alpha}"/></a:srgbClr>'


def _docx_text(shape:dict[str,Any])->str:
    t=shape.get("text") or {}
    content=t.get("content","")
    if not isinstance(content,str) or not content or shape["kind"]=="line":
        return ""
    runs=[]
    for value,formatting in _segments(content,t.get("spans") or []):
        size=float(formatting.get("font_size_pt",t.get("font_size_pt",12)))
        size=max(5,min(144,size))
        font=escape(str(formatting.get("font_family","Calibri")),quote=True)[:100]
        color=formatting.get("color",t.get("color","#222222"))
        color=(color[1:] if isinstance(color,str) and len(color)==7 and color.startswith("#") else "222222")
        props=(f'<w:rFonts w:ascii="{font}" w:hAnsi="{font}"/>'
               f'<w:sz w:val="{round(size*2)}"/><w:color w:val="{color}"/>')
        if formatting.get("bold",t.get("bold",False)):
            props+="<w:b/>"
        if formatting.get("italic",t.get("italic",False)):
            props+="<w:i/>"
        if formatting.get("underline"):
            props+='<w:u w:val="single"/>'
        split=value.split("\n")
        for i,chunk in enumerate(split):
            if i:
                runs.append("<w:r><w:br/></w:r>")
            if chunk:
                # Only legal XML 1.0 characters may enter WordprocessingML.
                clean="".join(ch for ch in chunk if ch in "\t\n\r" or ord(ch)>=32)
                runs.append(f'<w:r><w:rPr>{props}</w:rPr><w:t xml:space="preserve">{escape(clean)}</w:t></w:r>')
    alignment={"left":"left","center":"center","right":"right"}.get(t.get("align"),"center")
    return f'<wps:txbx><w:txbxContent><w:p><w:pPr><w:jc w:val="{alignment}"/></w:pPr>{"".join(runs)}</w:p></w:txbxContent></wps:txbx>'


def add_docx_shape(document:Any,block:dict[str,Any],setup:Any)->list[str]:
    """Append a native WordprocessingShape (Word 2010+) to a document paragraph.

    This path supports editable geometry, shape-owned text, opacity, one shadow,
    rotation and Word-native floating anchors. Unsupported effects are warned.
    """
    from docx.oxml import parse_xml
    shape=block["extensions"][SHAPE_KEY]
    kind=shape["kind"]
    w=round(float(shape["width_mm"])*36000)
    h=round(float(shape["height_mm"])*36000)
    if not 72000<=w<=36_000_000 or not 72000<=h<=36_000_000:
        raise ValueError("Shape dimensions must be from 2 to 1000 mm")
    ident=500000+len(document.element.xpath(".//wp:docPr"))
    name=escape(f"Writer {kind} shape",quote=True)
    alt=escape((shape.get("text") or {}).get("content","")[:500],quote=True)
    extent=f'<wp:extent cx="{w}" cy="{h}"/>'
    docpr=f'<wp:docPr id="{ident}" name="{name}" descr="{alt}"/><wp:cNvGraphicFramePr/>'
    layout=shape.get("layout") or {}
    if layout.get("mode")=="floating":
        x=round((float(setup.left)+float(layout.get("x_mm",0)))*36000)
        y=round((float(setup.top)+float(layout.get("y_mm",0)))*36000)
        behind=1 if layout.get("layer")=="behind_text" else 0
        z=max(-1000,min(1000,int(shape.get("z_index",0))))
        relative=1000000+z*100
        wrap=layout.get("wrap","none")
        wrapxml=('<wp:wrapSquare wrapText="bothSides"/>' if wrap=="square" else
                 '<wp:wrapTopAndBottom/>' if wrap=="top_bottom" else '<wp:wrapNone/>')
        container=(f'<wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" '
                   f'relativeHeight="{relative}" behindDoc="{behind}" locked="0" layoutInCell="1" allowOverlap="1">'
                   f'<wp:simplePos x="0" y="0"/>'
                   f'<wp:positionH relativeFrom="page"><wp:posOffset>{x}</wp:posOffset></wp:positionH>'
                   f'<wp:positionV relativeFrom="page"><wp:posOffset>{y}</wp:posOffset></wp:positionV>'
                   f'{extent}{wrapxml}{docpr}{{graphic}}</wp:anchor>')
    else:
        container=f'<wp:inline distT="0" distB="0" distL="0" distR="0">{extent}{docpr}{{graphic}}</wp:inline>'
    rotation=round(float(shape.get("rotation_deg",0))*60000)
    flip_attributes=(' flipH="1"' if shape.get("flip_horizontal") else '')+(
        ' flipV="1"' if shape.get("flip_vertical") else '')
    geom=_WORD_PRESETS[kind]
    fill=shape.get("fill") or {}
    outline=shape.get("outline") or {}
    overall=float(shape.get("opacity",1))
    fillxml=(f'<a:solidFill>{_xml_color(fill.get("color","#f1d24b"),overall*float(fill.get("opacity",1)))}</a:solidFill>'
             if fill.get("enabled",True) and kind!="line" else "<a:noFill/>")
    if outline.get("enabled",True):
        width=max(3600,round(float(outline.get("width_pt",1))*12700))
        stroke=_xml_color(outline.get("color","#222222"),overall*float(outline.get("opacity",1)))
        dash=outline.get("style")
        dashxml=f'<a:prstDash val="{"dash" if dash=="dashed" else "dot"}"/>' if dash in ("dashed","dotted") else ""
        linexml=f'<a:ln w="{width}"><a:solidFill>{stroke}</a:solidFill>{dashxml}</a:ln>'
    else:
        linexml='<a:ln><a:noFill/></a:ln>'
    shadows=shape.get("shadows") or []
    effects=""
    if shadows:
        s=shadows[0]
        if s.get("enabled",True):
            import math
            dx=float(s.get("offset_x_mm",0))
            dy=float(s.get("offset_y_mm",0))
            dist=round(math.hypot(dx,dy)*36000)
            direction=round((math.degrees(math.atan2(dy,dx))%360)*60000)
            blur=round(max(0,float(s.get("blur_mm",0)))*36000)
            cc=_xml_color(s.get("color","#555555"),overall*float(s.get("opacity",.5)))
            effects=(f'<a:effectLst><a:outerShdw blurRad="{blur}" dist="{dist}" dir="{direction}" '
                     f'algn="ctr" rotWithShape="0">{cc}</a:outerShdw></a:effectLst>')
    txbx=_docx_text(shape)
    pad=round(float((shape.get("text") or {}).get("padding_mm",2))*36000)
    bodypr=f'<wps:bodyPr wrap="square" lIns="{pad}" rIns="{pad}" tIns="{pad}" bIns="{pad}" anchor="ctr"/>'
    geometry=(f'<a:graphic><a:graphicData uri="{_WORD_URI}"><wps:wsp><wps:cNvSpPr txBox="0"/>'
              f'<wps:spPr><a:xfrm rot="{rotation}"{flip_attributes}><a:off x="0" y="0"/><a:ext cx="{w}" cy="{h}"/></a:xfrm>'
              f'<a:prstGeom prst="{geom}"><a:avLst/></a:prstGeom>{fillxml}{linexml}{effects}</wps:spPr>'
              f'{txbx}{bodypr}</wps:wsp></a:graphicData></a:graphic>')
    root=(f'<w:drawing xmlns:w="{_WORD_W}" xmlns:wp="{_WORD_WP}" '
          f'xmlns:a="{_WORD_A}" xmlns:wps="{_WORD_WPS}">'
          f'{container.format(graphic=geometry)}</w:drawing>')
    paragraph=document.add_paragraph()
    paragraph.add_run()._r.append(parse_xml(root))
    messages=[]
    if len(shadows)>1:
        messages.append("DOCX supports only the first of multiple Writer shadow layers in native shape mode")
    if len(set(shape.get("corners_mm") or []))>1:
        messages.append("Independent Writer corner radii are not expressible with Word preset geometry")
    if overall<1:
        messages.append("Word shape text opacity is not completely preserved")
    return messages
