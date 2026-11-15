"""Unified answer-card identity band.

Reads the student-number field of a teacher-configured answer card from OCR blocks that
were already saved. Never calls OCR, never reads answers, never guesses digits.
"""
import hashlib
import json
import re
import sqlite3
from contextlib import closing

VERSION = 'AC1'
ENTRY_MODES = ('handwritten',)  # 'bubbles' only after the fill-in reader passes acceptance.
CODE_ALPHABET = 'ACDEFGHJKMNPRTUVWXY'  # no digits or look-alike letters (B/I/L/O/Q/S/Z)
MARKER = re.compile(r'AC1\s*[-－—]\s*([A-Za-z]{6})')
LABEL = re.compile(r'学号\s*(?:后\s*[0-9０-９]+\s*位|[（(]\s*[0-9０-９]+\s*位\s*[)）])\s*[:：]?')
STOP = re.compile(r'学号|姓名|名字|班级|模板|AC1|第\s*[0-9０-９]+\s*/')
FULLWIDTH = str.maketrans('０１２３４５６７８９', '0123456789')
EDGE = '，,。.;；、:：'


def migrate(database):
    with closing(sqlite3.connect(database)) as conn, conn:
        columns = {r[1] for r in conn.execute('PRAGMA table_info(assignments)')}
        if 'answer_card_identity' not in columns:
            conn.execute("ALTER TABLE assignments ADD COLUMN answer_card_identity TEXT NOT NULL DEFAULT '{}'")


def format_code(aid, entry_mode, id_length, id_prefix):
    digest = hashlib.sha256(f'{VERSION}|{entry_mode}|{aid}|{id_length}|{id_prefix}'.encode()).digest()
    return VERSION + '-' + ''.join(CODE_ALPHABET[b % len(CODE_ALPHABET)] for b in digest[:6])


def snapshot_of(assignment):
    try:
        value = json.loads(assignment.get('answer_card_identity') or '{}')
    except (json.JSONDecodeError, AttributeError):
        return None
    return value if isinstance(value, dict) and value.get('format_code') else None


def suggestion(codes):
    """Suggest a format from the fixed roster; None plus a reason when not applicable."""
    if not codes:
        return None, '本次应交名单为空，无法建议学号格式。'
    if not all(re.fullmatch(r'[0-9]+', c) for c in codes):
        return None, '名单学号含字母或符号，首版答题卡学号栏不适用，请继续使用原有身份方式。'
    if len({len(c) for c in codes}) != 1:
        return None, '名单学号位数不一致，首版答题卡学号栏不适用，请继续使用原有身份方式。'
    length = len(codes[0])
    if length > 20:
        return None, '学号超过 20 位，首版答题卡学号栏不适用。'
    prefix = codes[0]
    for code in codes[1:]:
        while not code.startswith(prefix):
            prefix = prefix[:-1]
    prefix = prefix[:length - 1] if len(prefix) >= length else prefix
    return {'entry_mode': 'handwritten', 'id_length': length, 'id_prefix': prefix,
            'variable_count': length - len(prefix)}, ''


def validate(payload, codes):
    """Return (entry_mode, id_length, id_prefix) or raise ValueError with a teacher-facing reason."""
    mode = payload.get('entry_mode')
    if mode not in ENTRY_MODES:
        raise ValueError('首版仅支持“手写数字”学号栏；填涂识别尚未完成验收，暂不可选。')
    length = payload.get('id_length')
    if isinstance(length, bool) or not isinstance(length, int) or not 1 <= length <= 20:
        raise ValueError('学号总位数须为 1—20 的整数。')
    prefix = payload.get('id_prefix', '')
    if not isinstance(prefix, str) or not re.fullmatch(r'[0-9]*', prefix):
        raise ValueError('预印前缀只能填写数字，或留空。')
    if len(prefix) >= length:
        raise ValueError('预印前缀须短于学号总位数，至少留一位由学生填写。')
    _, problem = suggestion(codes)
    if problem:
        raise ValueError(problem)
    if any(len(c) != length for c in codes):
        raise ValueError(f'本次名单学号为 {len(codes[0])} 位，与设置的 {length} 位不符。')
    if any(not c.startswith(prefix) for c in codes):
        raise ValueError(f'本次名单并非全部以“{prefix}”开头，不能预印该前缀。')
    return mode, length, prefix


