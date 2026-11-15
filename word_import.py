"""Import editable grammar worksheets as drafts; never infer missing answers."""
import io
import re
import sqlite3
import zipfile
from contextlib import closing
from datetime import datetime

from docx import Document
from docx.text.paragraph import Paragraph

WORD_LIMIT = 5 * 1024 * 1024
BLANK = re.compile(r'[（(]\s*(\d{1,3})\s*[)）]\s*[_＿]{3,}(?:\s*[（(]([A-Za-z][^()（）\n]{0,79})[)）])?')
ANSWER = re.compile(r'(?<!\d)(\d{1,3})\s*[.．、:：)）]\s*(.*?)(?=\s+\d{1,3}\s*[.．、:：)）]\s*|$)', re.S)
ANSWER_HEADING = re.compile(r'^\s*(?:参考答案|标准答案|答案|answer\s*key|answers?)\s*[:：]?\s*$', re.I)


def migrate_word_fields(database):
    fields = {'tasks': ['passage', 'instructions', 'source_name'], 'questions': ['context', 'hint']}
    with closing(sqlite3.connect(database)) as conn, conn:
        missing = [(table, name) for table, names in fields.items() for name in names
                   if name not in {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}]
        if not missing:
            return
        backup = str(database) + '.bak-word-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        with closing(sqlite3.connect(backup)) as destination:
            conn.backup(destination)
        conn.execute('BEGIN IMMEDIATE')
        for table, name in missing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")


def word_paragraphs(raw):
    if len(raw) > WORD_LIMIT:
        raise ValueError('Word 超过 5MB，请删除无关图片或拆分试卷后上传。')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            parts = archive.infolist()
            if len(parts) > 1000 or sum(p.file_size for p in parts) > 20 * 1024 * 1024:
                raise ValueError('Word 解压内容过大，请拆分后上传。')
            if 'word/document.xml' not in archive.namelist():
                raise ValueError('文件不是有效的 Word DOCX。')
        doc = Document(io.BytesIO(raw))
        paragraphs = [Paragraph(p, doc).text.strip() for p in doc.element.body.iter()
                      if p.tag.endswith('}p')]
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('Word 无法读取，可能已损坏或加密。请另存为 .docx 后重试。') from exc
    if len(paragraphs) > 10000 or sum(map(len, paragraphs)) > 200000:
        raise ValueError('Word 文字过多，请按单篇试题拆分。')
    return paragraphs


def parse_word(raw, filename):
    paragraphs = word_paragraphs(raw)
    answer_start = next((i for i, line in enumerate(paragraphs) if ANSWER_HEADING.fullmatch(line)), None)
    if answer_start is None:
        raise ValueError('未找到独立的“答案”标题。请在文档末尾加入答案区（如：1. when  2. with）。扫描图片暂不支持。')
    prompt = paragraphs[:answer_start]
    answers = {}
    for line in paragraphs[answer_start+1:]:
        for match in ANSWER.finditer(line):
            n, answer = int(match[1]), ' '.join(match[2].split())
            if n in answers:
                raise ValueError(f'答案区第 {n} 题重复，请按单篇试题拆分或删除重复答案。')
            answers[n] = answer
    questions, seen = [], set()
    warnings = []
    instruction = '\n'.join(line for line in prompt if re.search(r'每[题空]|总分|共.{0,5}分|大小写', line))
    score = re.search(r'每[题空]\s*([一二三四五六七八九十]|\d+(?:\.\d+)?)\s*分', instruction)
    points = 1
    if score:
        value = score[1]
        points = '一二三四五六七八九十'.index(value)+1 if value in '一二三四五六七八九十' else float(value)
        if not 0 < points <= 1000:
            raise ValueError('每题分值不在允许范围内，请修改 Word 评分说明。')
    else:
        warnings.append('没有找到明确的每题分值，暂按每题 1 分，请核对。')
    for paragraph in prompt:
        for blank in BLANK.finditer(paragraph):
            n = int(blank[1])
            if n < 1 or n in seen:
                raise ValueError(f'题号 {n} 重复或无效，请按单篇、唯一题号拆分试题。')
            seen.add(n)
            # Context is the containing sentence; the full passage stays available separately.
            starts = [m.end() for m in re.finditer(r'[.!?]\s+', paragraph[:blank.start()])]
            start = starts[-1] if starts else 0
            end_match = re.search(r'[.!?](?:\s|$)', paragraph[blank.end():])
            end = blank.end()+end_match.end() if end_match else len(paragraph)
            context = paragraph[start:end].strip()
            answer = answers.get(n, '')
            if not answer:
                warnings.append(f'第 {n} 题缺少答案，请填写后再创建。')
            questions.append({'number': n, 'kind': 'grammar', 'answer': answer, 'variants': [],
                              'points': points, 'context': context, 'hint': (blank[2] or '').strip()})
    if not questions:
        raise ValueError('未找到“(题号) ______”形式的填空题。暂不支持扫描图片或任意题型自动解析。')
    if len(questions) > 200:
        raise ValueError('每次最多导入 200 道题，请拆分试卷。')
    extra = sorted(set(answers)-seen)
    if extra:
        warnings.append('答案区存在未匹配的题号：'+', '.join(map(str, extra))+'，请核对是否漏题。')
    warnings.append('候选内容尚未保存。请逐题核对原题、大小写、答案和分值；评分沿用严格匹配规则。')
    return {'name': filename.rsplit('.', 1)[0][:120], 'source_name': filename[:240],
            'instructions': instruction, 'passage': '\n\n'.join(line for line in prompt if line),
            'questions': sorted(questions, key=lambda q:q['number']), 'warnings': warnings}


