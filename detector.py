import math

import cv2
import numpy as np


def crop_dial(image, cfg):
    """Crop the analog dial using ratios so the setup survives resolution changes."""
    height, width = image.shape[:2]
    left = int(width * cfg["crop_left"])
    right = width - int(width * cfg["crop_right"])
    top = int(height * cfg["crop_top"])
    bottom = height - int(height * cfg["crop_bottom"])

    left, right = max(0, left), min(width, right)
    top, bottom = max(0, top), min(height, bottom)

    if right - left < 10 or bottom - top < 10:
        return image.copy()
    return image[top:bottom, left:right].copy()


def _arc(image, cfg):
    height, width = image.shape[:2]
    cx = width * cfg["pivot_x_ratio"]
    cy = height * cfg["pivot_y_ratio"]
    radius = min(width, height) * cfg["arc_radius_ratio"]

    gray = cv2.GaussianBlur(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    angles = np.linspace(
        cfg["arc_start_deg"],
        cfg["arc_end_deg"],
        int(cfg["arc_num_points"]),
    )

    values = []
    coords = []
    for degrees in angles:
        radians = math.radians(degrees)
        x = int(round(cx + radius * math.cos(radians)))
        y = int(round(cy - radius * math.sin(radians)))
        coords.append((x, y))
        values.append(float(gray[y, x]) if 0 <= x < width and 0 <= y < height else 255.0)

    values = np.asarray(values, dtype=np.float32)
    if np.ptp(values) < 5:
        return None, image.copy(), 0.0

    threshold = values.min() + np.ptp(values) * cfg["arc_dark_fraction"]
    flags = values <= threshold

    ranges = []
    start = None
    for index, flag in enumerate(flags):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            ranges.append((start, index - 1))
            start = None
    if start is not None:
        ranges.append((start, len(flags) - 1))

    if not ranges:
        return None, image.copy(), 0.0

    best = max(ranges, key=lambda value: value[1] - value[0])
    index = (best[0] + best[1]) // 2
    target_x, target_y = coords[index]
    angle = math.degrees(math.atan2(-(target_y - cy), target_x - cx)) % 360

    debug = image.copy()
    for (x, y), flag in zip(coords, flags):
        if 0 <= x < width and 0 <= y < height:
            cv2.circle(
                debug,
                (x, y),
                1,
                (0, 0, 255) if flag else (180, 180, 180),
                -1,
            )

    cv2.circle(debug, (int(cx), int(cy)), 5, (255, 0, 0), -1)
    cv2.line(debug, (int(cx), int(cy)), (target_x, target_y), (0, 255, 255), 3)

    segment_length = best[1] - best[0] + 1
    confidence = float(flags[best[0] : best[1] + 1].mean()) * min(
        1.0, segment_length / 8.0
    )
    return angle, debug, confidence


def _adaptive_edges(channel):
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    blur = cv2.GaussianBlur(clahe.apply(channel), (5, 5), 0)
    median = np.median(blur)
    low = int(max(5, median * 0.5))
    high = int(min(255, median * 1.5))
    if high <= low:
        high = min(255, low + 30)
    return cv2.Canny(blur, low, high)


def _hough(image, cfg):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blue, green, red = cv2.split(image)
    masks = [_adaptive_edges(channel) for channel in [gray, blue, green, red]]

    edges = masks[0]
    for mask in masks[1:]:
        edges = cv2.bitwise_or(edges, mask)

    kernel = np.ones((3, 3), np.uint8)
    edges = cv2.dilate(
        cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2),
        kernel,
        iterations=1,
    )
    lines = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 180,
        30,
        minLineLength=25,
        maxLineGap=20,
    )
    if lines is None:
        return None, image.copy(), 0.0

    height, width = image.shape[:2]
    cx = width * cfg["pivot_x_ratio"]
    cy = height * cfg["pivot_y_ratio"]
    min_dimension = min(width, height)
    candidates = []

    for x1, y1, x2, y2 in lines[:, 0]:
        vx, vy = x2 - x1, y2 - y1
        denominator = vx * vx + vy * vy
        if denominator == 0:
            continue

        projection = ((cx - x1) * vx + (cy - y1) * vy) / denominator
        projected_x = x1 + projection * vx
        projected_y = y1 + projection * vy
        distance = math.hypot(projected_x - cx, projected_y - cy)
        if distance > min_dimension * 0.25:
            continue

        length = math.hypot(vx, vy)
        score = length - distance * 0.4 - abs(length - min_dimension * 0.40) * 0.5
        candidates.append((score, (x1, y1, x2, y2)))

    if not candidates:
        return None, image.copy(), 0.0

    score, (x1, y1, x2, y2) = max(candidates, key=lambda item: item[0])
    vector_1 = (x1 - cx, y1 - cy)
    vector_2 = (x2 - cx, y2 - cy)
    vector = vector_1 if math.hypot(*vector_1) >= math.hypot(*vector_2) else vector_2
    vector_length = math.hypot(*vector)

    if vector_length < 1e-3:
        return None, image.copy(), 0.0

    ux, uy = vector[0] / vector_length, vector[1] / vector_length
    radius = min_dimension * 0.45
    end_x = int(cx + ux * radius)
    end_y = int(cy + uy * radius)
    angle = math.degrees(math.atan2(-(end_y - cy), end_x - cx)) % 360

    debug = image.copy()
    cv2.circle(debug, (int(cx), int(cy)), 5, (255, 0, 0), -1)
    cv2.line(debug, (int(cx), int(cy)), (end_x, end_y), (0, 255, 0), 3)

    confidence = max(0.0, min(1.0, score / (min_dimension * 0.45)))
    return angle, debug, confidence


def detect(image, cfg):
    """Run the configured detector, with ARC -> Hough fallback in auto mode."""
    method = cfg.get("detector_method", "auto")
    if method == "auto":
        methods = [("arc", _arc), ("hough", _hough)]
    elif method == "arc":
        methods = [("arc", _arc)]
    elif method == "hough":
        methods = [("hough", _hough)]
    else:
        raise ValueError(f"Unsupported detector method: {method}")

    for name, detector in methods:
        angle, debug, confidence = detector(image, cfg)
        if angle is not None:
            return {
                "angle": angle,
                "debug": debug,
                "method": name,
                "confidence": confidence,
            }

    return {
        "angle": None,
        "debug": image.copy(),
        "method": None,
        "confidence": 0.0,
    }


def angle_to_temperature(angle, points, offset=0.0):
    """Interpolate an angle against user-calibrated angle/temperature points."""
    if angle is None or not points:
        return None

    base = float(points[0]["angle"])
    pairs = []
    for point in points:
        relative_angle = (base - float(point["angle"])) % 360.0
        pairs.append((relative_angle, float(point["temp"])))
    pairs.sort(key=lambda pair: pair[0])

    x = (base - float(angle)) % 360.0
    xs = [pair[0] for pair in pairs]
    ys = [pair[1] for pair in pairs]
    if x < xs[0] or x > xs[-1]:
        return None

    return float(np.interp(x, xs, ys)) + float(offset)