def build_snapshot(aid, mode, length, prefix):
    return {'version': VERSION, 'entry_mode': mode, 'id_length': length, 'id_prefix': prefix,
            'variable_count': length - len(prefix), 'format_code': format_code(aid, mode, length, prefix)}


def compatible(snapshot, code):
    return bool(re.fullmatch(r'[0-9]+', code or '')) and len(code) == snapshot['id_length'] \
        and code.startswith(snapshot['id_prefix'])


def describe(snapshot):
    if snapshot['id_prefix']:
        return f"{snapshot['id_length']} 位，预印前缀 {snapshot['id_prefix']}，学生填写后 {snapshot['variable_count']} 位"
    return f"{snapshot['id_length']} 位，学生全部填写"


def field_label(snapshot):
    if snapshot['id_prefix']:
        return f"学号后{snapshot['variable_count']}位："
    return f"学号（{snapshot['id_length']}位）："


def _clean(value):
    return re.sub(r'\s+', '', value.translate(FULLWIDTH)).strip(EDGE)


def read_band(blocks, *, format_snapshot, students):
    """Return discrete band evidence, or None when the assignment has no card format.

    No confidence is produced: the cloud adapter returns none, and box positions are
    approximate layout regions that cannot locate individual digit cells.
    """
    if not format_snapshot:
        return None
    snapshot = format_snapshot
    texts = [(b.get('id', i), str(b.get('text') or '')) for i, b in enumerate(blocks or [])]
    expected = snapshot['format_code']
    band = {'state': 'absent', 'raw_text': '', 'code': None, 'observed_suffix': None, 'student_id': None,
            'reason': '', 'block_ids': [], 'format_code': expected}
    markers = {f'AC1-{m.upper()}' for _, t in texts for m in MARKER.findall(t)}
    readings = []
    for index, (bid, text) in enumerate(texts):
        for label in LABEL.finditer(text):
            tail = text[label.end():]
            stop = STOP.search(tail)
            ids = [bid]
            value = tail[:stop.start()] if stop else tail
            if not tail.strip(' \t' + EDGE) and index + 1 < len(texts):
                nxt = texts[index + 1][1]
                stop = STOP.search(nxt)
                value = nxt[:stop.start()] if stop else nxt
                ids.append(texts[index + 1][0])
            readings.append((label.group(0) + value, value, ids))
    band['raw_text'] = '\n'.join(r[0] for r in readings)
    band['block_ids'] = list(dict.fromkeys(i for r in readings for i in r[2]))
    others = sorted(markers - {expected})
    if others:
        band.update(state='template_mismatch',
                    reason=f"照片上的模板标识 {'、'.join(others)} 不是本次答题卡（{expected}），请核对是否混入其他作业。")
        return band
    if not readings:
        band['reason'] = '未读到答题卡学号栏，按完整学号/姓名处理。' if markers else ''
        return band
    if expected not in markers:
        band['reason'] = f'未读到本次模板标识 {expected}，不按学号栏数字推断，按完整学号/姓名处理。'
        return band
    values = list(dict.fromkeys(_clean(r[1]) for r in readings))
    if len(values) > 1:
        band.update(state='conflict', reason=f"学号栏出现多个不一致读数（{'、'.join(v or '空' for v in values)}），请核对原图。")
        return band
    value, need = values[0], snapshot['variable_count']
    band['observed_suffix'] = value
    problem = ('学号栏未填写或未识别到数字' if not value
               else '学号栏含非数字字符（不自动改写为数字）' if not re.fullmatch(r'[0-9]+', value)
               else f'学号栏少 {need - len(value)} 位' if len(value) < need
               else f'学号栏多 {len(value) - need} 位' if len(value) > need else '')
    if problem:
        band.update(state='incomplete', reason=f'{problem}（读作“{value}”，应为 {need} 位），不作为学号使用。')
        return band
    code = snapshot['id_prefix'] + value
    found = [s for s in students if s['student_code'] == code]
    if len(found) != 1:
        band.update(state='conflict', code=None,
                    reason=f'学号栏读作 {code}，不在本次有效应交名单中（或为免交学生），不靠相似姓名转配。'
                    if not found else f'学号 {code} 在名单中不唯一，请核对。')
        return band
    shown = f'尾号 {value} → {code}' if snapshot['id_prefix'] else code
    band.update(state='valid', code=code, student_id=found[0]['id'],
                reason=f'学号栏读作 {shown}（本次模板 {expected}），对应名单唯一。')
    return band
