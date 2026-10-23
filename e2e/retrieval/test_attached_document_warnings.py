"""Journey: a document attached in the chat composer that cannot be read says why.

The composer uploads an attachment and follows its processing; when reading it fails, the
reason the server stored is shown as a warning. On an install without the pandoc program, a
reStructuredText file (which unstructured converts through pypandoc) names the missing program,
and a PDF that needs a password to open (which pypdf cannot decrypt) says so. Twin of the
pandoc and password cases in integration/deps/test_document_extraction.py.

Discriminates: on dev ef67cc3fa, in a backend copy that no longer maps pypandoc's "No pandoc was
found" error to the pandoc message, the .rst warning shows the raw error and the test fails;
with the loader opening a locked PDF with its owner password the PDF is read without a warning.
"""

from __future__ import annotations

import io

import pytest
from playwright.sync_api import Page, expect

from utils.chat_ui import chat_input

pytestmark = [pytest.mark.journey, pytest.mark.requires_browser, pytest.mark.requires_source]

PANDOC_MISSING = "Pandoc is not installed on the server"
WARNING_TIMEOUT_MS = 30_000


def _attach(page: Page, name: str, mime_type: str, content: bytes) -> None:
    expect(chat_input(page)).to_be_visible()
    page.get_by_role("button", name="More", exact=True).last.click()
    with page.expect_file_chooser() as chooser:
        page.get_by_role("menu").get_by_role("menuitem", name="Upload Files").click()
    chooser.value.set_files({"name": name, "mimeType": mime_type, "buffer": content})


def _locked_pdf() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt(user_password="harbour-password", owner_password="harbour-owner")
    locked = io.BytesIO()
    writer.write(locked)
    return locked.getvalue()


def test_without_pandoc_an_attached_rst_file_names_the_missing_program(page_for, make_user):
    import pypandoc

    try:
        pypandoc.get_pandoc_version()
    except OSError:
        pass
    else:
        pytest.skip("pandoc is installed; this is the warning an install without it shows")
    page = page_for(make_user())

    _attach(page, "harbour.rst", "text/x-rst", b"Harbour\n=======\n\nThe tide turns at noon.\n")

    expect(page.get_by_text(PANDOC_MISSING)).to_be_visible(timeout=WARNING_TIMEOUT_MS)


def test_an_attached_pdf_that_needs_a_password_says_it_cannot_be_read(page_for, make_user):
    page = page_for(make_user())

    _attach(page, "survey.pdf", "application/pdf", _locked_pdf())

    expect(page.get_by_text("decrypt", exact=False)).to_be_visible(timeout=WARNING_TIMEOUT_MS)
