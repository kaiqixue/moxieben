"""Local CPU OCR. Never pass reference answers to the recognizer."""
import importlib.metadata
import json
import os
import threading
import time

_model = None
_identity_model = None
_lock = threading.Lock()


def status():
    packages = {}
    for package in ('paddleocr', 'paddlepaddle'):
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            packages[package] = None
    return {'installed': all(packages.values()), 'loaded': _model is not None,
            'device': 'cpu', 'packages': packages,
            'model': 'PP-OCRv5_server_det + en_PP-OCRv5_mobile_rec'}


def recognize_identity(path):
    return recognize(path, identity=True)


def recognize(path, identity=False):
    global _model, _identity_model
    # ponytail: one CPU inference at a time; add a worker queue only for multi-teacher use.
    if not _lock.acquire(blocking=False):
        raise RuntimeError('识别器正在处理另一张图片，请稍后重试。')
    start = time.monotonic()
    try:
        model = _identity_model if identity else _model
        recognition_model = 'PP-OCRv5_mobile_rec' if identity else 'en_PP-OCRv5_mobile_rec'
        if model is None:
            os.environ.setdefault('PADDLE_PDX_MODEL_SOURCE', 'BOS')
            from paddleocr import PaddleOCR
            model = PaddleOCR(
                text_detection_model_name='PP-OCRv5_server_det',
                text_recognition_model_name=recognition_model,
                device='cpu', cpu_threads=4, enable_mkldnn=False,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False, use_textline_orientation=False)
            if identity:
                _identity_model = model
            else:
                _model = model
        blocks = []
        raw = []
        for result in model.predict(str(path), text_rec_score_thresh=0.0):
            data = result.json
            if isinstance(data, str):
                data = json.loads(data)
            raw.append(data)
            data = data.get('res', data)
            for text, confidence, polygon in zip(data['rec_texts'], data['rec_scores'], data['rec_polys']):
                xs = [float(p[0]) for p in polygon]
                ys = [float(p[1]) for p in polygon]
                blocks.append({'id': len(blocks), 'text': text, 'confidence': float(confidence),
                               'box': [min(xs), min(ys), max(xs), max(ys)]})
        return {'blocks': blocks, 'raw': raw, 'elapsed_seconds': round(time.monotonic() - start, 2),
                'model': 'PP-OCRv5_server_det + ' + recognition_model}
    finally:
        _lock.release()
