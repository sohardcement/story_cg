"""Local CPU detection and geometry. Independent of generation and review.

Coordinates are original-image xyxy pixels, right/bottom exclusive. area_ratio
is clipped bbox area / image area; mask_area_ratio counts segmentation pixels.
Expansion is a fraction of bbox width/height added on EACH side. Segmentation
expansion dilates by ceil(expansion * max(bbox width, height)) pixels.
"""
from contextlib import redirect_stdout
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import sys
import time

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / 'config/detect.json'


def load_detect_config(path=DEFAULT_CONFIG):
    config = json.loads(Path(path).read_text())
    if config['device'] != 'cpu':
        raise ValueError('local detection supports CPU only')
    for kind in ('faces', 'persons'):
        section = config[kind]
        for key in ('confidence', 'min_area_ratio'):
            value = section[key]
            if not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise ValueError(f'{kind}.{key} must be in [0,1]')
    if not 0 <= config['iou'] <= 1 or config['imgsz'] <= 0 or config['max_det'] <= 0:
        raise ValueError('invalid prediction settings')
    for value in (config['mask']['expansion'], config['mask']['feather_px'], config['crop']['expansion']):
        if not math.isfinite(value) or value < 0:
            raise ValueError('invalid geometry settings')
    return config


def _image(image):
    if isinstance(image, Image.Image):
        return image.convert('RGB')
    with Image.open(image) as source:
        return source.convert('RGB')


def expand_box(bbox, size, expansion):
    if not math.isfinite(expansion) or expansion < 0:
        raise ValueError('expansion must be finite and nonnegative')
    x1, y1, x2, y2 = map(float, bbox)
    if not all(map(math.isfinite, (x1, y1, x2, y2))) or x2 <= x1 or y2 <= y1:
        raise ValueError('invalid bbox')
    w, h = size
    dx, dy = (x2-x1)*expansion, (y2-y1)*expansion
    box = (max(0, math.floor(x1-dx)), max(0, math.floor(y1-dy)),
           min(w, math.ceil(x2+dx)), min(h, math.ceil(y2+dy)))
    if box[2] <= box[0] or box[3] <= box[1]:
        raise ValueError('bbox is outside image')
    return box


def feather_mask(detection, size, *, expansion=None, feather=None, config=None):
    config = config if config is not None else load_detect_config()
    expansion = config['mask']['expansion'] if expansion is None else expansion
    feather = config['mask']['feather_px'] if feather is None else feather
    if not math.isfinite(feather) or feather < 0:
        raise ValueError('feather must be finite and nonnegative')
    box = expand_box(detection['bbox'], size, expansion)
    if detection.get('mask') is not None:
        mask = detection['mask'].convert('L').copy()
        if mask.size != size:
            raise ValueError('segmentation mask must match image size')
        bbox = detection['bbox']
        radius = math.ceil(expansion * max(bbox[2]-bbox[0], bbox[3]-bbox[1]))
        if radius:
            # cv2 dilation avoids Pillow's expensive large MaxFilter windows.
            import cv2
            import numpy as np
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*radius+1, 2*radius+1))
            mask = Image.fromarray(cv2.dilate(np.array(mask), kernel))
    else:
        mask = Image.new('L', size, 0)
        ImageDraw.Draw(mask).rectangle((box[0],box[1],box[2]-1,box[3]-1), fill=255)
    return mask.filter(ImageFilter.GaussianBlur(feather)) if feather else mask


def filter_detections(items, settings):
    return [item for item in items if item['confidence'] >= settings['confidence']
            and item['area_ratio'] >= settings['min_area_ratio']]


