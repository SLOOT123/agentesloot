"""Extractor fijo en un proceso separado. Nunca ejecuta el contenido subido."""
import base64
import io
import json
import re
import sys
import warnings
import zipfile
from html.parser import HTMLParser
from pathlib import PurePath
from xml.etree import ElementTree as ET

MAX_TEXT = 200_000
MAX_PAGES = 50
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".ico", ".avif"}


def picture(data):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 20_000_000
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as original:
            if original.width * original.height > 20_000_000:
                raise ValueError("image_too_large")
            frame_count = getattr(original, "n_frames", 1)
            original.seek(0)
            image = ImageOps.exif_transpose(original).convert("RGB")
            size = [image.width, image.height]
            image.thumbnail((1400, 1400))
            output = io.BytesIO()
            image.save(output, "JPEG", quality=86)
    return base64.b64encode(output.getvalue()).decode("ascii"), {"dimensions": size, "frames": frame_count}


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.output, self.ignored = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}: self.ignored += 1
        if tag in {"p", "div", "br", "tr", "li", "h1", "h2", "h3"}: self.output.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"}: self.ignored = max(0, self.ignored - 1)

    def handle_data(self, data):
        if not self.ignored: self.output.append(data)


def decode_text(data):
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try: return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        if b"\0" in data: raise ValueError("unsupported_format")
        text = data.decode("cp1252")
        if sum(ord(char) < 32 and char not in "\n\r\t\f" for char in text) > max(2, len(text) // 100):
            raise ValueError("unsupported_format")
        return text


def office(data, suffix):
    out, metadata = [], {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 2500 or sum(item.file_size for item in entries) > 50_000_000 or any(item.flag_bits & 1 for item in entries):
            raise ValueError("archive_too_large")
        def xml(name):
            info = archive.getinfo(name)
            if info.file_size > 10_000_000: raise ValueError("archive_too_large")
            return ET.fromstring(archive.read(name))
        if suffix == ".docx":
            root = xml("word/document.xml")
            for paragraph in root.iter():
                if paragraph.tag.endswith("}p"):
                    out.append("".join(node.text or "" for node in paragraph.iter() if node.tag.endswith("}t")))
        elif suffix == ".pptx":
            names = sorted((item.filename for item in entries if re.fullmatch(r"ppt/slides/slide\d+\.xml", item.filename)), key=lambda name: int(re.search(r"(\d+)\.xml", name)[1]))
            metadata = {"units": len(names), "units_extracted": min(len(names), MAX_PAGES), "truncated": len(names) > MAX_PAGES}
            for index, name in enumerate(names[:MAX_PAGES], 1):
                out.append("[Diapositiva " + str(index) + "]\n" + "\n".join(node.text or "" for node in xml(name).iter() if node.tag.endswith("}t")))
        else:
            ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            shared = []
            if "xl/sharedStrings.xml" in archive.namelist():
                shared = ["".join(item.itertext()) for item in xml("xl/sharedStrings.xml").findall("s:si", ns)]
            names = sorted(item.filename for item in entries if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", item.filename))
            metadata = {"units": len(names), "units_extracted": min(len(names), MAX_PAGES), "truncated": len(names) > MAX_PAGES}
            for name in names[:MAX_PAGES]:
                out.append("[Hoja " + PurePath(name).stem + "]")
                for row in xml(name).findall(".//s:row", ns):
                    values = []
                    for cell in row.findall("s:c", ns):
                        value = cell.findtext("s:v", "", ns)
                        if cell.get("t") == "s": value = shared[int(value)]
                        elif cell.get("t") == "inlineStr": value = "".join(cell.itertext())
                        formula = cell.findtext("s:f", "", ns)
                        values.append(cell.get("r", "") + "=" + ("Fórmula: " + formula + "; valor guardado: " + value if formula else value))
                    out.append(" | ".join(values))
    return "\n".join(out), metadata


def extract(payload):
    data = base64.b64decode(payload["data"], validate=True)
    if not data or len(data) > 20 * 1024 * 1024: raise ValueError("file_size")
    suffix = PurePath(payload["name"]).suffix.lower()
    metadata, preview = {}, ""
    if suffix == ".pdf":
        import pymupdf
        with pymupdf.open(stream=data, filetype="pdf") as document:
            if document.needs_pass: raise ValueError("encrypted_pdf")
            total = len(document)
            if not total: raise ValueError("empty_document")
            if payload.get("mode") == "render":
                pages = payload.get("pages", [0])[:4]
                views = []
                for number in pages:
                    if type(number) is not int or not 0 <= number < min(total, MAX_PAGES): continue
                    page = document[number]
                    matrix = pymupdf.Matrix(min(1.7, 1600 / max(page.rect.width, page.rect.height)), min(1.7, 1600 / max(page.rect.width, page.rect.height)))
                    raw = page.get_pixmap(matrix=matrix, alpha=False).tobytes("jpeg", jpg_quality=80)
                    views.append({"page": number + 1, "image": base64.b64encode(raw).decode("ascii")})
                return {"ok": True, "views": views}
            pieces, scanned, count = [], [], 0
            for index in range(min(total, MAX_PAGES)):
                text = document[index].get_text(sort=True)
                if len(text.strip()) < 30: scanned.append(index)
                piece = "[Página " + str(index + 1) + "]\n" + text
                pieces.append(piece[:MAX_TEXT - count])
                count += len(piece)
                if count >= MAX_TEXT: break
            text = "\n\n".join(pieces)
            page = document[0]
            scale = min(1.0, 800 / max(page.rect.width, page.rect.height))
            preview = base64.b64encode(page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("jpeg")).decode("ascii")
            metadata = {"pages": total, "pages_extracted": len(pieces), "scanned_pages": scanned, "truncated": total > len(pieces) or count >= MAX_TEXT}
        kind, media = "pdf", "application/pdf"
    elif suffix in IMAGE_TYPES:
        preview, metadata = picture(data)
        text, kind, media = "", "image", "image/jpeg"
        metadata["truncated"] = metadata.get("frames", 1) > 1
    elif suffix in {".docx", ".xlsx", ".pptx"}:
        text, metadata = office(data, suffix)
        kind, media = "document", "application/octet-stream"
        metadata["truncated"] = metadata.get("truncated", False) or len(text) > MAX_TEXT
    else:
        if suffix in {".exe", ".dll", ".com", ".msi", ".zip", ".7z", ".rar", ".doc", ".xls", ".ppt", ".mp3", ".mp4", ".wav", ".heic"}:
            raise ValueError("unsupported_format")
        text, kind, media = decode_text(data), "text", "text/plain"
        if suffix in {".html", ".htm"}:
            parser = HTMLText(); parser.feed(text); text = "".join(parser.output)
        metadata["truncated"] = len(text) > MAX_TEXT
    text = text.replace("\x00", "")[:MAX_TEXT]
    return {"ok": True, "kind": kind, "media_type": media, "text": text, "preview": preview, "metadata": metadata}


if __name__ == "__main__":
    try:
        result = extract(json.loads(sys.stdin.buffer.read(29 * 1024 * 1024)))
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) and str(error) in {"file_size", "unsupported_format", "image_too_large", "archive_too_large", "encrypted_pdf", "empty_document"} else "invalid_file"
        result = {"ok": False, "error": code}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
