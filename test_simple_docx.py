"""Synthetic checks for the standard-library Word writer: temp files only."""
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import simple_docx

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


class SimpleDocx(unittest.TestCase):
    def test_writes_a_valid_package_with_headings_and_escaped_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = simple_docx.write(Path(tmp) / 'a.docx', 'Swarm & Co', [
                'Chapter 1. Tank <Nine>', 'Line one\nline two', 'Bad \x07 char', '  '], note='Fiction.')
            with zipfile.ZipFile(path) as z:
                self.assertEqual(sorted(z.namelist()), ['[Content_Types].xml', '_rels/.rels', 'word/document.xml'])
                root = ET.fromstring(z.read('word/document.xml'))    # must be well-formed XML
            paras = root.find(W + 'body').findall(W + 'p')
            text = [''.join(t.text or '' for t in p.iter(W + 't')) for p in paras]
            self.assertEqual(text, ['Swarm & Co', 'Fiction.', 'Chapter 1. Tank <Nine>', 'Line oneline two', 'Bad  char'])
            self.assertIsNotNone(paras[2].find('.//' + W + 'b'))         # heading is bold
            self.assertIsNone(paras[4].find('.//' + W + 'b'))            # body text is not
            self.assertIsNotNone(paras[3].find('.//' + W + 'br'))        # line break kept

    def test_paragraphs_split_on_blank_lines(self):
        self.assertEqual(simple_docx.paragraphs('a\n\n\nb\n  \nc'), ['a', 'b', 'c'])


if __name__ == '__main__':
    unittest.main()
