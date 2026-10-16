"""charyam_docs.py - Génération de documents Word/PDF et calcul de devis pour CHARYAM.

Principes
- Tout est produit EN MÉMOIRE (bytes) : aucun fichier temporaire, donc aucun risque
  de fuite entre utilisateurs et rien à nettoyer.
- DOCX : zéro dépendance (zipfile + XML), pour limiter le risque de compatibilité
  avec Python 3.14 sur Render.
- PDF : reportlab, importé seulement au moment de l'usage.
- Les calculs de devis sont faits ici avec Decimal, jamais par le modèle.

Format des blocs (liste de dict) :
  {"type": "h1"|"h2"|"h3"|"p"|"bullet", "text": str}
  {"type": "num", "n": int, "text": str}
  {"type": "table", "rows": [[str, ...], ...]}   # 1re ligne = en-tête
"""
from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from xml.sax.saxutils import escape as _xml_escape

# Palette sobre et lisible (ATS : la couleur ne touche que le texte des titres et un filet, jamais
# d'image, de colonne ni de tableau de mise en page)
COLOR_NAVY = "1F3A5F"     # nom / titres principaux
COLOR_TEAL = "0E7C7B"     # sous-titres et filets
COLOR_TINT = "DCEFEE"     # fond d'en-tête de tableau

MAX_BLOCKS = 2000
MAX_TEXT = 20000  # caractères par bloc

# Caractères interdits en XML 1.0 (contrôles, substituts isolés, U+FFFE/FFFF)
_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_SEP_CELL = re.compile(r"^:?-{2,}:?$")


def _clean(value) -> str:
    return _BAD_XML.sub("", str(value))[:MAX_TEXT]


# --------------------------------------------------------------------------- #
# Markdown simplifié -> blocs
# --------------------------------------------------------------------------- #
def _strip_inline(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    return text.replace("`", "").strip()


def markdown_to_blocks(text: str) -> list[dict]:
    """Convertit le texte produit par Claude (titres #, listes, tableaux |) en blocs."""
    blocks: list[dict] = []
    para: list[str] = []
    table: list[list[str]] = []

    def flush_para():
        if para:
            blocks.append({"type": "p", "text": " ".join(para)})
            para.clear()

    def flush_table():
        if table:
            width = max(len(r) for r in table)
            rows = [r + [""] * (width - len(r)) for r in table]
            blocks.append({"type": "table", "rows": rows})
            table.clear()

    for raw in _clean(text).splitlines():
        s = raw.strip()
        if table and not s.startswith("|"):
            flush_table()
        if not s:
            flush_para()
            continue
        m = re.match(r"^(#{1,3})\s+(.*)$", s)
        if m:
            flush_para()
            blocks.append({"type": f"h{len(m.group(1))}", "text": _strip_inline(m.group(2))})
            continue
        if s.startswith("|"):
            flush_para()
            cells = [_strip_inline(c) for c in s.strip("|").split("|")]
            if cells and all(_SEP_CELL.match(c.strip()) for c in cells):
                continue  # ligne de séparation |---|---|
            table.append(cells)
            continue
        m = re.match(r"^[-*•]\s+(.*)$", s)
        if m:
            flush_para()
            blocks.append({"type": "bullet", "text": _strip_inline(m.group(1))})
            continue
        m = re.match(r"^(\d{1,3})[.)]\s+(.*)$", s)
        if m:
            flush_para()
            blocks.append({"type": "num", "n": int(m.group(1)), "text": _strip_inline(m.group(2))})
            continue
        para.append(_strip_inline(s))
    flush_para()
    flush_table()
    return blocks[:MAX_BLOCKS]


# --------------------------------------------------------------------------- #
# DOCX (sans dépendance)
# --------------------------------------------------------------------------- #
_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
    "</Types>"
)
_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
    "</Relationships>"
)
_DOC_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    "</Relationships>"
)


