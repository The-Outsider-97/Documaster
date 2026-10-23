"""PDF operations: page arrangement, annotation, stamps, forms, redaction and rendering.

Built on pypdf (structure) and pypdfium2 (rendering, text geometry, search). Every operation reads a source PDF and writes a
new one; verification is part of each operation where a claim is made (a redaction is only reported once the removed text is
shown to be absent from the result).
"""
