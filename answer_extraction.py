"""Extract literal OCR text between a numbered blank and its printed context.

Reference answers are deliberately not an input. Original blocks stay unchanged.
"""
import re
import unicodedata
from difflib import SequenceMatcher

MARKER = re.compile(r'[（(]\s*(\d{1,3})\s*[)）]')
CARD_NUMBER = re.compile(r'^\s*(?:[（(]\s*(\d{1,3})\s*[)）]|(\d{1,3})\s*[.．、:：)）])\s*(.*)$')


def worksheet_candidates(questions, pages):
    """Map explicit paper sections/numbers; never use reference answers to locate ink."""
    configured = [q for q in questions if q.get('paper_section') and q.get('paper_number')]
    found = {}
    for page in pages:
        if page['status'] in ('error', 'running') or page.get('answer_layout') == 'separate':
            continue
        section, active, segments = '', None, []
        for block in reading_order(page['blocks']):
            for line in block['text'].splitlines():
                title = re.match(r'^\s*(?:Section\s+([A-Za-z0-9]+)|([IVX]+)[.．、]\s*\S.*)\s*$', line, re.I)
                if title:
                    section = (title[1] or title[2]).casefold()
                    active = None
                    # Preserve an unnumbered first answer as an explicitly uncertain proposal.
                    if any(q['paper_section'].casefold() == section and str(q['paper_number']) == '1' for q in configured):
                        active = {'section': section, 'number': '1', 'lines': [], 'regions': [], 'unnumbered': True}
                        segments.append(active)
                    continue
                if re.search(r'Summary|Directions:', line, re.I):
                    active = None
                    section = ''
                    continue
                marker = CARD_NUMBER.match(line)
                if marker:
                    n = marker[1] or marker[2]
                    if active and active.get('unnumbered') and active['number'] == n and not active['lines']:
                        segments.remove(active)
                    # OCR may repeat an isolated number directly below its question.
                    if active and active['number'] == n and not marker[3].strip():
                        continue
                    anchored = [q for q in configured if q.get('context') and
                                re.sub(r'\s+', '', unicodedata.normalize('NFKC',q['context'].splitlines()[0])) ==
                                re.sub(r'\s+', '', unicodedata.normalize('NFKC',marker[3]))]
                    active = {'section': anchored[0]['paper_section'].casefold() if len(anchored)==1 else section,
                              'number': n, 'lines': [], 'regions': []}
                    segments.append(active)
                    line = marker[3]
                if active is not None:
                    if re.search(r'姓名|学号|第\s*\d+\s*页|周测卷|Summary|Directions:', line, re.I):
                        continue
                    if (active['regions'] and any(re.search(r'[A-Za-z]',x) and not re.search(r'[\u4e00-\u9fff]',x) for x in active['lines'])
                            and not marker and block.get('approximate')):
                        previous = active['regions'][-1]['box']
                        box = block['box']
                        intersection = max(0,min(previous[2],box[2])-max(previous[0],box[0])) * max(0,min(previous[3],box[3])-max(previous[1],box[1]))
                        area = min((previous[2]-previous[0])*(previous[3]-previous[1]),(box[2]-box[0])*(box[3]-box[1]))
                        if intersection < area * .5:
                            active = None
                            continue
                    active['lines'].append(line)
                    region = {'page_id': page['id'], 'box': block['box'], 'block_ids': [block['id']]}
                    if region not in active['regions']:
                        active['regions'].append(region)
        for q in configured:
            hits = [s for s in segments if s['section'] == q['paper_section'].strip().casefold()
                    and s['number'] == str(q['paper_number'])]
            if len(hits) != 1:
                continue
            segment = hits[0]
            english = [line for line in segment['lines'] if not re.search(r'[\u4e00-\u9fff]', line) and re.search(r'[A-Za-z]',line)]
            if q['kind'] in ('word', 'phrase'):
                # Only explicit OCR answer spans justify separating ink from printed context.
                fragments = re.findall(r'\\(?:underline|uwave)\{(?:\\text\{([^{}]*)\}|([^{}]*))\}', '\n'.join(english))
                transcript = ' '.join(a or b for a, b in fragments).strip()
            else:
                transcript = ' '.join(plain_ocr(line).strip() for line in english).strip()
            if not transcript or not re.search(r'[A-Za-z]', transcript):
                continue
            found.setdefault(q['question_id'], []).append(dict(question_id=q['question_id'],
                transcript=transcript, regions=segment['regions'], source='worksheet_section',
                uncertain=True, reason='已按卷面分组和原题号提取；请核对作答区域及印刷文字后确认。'))
    return [values[0] for values in found.values() if len(values) == 1]


def card_lines(page):
    for block in reading_order(page['blocks']):
        for line in plain_ocr(block['text']).splitlines():
            yield block, line, CARD_NUMBER.match(line)


def number_occurrences(page):
    if page.get('answer_layout') == 'separate':
        return [int(m[1] or m[2]) for _, _, m in card_lines(page) if m]
    return [int(a or b) for block in page['blocks'] for a, b in re.findall(
        r'[（(]\s*(\d{1,3})\s*[)）]|^\s*(\d{1,3})\s*[.．、:：)）]', block['text'], re.MULTILINE)]