def _style(sid: str, name: str, size: int, bold: bool, before: int = 0, after: int = 120,
           color: str = "", rule: str = "") -> str:
    b = "<w:b/>" if bold else ""
    c = f'<w:color w:val="{color}"/>' if color else ""
    border = (f'<w:pBdr><w:bottom w:val="single" w:sz="8" w:space="2" w:color="{rule}"/></w:pBdr>'
              if rule else "")
    return (
        f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/>'
        '<w:basedOn w:val="Normal"/><w:qFormat/>'
        f'<w:pPr><w:keepNext/>{border}<w:spacing w:before="{before}" w:after="{after}"/></w:pPr>'
        f'<w:rPr>{b}{c}<w:sz w:val="{size}"/></w:rPr></w:style>'
    )


_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>'
    '<w:sz w:val="22"/><w:lang w:val="fr-FR"/></w:rPr></w:rPrDefault></w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/>'
    '<w:pPr><w:spacing w:after="120" w:line="264" w:lineRule="auto"/></w:pPr></w:style>'
    + _style("Title", "Title", 40, True, 0, 240, color=COLOR_NAVY)
    + _style("Heading1", "heading 1", 36, True, 0, 60, color=COLOR_NAVY)
    + _style("Heading2", "heading 2", 24, True, 240, 100, color=COLOR_TEAL, rule=COLOR_TEAL)
    + _style("Heading3", "heading 3", 22, True, 160, 60, color=COLOR_NAVY)
    + "</w:styles>"
)


def _run(text: str, bold: bool = False) -> str:
    b = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return f'<w:r>{b}<w:t xml:space="preserve">{_xml_escape(_clean(text))}</w:t></w:r>'


def _para(text: str, style: str | None = None, indent: tuple[int, int] | None = None,
          bold: bool = False, align: str | None = None) -> str:
    ppr = ""
    if style:
        ppr += f'<w:pStyle w:val="{style}"/>'
    if indent:
        ppr += f'<w:ind w:left="{indent[0]}" w:hanging="{indent[1]}"/>'
    if align:
        ppr += f'<w:jc w:val="{align}"/>'
    ppr = f"<w:pPr>{ppr}</w:pPr>" if ppr else ""
    return f"<w:p>{ppr}{_run(text, bold)}</w:p>"


