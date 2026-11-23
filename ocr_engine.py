"""Local CPU OCR. Never pass reference answers to the recognizer."""
import importlib.metadata
import json
import os
import threading
import time
from pathlib import Path

_model = None
_identity_model = None
_localization_model = None
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


def recognize_offline(path):
    """Require cached models; this fallback must never install or download anything."""
    cache = Path.home()/'.paddlex'/'official_models'
    for name in ('PP-OCRv5_mobile_det', 'en_PP-OCRv5_mobile_rec'):
        if not all((cache/name/file).is_file() for file in ('inference.json', 'inference.pdiparams')):
            raise ValueError('本机精确定位模型未缓存。')
    return recognize(path, offline=True)


def recognize(path, identity=False, *, offline=False):
    global _model, _identity_model, _localization_model
    # ponytail: one CPU inference at a time; add a worker queue only for multi-teacher use.
    if not _lock.acquire(blocking=False):
        raise RuntimeError('识别器正在处理另一张图片，请稍后重试。')
    start = time.monotonic()
    try:
        model = _identity_model if identity else _localization_model if offline else _model
        detection_model = 'PP-OCRv5_mobile_det' if offline else 'PP-OCRv5_server_det'
        recognition_model = 'PP-OCRv5_mobile_rec' if identity else 'en_PP-OCRv5_mobile_rec'
        if model is None:
            if offline:
                os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK'] = 'True'
            else:
                os.environ.setdefault('PADDLE_PDX_MODEL_SOURCE', 'BOS')
            from paddleocr import PaddleOCR
            model = PaddleOCR(
                text_detection_model_name=detection_model,
                text_recognition_model_name=recognition_model,
                device='cpu', cpu_threads=4, enable_mkldnn=False,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False, use_textline_orientation=False,
                **({'text_detection_model_dir': str(Path.home()/'.paddlex/official_models/PP-OCRv5_mobile_det'),
                    'text_recognition_model_dir': str(Path.home()/'.paddlex/official_models/en_PP-OCRv5_mobile_rec'),
                    'text_recognition_batch_size': 1}
                   if offline else {}))
            if identity:
                _identity_model = model
            elif offline:
                _localization_model = model
            else:
                _model = model
        blocks = []
        raw = []
        for result in model.predict(str(path), text_rec_score_thresh=0.0,
                                    **({'text_det_limit_side_len': 960, 'text_det_limit_type': 'max'} if offline else {})):
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
                'model': detection_model + ' + ' + recognition_model}
    finally:
        _lock.release()
