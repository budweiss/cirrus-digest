"""Minimal Word (.docx) writer, standard library only (S679, 11-24).

CUMULUS1 has no python-docx, and Buddy asked for Word attachments when a client asks for a
file. A .docx is a zip of three small XML parts. Headings are bold and larger; everything else
is plain paragraphs, with line breaks kept. Checked to open with macOS textutil (Word's format
reader) and to parse as XML.
"""
import re
import zipfile
from xml.sax.saxutils import escape

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '</Types>')
_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'officeDocument" Target="word/document.xml"/></Relationships>')
_NOT_XML = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]')
HEADING = re.compile(r'(?:chapter|part|prologue|epilogue|scene)\b[^\n]{0,70}$', re.I)


def _para(text, size=None, bold=False, italic=False):
    props = ('<w:b/>' if bold else '') + ('<w:i/>' if italic else '') + ('<w:sz w:val="%d"/>' % size if size else '')
    rpr = '<w:rPr>%s</w:rPr>' % props if props else ''
    runs = []
    for i, line in enumerate(text.split('\n')):
        if i:
            runs.append('<w:r><w:br/></w:r>')
        runs.append('<w:r>%s<w:t xml:space="preserve">%s</w:t></w:r>' % (rpr, escape(_NOT_XML.sub('', line))))
    return '<w:p>%s</w:p>' % ''.join(runs)


def write(path, title, paragraphs, note=None, heading=HEADING.match):
    """Write title, an optional italic note, then paragraphs; heading(p) makes p a heading."""
    body = [_para(title, size=40, bold=True)]
    if note:
        body.append(_para(note, italic=True))
    for p in paragraphs:
        p = p.strip()
        if p:
            body.append(_para(p, size=28, bold=True) if heading(p) else _para(p))
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body>%s<w:sectPr/></w:body></w:document>' % ''.join(body))
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', _CONTENT_TYPES)
        z.writestr('_rels/.rels', _RELS)
        z.writestr('word/document.xml', document)
    return path


def paragraphs(text):
    """Split plain text on blank lines."""
    return [p for p in re.split(r'\n\s*\n', text or '') if p.strip()]