def _table(rows: list[list[str]]) -> str:
    border = "".join(
        f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="9DB8B7"/>'
        for side in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    out = [f'<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/><w:tblBorders>{border}</w:tblBorders></w:tblPr>']
    for i, row in enumerate(rows):
        out.append("<w:tr>")
        for cell in row:
            shade = f'<w:shd w:val="clear" w:color="auto" w:fill="{COLOR_TINT}"/>' if i == 0 else ""
            out.append(
                f"<w:tc><w:tcPr>{shade}</w:tcPr>"
                f'<w:p><w:pPr><w:spacing w:after="40"/></w:pPr>{_run(cell, bold=(i == 0))}</w:p></w:tc>'
            )
        out.append("</w:tr>")
    out.append("</w:tbl><w:p/>")
    return "".join(out)


def docx_bytes(title: str, blocks: list[dict], author: str = "CHARYAM", theme: str = "doc") -> bytes:
    """Construit un fichier .docx valide et le renvoie en mémoire.

    theme="cv" : pas de titre automatique (le nom du candidat, en titre 1, ouvre le document)."""
    body = [] if theme == "cv" else [_para(title, style="Title")]
    for blk in blocks[:MAX_BLOCKS]:
        t = blk.get("type")
        if t in ("h1", "h2", "h3"):
            body.append(_para(blk["text"], style={"h1": "Heading1", "h2": "Heading2", "h3": "Heading3"}[t]))
        elif t == "bullet":
            body.append(_para("•  " + blk["text"], indent=(567, 283)))
        elif t == "num":
            body.append(_para(f'{blk.get("n", 1)}.  {blk["text"]}', indent=(567, 283)))
        elif t == "table" and blk.get("rows"):
            body.append(_table(blk["rows"]))
        else:
            body.append(_para(blk.get("text", "")))
    sect = (
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" w:header="708" w:footer="708" w:gutter="0"/>'
        "</w:sectPr>"
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body>{"".join(body)}{sect}</w:body></w:document>'
    )
    core = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>{_xml_escape(_clean(title))}</dc:title><dc:creator>{_xml_escape(_clean(author))}</dc:creator>"
        "</cp:coreProperties>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("word/document.xml", document)
        z.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        z.writestr("word/styles.xml", _STYLES)
        z.writestr("docProps/core.xml", core)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# PDF (reportlab, polices standard WinAnsi : accents français OK)
# --------------------------------------------------------------------------- #
def _winansi(text: str) -> str:
    """Remplace ce que les polices standard ne savent pas afficher (emojis...)."""
    return _clean(text).encode("cp1252", errors="replace").decode("cp1252")


def pdf_bytes(title: str, blocks: list[dict], author: str = "CHARYAM", theme: str = "doc") -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    def esc(s: str) -> str:
        return _xml_escape(_winansi(s))

    base = getSampleStyleSheet()
    st = {
        "title": ParagraphStyle("t", parent=base["Title"], fontName="Helvetica-Bold", fontSize=20, spaceAfter=12,
                                textColor=colors.HexColor("#" + COLOR_NAVY)),
        "h1": ParagraphStyle("h1", parent=base["Heading1"], fontName="Helvetica-Bold", fontSize=18, spaceBefore=0,
                             spaceAfter=4, textColor=colors.HexColor("#" + COLOR_NAVY)),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=12, spaceBefore=12,
                             spaceAfter=2, textColor=colors.HexColor("#" + COLOR_TEAL)),
        "h3": ParagraphStyle("h3", parent=base["Heading3"], fontName="Helvetica-Bold", fontSize=11, spaceBefore=8,
                             textColor=colors.HexColor("#" + COLOR_NAVY)),
        "p": ParagraphStyle("p", parent=base["BodyText"], fontName="Helvetica", fontSize=10.5, leading=14, spaceAfter=6),
        "li": ParagraphStyle("li", parent=base["BodyText"], fontName="Helvetica", fontSize=10.5, leading=14,
                             leftIndent=18, firstLineIndent=-10, spaceAfter=3),
        "cell": ParagraphStyle("c", parent=base["BodyText"], fontName="Helvetica", fontSize=9.5, leading=12),
        "cellb": ParagraphStyle("cb", parent=base["BodyText"], fontName="Helvetica-Bold", fontSize=9.5, leading=12),
    }
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm,
                            bottomMargin=2 * cm, title=_winansi(title), author=_winansi(author))
    story = [] if theme == "cv" else [Paragraph(esc(title), st["title"])]
    for blk in blocks[:MAX_BLOCKS]:
        t = blk.get("type")
        if t in ("h1", "h2", "h3"):
            story.append(Paragraph(esc(blk["text"]), st[t]))
            if t == "h2":
                story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#" + COLOR_TEAL),
                                        spaceBefore=1, spaceAfter=5))
        elif t == "bullet":
            story.append(Paragraph("•&nbsp;&nbsp;" + esc(blk["text"]), st["li"]))
        elif t == "num":
            story.append(Paragraph(f'{int(blk.get("n", 1))}.&nbsp;&nbsp;' + esc(blk["text"]), st["li"]))
        elif t == "table" and blk.get("rows"):
            rows = blk["rows"]
            ncols = max(len(r) for r in rows)
            data = [
                [Paragraph(esc(c), st["cellb" if i == 0 else "cell"]) for c in (r + [""] * (ncols - len(r)))]
                for i, r in enumerate(rows)
            ]
            tbl = Table(data, colWidths=[(A4[0] - 4 * cm) / ncols] * ncols, repeatRows=1)
            tbl.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#9DB8B7")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#" + COLOR_TINT)),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))
            story += [tbl, Spacer(1, 8)]
        else:
            story.append(Paragraph(esc(blk.get("text", "")), st["p"]))
    doc.build(story)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Devis : calculs vérifiables (Decimal)