ANSWER_GROUPS = {
    '半句': ('phrase', '半句翻译'), '半句翻译': ('phrase', '半句翻译'),
    '整句': ('sentence', '整句翻译'), '整句翻译': ('sentence', '整句翻译'),
    '句子翻译': ('sentence', '整句翻译'),
    'summary writing': ('summary', '概要写作'), 'summary': ('summary', '概要写作'),
    '概要写作': ('summary', '概要写作'),
}
NUMBERED_LINE = re.compile(r'^(?:[（(]([1-9]\d{0,2})[）)]|([1-9]\d{0,2})[.．、:：)）])\s*(.*)$')
VERSION_LINE = re.compile(r'^(version\s*\d+|版本\s*(?:\d+|[一二三四五六七八九十]+))(?=\s|[:：.．、]|$)[\s:：.．、]*(.*)$', re.I)


def parse_answer_word(raw, filename):
    groups, warnings = [], []
    group = item = version = None
    for paragraph in word_paragraphs(raw):
        for raw_line in paragraph.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            heading = ' '.join(line.rstrip(':：').split()).casefold()
            if heading in ANSWER_GROUPS:
                kind, label = ANSWER_GROUPS[heading]
                if group and group['kind'] == kind and not group['items']:
                    continue
                group = {'kind': kind, 'label': label, 'items': []}
                groups.append(group)
                item = version = None
                continue
            if heading in {'翻译', '翻译答案', '翻译批改答案'} or ANSWER_HEADING.fullmatch(line):
                continue
            if group and group['kind'] == 'summary':
                match = VERSION_LINE.fullmatch(line)
                if item is None:
                    item = {'number': 1, 'versions': []}
                    group['items'].append(item)
                if match or version is None:
                    version = {'label': match[1] if match else '参考答案', 'answer': match[2] if match else line}
                    item['versions'].append(version)
                else:
                    version['answer'] = '\n'.join(filter(None, (version['answer'], line)))
                continue
            match = NUMBERED_LINE.fullmatch(line)
            if match:
                if group is None:
                    group = {'kind': 'unclassified', 'label': '未分组答案', 'items': []}
                    groups.append(group)
                    warnings.append('未找到分组标题，请核对题号并选择题型。')
                item = {'number': int(match[1] or match[2]), 'answer': match[3]}
                group['items'].append(item)
            elif item is not None:
                item['answer'] = '\n'.join(filter(None, (item['answer'], line)))
            else:
                warnings.append(f'请核对未归入题目的内容：{line}')
    groups = [group for group in groups if group['items']]
    total = sum(len(group['items']) for group in groups)
    if not total:
        raise ValueError('未找到可拆分的答案。请使用分组标题和“1. 答案”格式，或在答案表手动录入。')
    if total > 200:
        raise ValueError('每次最多预览 200 道题，请拆分答案文档。')
    for group in groups:
        numbers = [item['number'] for item in group['items']]
        if len(set(numbers)) < len(numbers):
            warnings.append(f"{group['label']}中有重复题号，已保留各条答案，请核对后勾选。")
    return {'name': filename.rsplit('.', 1)[0][:120], 'source_name': filename[:240],
            'groups': groups, 'warnings': warnings}
