"""Small real documents built in code for the extraction tests."""

import io


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def text_pdf(lines: list[str]) -> bytes:
    """A valid one-page PDF whose text layer holds the given lines."""
    stream = "BT /F1 12 Tf 50 760 Td 16 TL " + " ".join(f"({_escape(line)}) Tj T*" for line in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = io.BytesIO()
    output.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(output.tell())
        output.write(f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1"))
    xref = output.tell()
    output.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        output.write(f"{offset:010d} 00000 n \n".encode())
    output.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return output.getvalue()


def word_document(lines: list[str], table: list[list[str]] | None = None) -> bytes:
    from docx import Document

    document = Document()
    for line in lines:
        document.add_paragraph(line)
    if table:
        grid = document.add_table(rows=len(table), cols=len(table[0]))
        for row_index, row in enumerate(table):
            for column, value in enumerate(row):
                grid.cell(row_index, column).text = value
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


FIRE_CERTIFICATE = [
    "FIRE SAFETY CERTIFICATE",
    "Certificate No: FS-2291",
    "Issued by: City Fire Department",
    "Issued to: Globex Supplies",
    "Date of issue: 15 March 2026",
    "Expiry date: 14 March 2027",
]