# --------------------------------------------------------------------------- #
def _dec(value, name: str) -> Decimal:
    s = str(value).replace(" ", "").replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"{name} invalide : {value!r}") from None
    if not d.is_finite():
        raise ValueError(f"{name} invalide : {value!r}")
    return d


def _round(d: Decimal) -> int:
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fcfa(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def compute_quote(lines: list[dict], tva_rate="0") -> dict:
    """lines : [{"designation", "quantite", "prix_unitaire"}]. Montants en FCFA entiers.

    La virgule est lue comme séparateur décimal ; les espaces sont ignorés.
    Arrondi : au franc, moitié vers le haut, ligne par ligne.
    """
    if not lines:
        raise ValueError("Aucune ligne dans le devis")
    rate = _dec(tva_rate, "TVA")
    if not (Decimal(0) <= rate <= Decimal(100)):
        raise ValueError("TVA hors de 0-100 %")
    out, total_ht = [], 0
    for i, ln in enumerate(lines, 1):
        qty = _dec(ln.get("quantite"), f"quantité ligne {i}")
        pu = _dec(ln.get("prix_unitaire"), f"prix unitaire ligne {i}")
        if qty <= 0:
            raise ValueError(f"quantité ligne {i} doit être positive")
        if pu < 0:
            raise ValueError(f"prix unitaire ligne {i} ne peut pas être négatif")
        total = _round(qty * pu)
        total_ht += total
        out.append({"n": i, "designation": _clean(ln.get("designation", "")), "quantite": qty,
                    "prix_unitaire": pu, "total": total})
    tva = _round(Decimal(total_ht) * rate / 100)
    return {"lignes": out, "total_ht": total_ht, "tva_taux": rate, "tva": tva, "total_ttc": total_ht + tva}


def _qty(d: Decimal) -> str:
    return str(d.normalize()) if d != d.to_integral() else str(int(d))


def quote_blocks(quote: dict, objet: str = "", client: str = "", donnees_client: list[str] | None = None,
                 hypotheses: list[str] | None = None) -> list[dict]:
    """Blocs prêts pour docx_bytes/pdf_bytes. Sépare données fournies et hypothèses."""
    blocks: list[dict] = []
    if objet:
        blocks.append({"type": "p", "text": f"Objet : {objet}"})
    if client:
        blocks.append({"type": "p", "text": f"Client : {client}"})
    rows = [["N°", "Désignation", "Qté", "Prix unitaire (FCFA)", "Total (FCFA)"]]
    for ln in quote["lignes"]:
        rows.append([str(ln["n"]), ln["designation"], _qty(ln["quantite"]),
                     fcfa(_round(ln["prix_unitaire"])), fcfa(ln["total"])])
    rows.append(["", "Total HT", "", "", fcfa(quote["total_ht"])])
    rows.append(["", f'TVA ({_qty(quote["tva_taux"])} %)', "", "", fcfa(quote["tva"])])
    rows.append(["", "Total TTC", "", "", fcfa(quote["total_ttc"])])
    blocks.append({"type": "h2", "text": "Détail estimatif"})
    blocks.append({"type": "table", "rows": rows})
    if donnees_client:
        blocks.append({"type": "h2", "text": "Données fournies par le client"})
        blocks += [{"type": "bullet", "text": x} for x in donnees_client]
    if hypotheses:
        blocks.append({"type": "h2", "text": "Hypothèses (à valider)"})
        blocks += [{"type": "bullet", "text": x} for x in hypotheses]
    blocks.append({"type": "p", "text": "Devis estimatif : les montants dépendent des hypothèses ci-dessus."})
    return blocks


# --------------------------------------------------------------------------- #
# Noms de fichiers
# --------------------------------------------------------------------------- #
def safe_filename(name: str, ext: str, default: str = "document") -> str:
    base = unicodedata.normalize("NFKD", _clean(name)).encode("ascii", "ignore").decode()
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", base).strip("_")[:60] or default
    return f"{base}.{ext.lstrip('.')}"
