"""Read-only PDF/Word answer cards; never receive reference answers or a roster."""
import io
import os
from functools import lru_cache
from pathlib import Path
from xml.sax.saxutils import escape

INSTRUCTIONS = '请按题号在横线上作答，每题只保留一份清楚的最终答案。需修改时，划去旧答案，在本题空白处重写。每页均填写学号和姓名。'


def paginate(questions):
    pages, current, units = [], [], 0
    for question in questions:
        cost = 3 if question['kind'] == 'sentence' else 1
        if current and units + cost > 10:
            pages.append(current)
            current, units = [], 0
        # Export only blank answer spaces, never reference answers or article text.
        current.append({'number': question['number'], 'kind': question['kind']})
        units += cost
    if current:
        pages.append(current)
    return pages


@lru_cache(maxsize=1)
def pdf_font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    candidates = [Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts/simsun.ttc',
                  Path('/usr/share/fonts/truetype/arphic/uming.ttc')]
    for path in candidates:
        if path.is_file():
            pdfmetrics.registerFont(TTFont('AnswerCardCJK', str(path), subfontIndex=0))
            return 'AnswerCardCJK'
    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    return 'STSong-Light'


def pdf_card(title, pages, class_name, card_format, label):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfgen import canvas
    from reportlab.platypus import Paragraph

    stream = io.BytesIO()
    document = canvas.Canvas(stream, pagesize=A4)
    document.setTitle(title + ' · 独立答题卡')
    font = pdf_font()
    left, width = 16 * mm, A4[0] - 32 * mm

    def text(value, top, size=11):
        paragraph = Paragraph(escape(value), ParagraphStyle('card', fontName=font,
                              fontSize=size, leading=size * 1.5, wordWrap='CJK'))
        _, height = paragraph.wrap(width, A4[1])
        paragraph.drawOn(document, left, top - height)
        return top - height

    for index, questions in enumerate(pages, 1):
        y = text(title + ' · 独立答题卡', A4[1] - 16 * mm, 18) - 4 * mm
        y = text(f'班级：{class_name or "________________"}　姓名：____________', y) - 3 * mm
        if card_format:
            if card_format['id_prefix']:
                y = text('学号前缀：' + card_format['id_prefix'], y) - 3 * mm
            document.setFont(font, 11)
            document.drawString(left, y - 7 * mm, label)
            count = card_format['variable_count']
            box_width = min(9 * mm, (width - 42 * mm) / count)
            for digit in range(count):
                document.rect(left + 42 * mm + digit * box_width, y - 11 * mm, box_width, 11 * mm)
            y -= 15 * mm
            y = text(f'模板：{card_format["format_code"]} · 第 {index} / {len(pages)} 页', y, 10)
            instructions = '学号方格内每格只写一个阿拉伯数字；请勿涂改模板编号。' + INSTRUCTIONS
        else:
            y = text('学号：________________', y) - 2 * mm
            y = text(f'第 {index} / {len(pages)} 页', y, 10)
            instructions = INSTRUCTIONS
        y = text(instructions, y - 3 * mm, 10) - 3 * mm
        document.setFont(font, 12)
        for question in questions:
            document.drawString(left, y - 9 * mm, str(question['number']) + '.')
            for _ in range(3 if question['kind'] == 'sentence' else 1):
                y -= 14 * mm
                document.setStrokeColorRGB(.45, .49, .55)
                document.line(left + 15 * mm, y, left + width, y)
            y -= 4 * mm
        document.showPage()
    document.save()
    return stream.getvalue()


def word_card(title, pages, class_name, card_format, label):
    from docx import Document
    from docx.enum.table import WD_ROW_HEIGHT_RULE
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt

    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Mm(210), Mm(297)
    section.top_margin = section.bottom_margin = Mm(16)
    section.left_margin = section.right_margin = Mm(16)
    normal = document.styles['Normal']
    normal.font.name, normal.font.size = 'Arial', Pt(11)
    normal.element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'), '宋体')
    normal.paragraph_format.space_after = Pt(4)
    document.core_properties.title = title + ' · 独立答题卡'

    def border(parent, element, side):
        borders = parent.find(qn(element))
        if borders is None:
            borders = OxmlElement(element)
            parent.append(borders)
        edge = OxmlElement('w:' + side)
        for key, value in [('val', 'single'), ('sz', '6'), ('color', '737D8C')]:
            edge.set(qn('w:' + key), value)
        borders.append(edge)

    for index, questions in enumerate(pages, 1):
        if index > 1:
            document.add_page_break()
        heading = document.add_paragraph()
        heading.add_run(title + ' · 独立答题卡').font.size = Pt(18)
        document.add_paragraph(f'班级：{class_name or "________________"}　姓名：____________')
        if card_format:
            if card_format['id_prefix']:
                document.add_paragraph('学号前缀：' + card_format['id_prefix'])
            count = card_format['variable_count']
            grid = document.add_table(rows=1, cols=count + 1)
            grid.autofit = False
            grid.columns[0].width = Mm(42)
            grid.cell(0, 0).text = label
            grid.cell(0, 0).width = Mm(42)
            grid.rows[0].height, grid.rows[0].height_rule = Mm(11), WD_ROW_HEIGHT_RULE.AT_LEAST
            for digit in range(count):
                cell = grid.cell(0, digit + 1)
                grid.columns[digit + 1].width = cell.width = Mm(min(9, 136 / count))
                for side in ('top', 'left', 'bottom', 'right'):
                    border(cell._tc.get_or_add_tcPr(), 'w:tcBorders', side)
            document.add_paragraph(f'模板：{card_format["format_code"]} · 第 {index} / {len(pages)} 页')
            instructions = '学号方格内每格只写一个阿拉伯数字；请勿涂改模板编号。' + INSTRUCTIONS
        else:
            document.add_paragraph('学号：________________')
            document.add_paragraph(f'第 {index} / {len(pages)} 页')
            instructions = INSTRUCTIONS
        document.add_paragraph(instructions)
        table = document.add_table(rows=0, cols=2)
        table.autofit = False
        table.columns[0].width, table.columns[1].width = Mm(15), Mm(163)
        for question in questions:
            row = table.add_row()
            row._tr.get_or_add_trPr().append(OxmlElement('w:cantSplit'))
            row.cells[0].text = str(question['number']) + '.'
            lines = 3 if question['kind'] == 'sentence' else 1
            for line in range(lines):
                paragraph = row.cells[1].paragraphs[0] if not line else row.cells[1].add_paragraph()
                paragraph.paragraph_format.line_spacing = Mm(14)
                paragraph.paragraph_format.space_after = Mm(3 if line == lines - 1 else 0)
                border(paragraph._p.get_or_add_pPr(), 'w:pBdr', 'bottom')
                border(paragraph._p.get_or_add_pPr(), 'w:pBdr', 'between')
                paragraph.add_run(' ')
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()