def card_candidates(questions, pages):
    """Explicit numbered answers only; never consult the answer key or article."""
    by_number = {q['number']: q for q in questions}
    found = {}
    for page in pages:
        if page.get('answer_layout') != 'separate' or page['status'] in ('error', 'running'):
            continue
        active = None
        for block, line, marker in card_lines(page):
            if marker:
                n = int(marker[1] or marker[2])
                active = None
                if n not in by_number:
                    continue
                active = {'question_id': by_number[n]['question_id'], 'transcript': marker[3], 'regions': []}
                found.setdefault(n, []).append(active)
            elif active is not None:
                if re.search(r'姓名|学号|班级|日期|第\s*\d+\s*页', line):
                    active = None
                    continue
                active['transcript'] += ' ' + line
            if active is not None:
                region = {'page_id': page['id'], 'box': block['box'], 'block_ids': [block['id']]}
                if region not in active['regions']:
                    active['regions'].append(region)
    result = []
    for n, values in found.items():
        item = values[0]
        item['transcript'] = ' '.join(item['transcript'].split())
        if len(values) == 1 and item['transcript'] and not re.search(r'[_＿]', item['transcript']):
            result.append(item)
    return result


def reading_order(blocks):
    """VL paragraph coordinates describe geometry, not the direction of reading."""
    if blocks and all(type(b.get('reading_order')) is int for b in blocks):
        return sorted(blocks, key=lambda b: b['reading_order'])
    if blocks and all(b.get('approximate') for b in blocks):
        return list(blocks)  # Also supports saved VL results from before reading_order was added.
    # ponytail: adjacent horizontal lines only; complex columns need explicit region selection.
    rows = []
    for block in sorted(blocks, key=lambda b: (b['box'][1], b['box'][0])):
        box = block['box']
        if rows:
            anchor, line = rows[-1]
            same_line = (abs((box[1]+box[3])-(anchor[1]+anchor[3])) <= min(box[3]-box[1], anchor[3]-anchor[1])
                         and all(box[0] >= b['box'][2] or box[2] <= b['box'][0] for b in line))
            if same_line:
                line.append(block)
                continue
        rows.append((box, [block]))
    return [block for _, line in rows for block in sorted(line, key=lambda b: b['box'][0])]


def plain_ocr(value):
    # Remove only known presentation wrappers, never spelling/case/punctuation.
    value = re.sub(r'\$\s*(\\(?:underline|uwave)\{\\text\{[^{}]*\}\}|\\(?:text|underline|uwave|mathrm)\{[^{}]*\})\s*\$', r'\1', value)
    value = re.sub(r'\\(?:underline|uwave)\{\\text\{([^{}]*)\}\}', r'\1', value)
    value = re.sub(r'\\(?:text|underline|uwave|mathrm)\{([^{}]*)\}', r'\1', value)
    return value


def printed_context_matches(tokens, segment):
    """Locate printed words only. Never approximate or rewrite the student's answer."""
    exact = list(re.finditer(r'\s*'.join(re.escape(t) for t in tokens), segment, flags=re.IGNORECASE))
    if exact:
        return [(m.start(), m.end()) for m in exact]
    expected = re.findall(r'\w+', ' '.join(tokens).lower())
    if len(expected) < 6 or not re.fullmatch(r'\w+', tokens[0]):
        return []  # Short context cannot justify a tolerant boundary.
    observed = list(re.finditer(r'\w+', segment))
    matches = []
    for start in range(len(observed)-len(expected)+1):
        if observed[start].start() > 100:
            break  # No answer can be longer than 100 characters; bound the search as well.
        changes, different = 0, 0
        for word, match in zip(expected, observed[start:start+len(expected)]):
            actual = match[0].lower()
            if word == actual:
                continue
            if min(len(word),len(actual)) < 4:
                break
            ops = SequenceMatcher(None, word, actual, autojunk=False).get_opcodes()
            edits = sum(max(j-i,l-k) for op,i,j,k,l in ops if op != 'equal')
            if edits > (2 if len(word) >= 6 else 1):
                break
            changes += edits
            different += 1
            if different > min(2,len(expected)-5) or changes > 3:
                break
        else:
            matches.append((observed[start].start(),observed[start+len(expected)-1].end()))
    return matches


