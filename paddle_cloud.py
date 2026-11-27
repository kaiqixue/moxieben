"""PaddleOCR AI Studio Jobs adapter. Never sends reference answers or a roster."""
import hashlib
import json
import math
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from PIL import Image, ImageDraw

JOB_URL = 'https://paddleocr.aistudio-app.com/api/v2/ocr/jobs'
MODEL = 'PaddleOCR-VL-1.6'
MAX_WAIT = 180
RESULT_LIMIT = 8 * 1024 * 1024
OPTIONS = {'useDocOrientationClassify': True, 'useDocUnwarping': False, 'useChartRecognition': False,
           'useOcrForImageBlock': True, 'markdownIgnoreLabels': []}


def _check(response):
    if response.status_code in (401, 403):
        raise ValueError('PaddleOCR Token 无效、过期或无权限，请在识别设置中更新。')
    if response.status_code == 429:
        raise ValueError('PaddleOCR 请求过多或额度不足，请检查控制台额度。')
    if not 200 <= response.status_code < 300:
        if response.status_code==400:
            try:
                code=response.json().get('code')
            except (ValueError,AttributeError):
                code=None
            reasons={10001:'上传文件为空',10002:'文件链接无法识别',10003:'文件大小超过限制',
                     10004:'文件格式不支持',10005:'文件内容无法解析',10006:'文件页数超过限制',
                     10007:'模型名无效，请检查模型配置',10008:'请求参数无效，请检查识别参数',
                     10009:'批次任务数超过限制',11002:'云端任务已过期，请明确重试'}
            if type(code) is int and code in reasons:
                raise ValueError(f'PaddleOCR 服务返回 HTTP 400（{code}）：{reasons[code]}。')
        raise ValueError(f'PaddleOCR 服务返回 HTTP {response.status_code}，请稍后重试并检查接口配置。')


def _data(response):
    _check(response)
    try:
        value = response.json()['data']
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (KeyError, TypeError, ValueError):
        raise ValueError('PaddleOCR 返回的任务数据格式无效。') from None


def _parse_result(raw, width, height):
    pages = []
    try:
        for line in raw.decode('utf-8-sig').splitlines():
            if line.strip():
                parsed = json.loads(line)['result']['layoutParsingResults']
                if not isinstance(parsed, list):
                    raise ValueError()
                pages.extend(parsed)
        if len(pages) != 1:
            raise ValueError()
        page = pages[0]
        pruned = page.get('prunedResult') or {}
        if isinstance(pruned, str):
            pruned = json.loads(pruned)
        regions = pruned.get('parsing_res_list') or []
        markdown = page.get('markdown', {}).get('text')
        if not isinstance(markdown, str) and not regions:
            raise ValueError()
        angle = pruned.get('doc_preprocessor_res', {}).get('angle', 0)
        if angle == -1:
            angle = 0
        # Paddle rotates counterclockwise before layout detection. Display boxes on the unchanged input.
        rotated_width, rotated_height = (height, width) if angle in (90, 270) else (width, height)
        blocks = []
        # Keep non-body regions in place; order body regions by the provider's explicit reading sequence.
        ordered = sorted((r for r in regions if type(r.get('block_order')) is int), key=lambda r: r['block_order'])
        ordered = iter(ordered)
        regions = [next(ordered) if type(r.get('block_order')) is int else r for r in regions]
        for region in regions:
            text = region['block_content']
            box = region.get('block_bbox')
            if not isinstance(text, str):
                raise ValueError()
            valid = (isinstance(box, list) and len(box) == 4 and
                     all(type(v) in (int, float) and math.isfinite(v) for v in box))
            if not valid or angle not in (0, 90, 180, 270) or not (0 <= box[0] < box[2] <= rotated_width and 0 <= box[1] < box[3] <= rotated_height):
                box = [0, 0, width, height]
            elif angle == 90:
                box = [width-box[3], box[0], width-box[1], box[2]]
            elif angle == 180:
                box = [width-box[2], height-box[3], width-box[0], height-box[1]]
            elif angle == 270:
                box = [box[1], height-box[2], box[3], height-box[0]]
            for line in text.splitlines():
                if line.strip():
                    blocks.append({'id': len(blocks), 'text': line.strip(), 'box': box,
                                   'confidence': None, 'approximate': True, 'reading_order': len(blocks)})
        if not blocks and isinstance(markdown, str):
            # ponytail: markdown-only results use the whole page; teachers map complex layouts manually.
            for line in markdown.splitlines():
                if line.strip():
                    blocks.append({'id': len(blocks), 'text': line.strip(), 'box': [0, 0, width, height],
                                   'confidence': None, 'approximate': True, 'reading_order': len(blocks)})
        if len(blocks) > 2000:
            raise ValueError()
        return {'blocks': blocks, 'raw': {'pruned_result': pruned, 'markdown': markdown or ''}}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError('PaddleOCR 识别结果格式无效或不是单张照片结果，请重试；不能据此判定学生未作答。') from None


