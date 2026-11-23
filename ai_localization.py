"""Offline line geometry for review only; never replace primary OCR or teacher work."""
import math
import tempfile
from pathlib import Path

from PIL import Image
from answer_extraction import grammar_candidates, card_candidates, number_occurrences, reading_order


def original_box(box, width, height, rotation):
    if rotation == 90:
        return [width-box[3], box[0], width-box[1], box[2]]
    if rotation == 180:
        return [width-box[2], height-box[3], width-box[0], height-box[1]]
    if rotation == 270:
        return [box[1], height-box[2], box[3], height-box[0]]
    return list(box)


def offline_blocks(detail, uploads):
    from ocr_engine import recognize_offline
    result = {}
    turns = {90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_270}
    for page in detail['pages']:
        if page['status'] != 'done':
            continue
        with tempfile.TemporaryDirectory(prefix='ai-localization-') as folder:
            path = Path(folder)/'upright.png'
            with Image.open(uploads[page['id']]) as image:
                angle = page.get('display_rotation', 0)
                upright = image.transpose(turns[angle]) if angle in turns else image.copy()
                upright.save(path, 'PNG')
                width, height = upright.size
            recognized = recognize_offline(path)
        blocks = []
        for block in reading_order(recognized['blocks']):
            box = block.get('box')
            if (block.get('approximate') or not isinstance(block.get('text'), str)
                    or not isinstance(box, list) or len(box) != 4
                    or any(type(x) not in (int,float) or not math.isfinite(x) for x in box)
                    or not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)):
                raise ValueError('Invalid offline geometry')
            blocks.append(dict(block, id=len(blocks), reading_order=len(blocks),
                box=original_box(box, page['width'], page['height'], angle)))
        result[page['id']] = blocks
    return result


def localized_detail(detail, blocks):
    pages = [dict(p, blocks=blocks[p['id']]) for p in detail['pages'] if p['id'] in blocks]
    found = {q['question_id']: q for q in grammar_candidates(detail['reviews'], pages) + card_candidates(detail['reviews'], pages)}
    reviews = []
    for q in detail['reviews']:
        candidate = found.get(q['question_id'])
        occurrences = sum(number_occurrences(p).count(q['number']) for p in pages)
        if not candidate or occurrences != 1 or not candidate['transcript'].strip() or candidate.get('uncertain'):
            reviews.append(q)
            continue
        # Precise neighboring regions are geometry only, never edits to a teacher's decision.
        updated = dict(q, regions=candidate['regions'])
        if q['uncertain'] and not q['teacher_edited'] and not q['confirmed']:
            original = q['auto_result'] or {}
            updated['auto_result'] = dict(original, source='localization_ocr',
                transcript=candidate['transcript'], regions=candidate['regions'],
                localization_original=original, localization_model='local_line_ocr',
                recognition_evidence=original.get('recognition_evidence', []) +
                    [dict(source='localization_ocr',transcript=candidate['transcript'])])
        reviews.append(updated)
    return dict(detail, reviews=reviews)