@lru_cache(maxsize=4)
def _model(path):
    # Must be set BEFORE importing ultralytics; avoid ~/.config and remote assets.
    runtime = ROOT / '.cache/detect'
    runtime.mkdir(parents=True, exist_ok=True)
    os.environ['YOLO_CONFIG_DIR'] = str(runtime)
    os.environ['MPLCONFIGDIR'] = str(runtime / 'matplotlib')
    os.environ['YOLO_OFFLINE'] = 'true'
    os.environ['YOLO_AUTOINSTALL'] = 'false'
    pillow_open = Image.open
    try:
        with redirect_stdout(sys.stderr):
            from ultralytics import YOLO
            return YOLO(path)
    finally:
        # Ultralytics globally wraps Pillow and tries HEIF imports on ANY error.
        # Preserve the caller's PNG/JPEG/file-error behavior without extra packages.
        Image.open = pillow_open


def _detect(image, kind, config):
    config = config if config is not None else load_detect_config()
    image = _image(image)
    model_path = Path(config[kind]['model'])
    if not model_path.is_absolute():
        model_path = ROOT / model_path
    if not model_path.is_file():
        raise FileNotFoundError(f'model missing (automatic download disabled): {model_path}')
    w, h = image.size
    with redirect_stdout(sys.stderr):
        result = _model(str(model_path)).predict(image, device='cpu', verbose=False,
            conf=config[kind]['confidence'], iou=config['iou'], imgsz=config['imgsz'],
            max_det=config['max_det'], retina_masks=True, save=False)[0]
    items = []
    if result.boxes is None:
        return items
    for index, (coords, confidence) in enumerate(zip(result.boxes.xyxy.tolist(), result.boxes.conf.tolist())):
        x1,y1,x2,y2 = coords
        bbox = [max(0., min(float(w), x1)), max(0., min(float(h),y1)),
                max(0., min(float(w), x2)), max(0., min(float(h),y2))]
        area = (bbox[2]-bbox[0])*(bbox[3]-bbox[1])/(w*h)
        item = {'bbox': bbox, 'confidence': confidence, 'area_ratio': area}
        if kind == 'persons':
            item['mask'] = None
            item['mask_area_ratio'] = None
            if result.masks is not None:
                import numpy as np
                pixels = result.masks.data[index].cpu().numpy()
                mask = Image.fromarray((pixels > .5).astype(np.uint8)*255)
                if mask.size != image.size:
                    raise ValueError('retina mask does not match original image size')
                item['mask'] = mask
                item['mask_area_ratio'] = float((pixels > .5).sum()/(w*h))
        items.append(item)
    return filter_detections(items, config[kind])


def detect_faces(image, *, config=None):
    return _detect(image, 'faces', config)


def detect_persons(image, *, config=None):
    return _detect(image, 'persons', config)


def detect_image(image, *, config=None):
    config = config if config is not None else load_detect_config()
    image = _image(image)
    started = time.perf_counter()
    faces = detect_faces(image, config=config)
    faces_seconds = time.perf_counter()-started
    started = time.perf_counter()
    persons = detect_persons(image, config=config)
    persons_seconds = time.perf_counter()-started
    return {'size': list(image.size), 'faces': faces, 'persons': persons,
            'timing_seconds': {'faces': faces_seconds, 'persons': persons_seconds,
                               'total': faces_seconds+persons_seconds}}


def json_result(result, mask_dir=None):
    """JSON keeps pixel-space contours; optional lossless masks saved separately."""
    import cv2
    import numpy as np
    output = {**result, 'persons': []}
    for index, item in enumerate(result['persons']):
        item = dict(item)
        mask = item.pop('mask')
        item['mask_contours'] = []
        if mask is not None:
            contours, hierarchy = cv2.findContours(np.array(mask), cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
            item['mask_contours'] = [contour.reshape(-1,2).tolist() for contour in contours]
            item['mask_contour_hierarchy'] = hierarchy[0].tolist() if hierarchy is not None else []
            if mask_dir is not None:
                mask_dir = Path(mask_dir)
                mask_dir.mkdir(parents=True, exist_ok=True)
                path = mask_dir / f'person{index+1:02d}.png'
                mask.save(path)
                item['mask_path'] = str(path)
        output['persons'].append(item)
    return output