def grammar_candidates(questions, pages, diagnostics=None):
    diagnostics = diagnostics if diagnostics is not None else {}
    for q in questions:
        if q['kind'] == 'grammar':
            diagnostics[q['question_id']] = {'diagnostic': 'answer_not_located',
                'reason': '识别文字未能可靠关联本题：题号、后文或候选不唯一，请核对原图。', 'regions': []}
    found_candidates = {}
    marker_pages = {}
    for page in pages:
        if page.get('answer_layout') == 'separate':
            continue
        if page['status'] in ('error', 'running'):
            continue
        parts, spans, position = [], [], 0
        for block in reading_order(page['blocks']):
            part = plain_ocr(block['text'])
            parts.append(part)
            spans.append((position, position + len(part), block))
            position += len(part) + 1
        text = '\n'.join(parts)
        markers = list(MARKER.finditer(text))
        for marker in markers:
            marker_pages.setdefault(int(marker[1]), set()).add(page['id'])
        for q in questions:
            if q['kind'] != 'grammar':
                continue
            blank = re.search(rf'[（(]\s*{q["number"]}\s*[)）]\s*[_＿]{{3,}}', q.get('context', ''))
            hits = [m for m in markers if int(m[1]) == q['number']]
            if not blank or not hits:
                continue
            suffix = q['context'][blank.end():].strip()
            tokens = re.findall(r'\w+|[^\w\s]', suffix)[:10]
            if sum(bool(re.search(r'\w', token)) for token in tokens) < 3:
                continue
            anchors = [(tokens, False)]
            # The printed context is authoritative even when an imported hint field is empty.
            hint_prefix = re.match(r'[（(]\s*[A-Za-z][A-Za-z -]*\s*[)）]\s*', suffix)
            if hint_prefix:
                hint_tokens = re.findall(r'\w+|[^\w\s]', suffix[hint_prefix.end():])[:10]
                if sum(bool(re.search(r'\w', token)) for token in hint_tokens) >= 3:
                    anchors.append((hint_tokens, True))
            candidates = []
            for marker in hits:
                end_marker = next((m.start() for m in markers if m.start() > marker.start()), len(text))
                segment = text[marker.end():end_marker]
                for anchor_tokens, without_hint in anchors:
                    matches = printed_context_matches(anchor_tokens, segment)
                    if len(matches) != 1:
                        continue
                    match = matches[0]
                    candidate = ' '.join(segment[:match[0]].strip(' _＿\n\t').split())
                    if without_hint:
                        # A printed prompt can itself be mistranscribed; only remove it at its known position.
                        candidate = re.sub(r'\s*[（(]\s*[A-Za-z][A-Za-z -]*\s*[)）]\s*$', '', candidate).rstrip()
                    # Nearby printed markers/blank lines are layout noise, not an isolated answer.
                    if (MARKER.search(candidate) or re.search(r'[_＿]', candidate)
                        or (hint_prefix and re.search(r'[（(]\s*[a-zA-Z]+\s*[)）]', candidate))):
                        continue
                    candidates.append((marker, match, segment, candidate))
                    break
            if len(candidates) != 1:
                continue
            marker, match, segment, candidate = candidates[0]
            evidence = [{'page_id': page['id'], 'box': b['box'], 'block_ids': [b['id']]}
                        for start, stop, b in spans
                        if start < marker.end()+match[1] and stop > marker.start()]
            if not candidate:
                diagnostics[q['question_id']] = {'diagnostic': 'ocr_no_answer',
                    'reason': '识别结果在本题题号与印刷后文之间未返回作答文字；不等于学生未作答，请核对原图。',
                    'regions': evidence}
                continue
            if len(candidate) > 100 or len(candidate.split()) > 8 or re.search(r'[{}\\<>]', candidate):
                continue
            regions = [{'page_id': page['id'], 'box': b['box'], 'block_ids': [b['id']]}
                       for start, stop, b in spans
                       if start < marker.end()+match[0] and stop > marker.start()]
            item = {'question_id': q['question_id'], 'transcript': candidate, 'regions': regions}
            if ('\ufffd' in candidate or re.fullmatch(r'[（(].*[)）]', candidate)
                or any(candidate.count(left) != candidate.count(right) for left,right in [('(',')'),('（','）')])):
                item.update(uncertain=True, reason='识别作答含括号或无法还原的字符，保留原文，请对照原图核对。')
            found_candidates.setdefault(q['question_id'], []).append(item)
    found = {qid: values[0] for qid, values in found_candidates.items()
             if len(values) == 1 and len(marker_pages.get(next(q['number'] for q in questions if q['question_id'] == qid), set())) == 1}
    return list(found.values())

def answer_diagnostics(questions, pages):
    diagnostics = {}
    grammar_candidates(questions, pages, diagnostics)
    return diagnostics


def missing_answer_crop(page, questions):
    """Crop a known numbered blank with its neighboring printed text, never an answer key."""
    blocks = reading_order(page['blocks'])
    numbers = {q['number'] for q in questions if page.get('answer_layout') == 'separate' or q['kind'] == 'grammar'}
    selected = []
    for index, block in enumerate(blocks):
        if numbers.intersection(number_occurrences(dict(page, blocks=[block]))):
            selected.append(block['box'])
            # A blank at a paragraph boundary can continue in the next provider block.
            if index+1 < len(blocks):
                selected.append(blocks[index+1]['box'])
    if not selected:
        return None
    width, height = page['width'], page['height']
    padding = max(16, round(min(width, height)*.02))
    box = [max(0, int(min(b[0] for b in selected))-padding), max(0, int(min(b[1] for b in selected))-padding),
           min(width, int(max(b[2] for b in selected))+padding), min(height, int(max(b[3] for b in selected))+padding)]
    if (box[2]-box[0])*(box[3]-box[1]) >= width*height*.9:
        return None  # Whole-page / unlocalized boxes cannot justify pretending to target a blank.
    return box