def recognize(path, config, *, db=None):
    """Submit, poll with a deadline, then read JSONL without downloading optional images."""
    endpoint = config.get('paddle_url', JOB_URL).rstrip('/')
    token = config.get('paddle_token', '')
    model = config.get('paddle_model', MODEL)
    if not token:
        raise ValueError('请先在识别设置中填写 PaddleOCR Token。')
    if config.get('outbound_hosts'):
        from production import validate_outbound_url
        validate_outbound_url(endpoint, config['outbound_hosts'])
    with Image.open(path) as image:
        width, height = image.size
    started = time.monotonic()
    headers = {'Authorization': 'bearer ' + token}
    options = dict(OPTIONS, useLayoutDetection=config.get('paddle_layout_detection', True))
    input_sha256 = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    # Only unfinished requests are resumed. A completed explicit rerun is a fresh request.
    request_key = hashlib.sha256(json.dumps([endpoint, model, options, input_sha256,
        hashlib.sha256(token.encode()).hexdigest()], sort_keys=True).encode()).hexdigest()
    def forget():
        if db:
            with db() as conn:
                conn.execute('DELETE FROM cloud_ocr_jobs WHERE request_key=?', (request_key,))
    job_id = None
    if db:
        with db() as conn:
            saved = conn.execute('SELECT job_id FROM cloud_ocr_jobs WHERE request_key=?', (request_key,)).fetchone()
            job_id = saved['job_id'] if saved else None
    try:
        if not job_id:
            with Path(path).open('rb') as file:
                response = requests.post(endpoint, headers=headers,
                    data={'model': model, 'optionalPayload': json.dumps(options)},
                    files={'file': ('photo.png', file, 'image/png')}, timeout=(10, 30), allow_redirects=False)
            job_id = _data(response).get('jobId')
            if not isinstance(job_id, str) or not job_id or len(job_id) > 240:
                raise ValueError('PaddleOCR 未返回有效任务编号，请检查接口配置。')
            if db:
                with db() as conn:
                    conn.execute('INSERT OR REPLACE INTO cloud_ocr_jobs(request_key,job_id) VALUES(?,?)', (request_key,job_id))
        # ponytail: sequential polling suits this single-teacher app; a worker queue is unnecessary here.
        while True:
            remaining = MAX_WAIT - (time.monotonic() - started)
            if remaining <= 0:
                raise ValueError('PaddleOCR 等待任务超时，原图和复核记录保留。云端任务可能仍在运行，请勿连续重复提交。')
            response = requests.get(endpoint + '/' + quote(job_id, safe=''), headers=headers,
                                    timeout=(min(10, remaining), min(30, remaining)), allow_redirects=False)
            if response.status_code in (404, 410):
                forget()
                raise ValueError('云端原任务已失效，本次未重新提交；再次点击重试将创建新任务。')
            data = _data(response)
            if data.get('jobId', job_id) != job_id:
                raise ValueError('PaddleOCR 返回的任务编号与提交任务不一致，未采用该结果，请重试。')
            state = data.get('state')
            if state == 'failed':
                forget()
                raise ValueError('PaddleOCR 云端任务失败，请检查照片清晰度、文件格式和控制台状态后重试。')
            if state == 'done':
                result_url = data.get('resultUrl', {}).get('jsonUrl', '')
                address = urlsplit(result_url)
                if address.scheme != 'https' or not address.hostname or address.username or address.password:
                    raise ValueError('PaddleOCR 返回的结果下载地址无效。')
                if config.get('result_hosts'):
                    from production import validate_outbound_url
                    validate_outbound_url(result_url, config['result_hosts'])
                # A pre-signed object-store URL must never receive the teacher's API Token.
                result_response = requests.get(result_url, timeout=(10, 30), allow_redirects=False, stream=True)
                try:
                    _check(result_response)
                    chunks, size = [], 0
                    for chunk in result_response.iter_content(65536):
                        size += len(chunk)
                        if size > RESULT_LIMIT:
                            raise ValueError('PaddleOCR 识别结果过大，请拆分照片。')
                        chunks.append(chunk)
                    try:
                        result = _parse_result(b''.join(chunks), width, height)
                    except ValueError:
                        forget()
                        raise
                finally:
                    result_response.close()
                forget()
                return {**result, 'provider': 'paddle_cloud', 'model': model, 'job_id': job_id,
                        'input_sha256': input_sha256, 'request_options': options,
                        'elapsed_seconds': round(time.monotonic() - started, 2),
                        'warning': 'PaddleOCR 云端位置为版面区域参考，不提供字母级定位或 OCR 置信度，请逐题核对。'}
            if state not in ('pending', 'running'):
                raise ValueError('PaddleOCR 返回未知任务状态，请检查服务后重试。')
            time.sleep(min(3, max(0, MAX_WAIT - (time.monotonic() - started))))
    except requests.Timeout:
        raise ValueError('PaddleOCR 请求超时，原图保留；任务可能已提交，请稍后重试，避免重复计费。') from None
    except requests.RequestException:
        raise ValueError('无法连接 PaddleOCR 服务，请检查网络和接口地址。') from None


def test_connection(config):
    import tempfile
    with tempfile.TemporaryDirectory(prefix='paddle-connection-') as folder:
        image = Image.new('RGB', (640, 180), 'white')
        ImageDraw.Draw(image).text((30, 60), 'OCR connection test 123', fill='black', font_size=32)
        path = Path(folder) / 'test.png'
        image.save(path)
        result = recognize(path, config)
    if not result['blocks']:
        raise ValueError('接口已返回但测试文字未识别，请检查模型配置。')
    return {'ok': True, 'message': f"PaddleOCR 图片任务识别成功，用时 {result['elapsed_seconds']} 秒。真实手写仍需核对。"}
