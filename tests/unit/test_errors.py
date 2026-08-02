from pathlib import Path

import pytest

from estimate_extractor.errors import UnsupportedPDFError
from estimate_extractor.pdf.reader import open_document


def test_open_nonexistent_file_raises_unsupported_pdf_error(tmp_path):
    with pytest.raises(UnsupportedPDFError):
        open_document(tmp_path / "does_not_exist.pdf")


def test_open_non_pdf_file_raises_unsupported_pdf_error(tmp_path):
    bogus = tmp_path / "not_a_pdf.pdf"
    bogus.write_text("this is not a PDF file at all")
    with pytest.raises(UnsupportedPDFError):
        open_document(bogus)
