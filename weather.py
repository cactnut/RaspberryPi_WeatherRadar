#!/usr/bin/env python3
"""雨雲レーダーをMHS-3.5inch GPIO LCDにインタラクティブ表示するスクリプト

機能:
- 現在のレーダー表示（デフォルト）
- タッチ操作（スワイプでマップ移動、ボタンでズーム/アニメーション/リロード）
- 過去3時間+予報1時間のアニメーション再生
- 7日間天気予報表示（3日詳細 + 4日概要）
"""

import io
import logging
import math
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum, auto
from pathlib import Path

import numpy as np
import requests
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont  # noqa: F401

load_dotenv()

# ── 設定 ──────────────────────────────────────────────

DISPLAY_WIDTH = 480
DISPLAY_HEIGHT = 320
MAP_HEIGHT = 240
FORECAST_HEIGHT = 80
FRAMEBUFFER_DEVICE = "/dev/fb1"

# デフォルト表示範囲 (.env から読み込み)
DEFAULT_ZOOM = int(os.environ.get("DEFAULT_ZOOM", "9"))
DEFAULT_LAT = float(os.environ.get("DEFAULT_LAT", "35.57796392582172"))
DEFAULT_LON = float(os.environ.get("DEFAULT_LON", "139.6994511351743"))
GRID_COLS = 3
GRID_ROWS = 2
TILE_SIZE = 256

# デフォ位置の緯度経度からデフォルトタイル座標を算出 (グリッド中央にマーカーが来るようオフセット)
_n = 2 ** DEFAULT_ZOOM
DEFAULT_TILE_X = int((DEFAULT_LON + 180.0) / 360.0 * _n) - GRID_COLS // 2
DEFAULT_TILE_Y = int((1.0 - math.log(math.tan(math.radians(DEFAULT_LAT))
                      + 1.0 / math.cos(math.radians(DEFAULT_LAT)))
                      / math.pi) / 2.0 * _n) - GRID_ROWS // 2 + 1

# 背景地図ガンマ補正 (陸地の輝度範囲はそのまま、それ以外を明るくする)
BASE_MAP_GAMMA = 0.4
BASE_MAP_LAND_RANGE = (0, 14)  # この輝度範囲を陸地とみなし変更しない
_lo, _hi = BASE_MAP_LAND_RANGE
_GAMMA_LUT = tuple(
    i if _lo <= i <= _hi
    else int(255 * (i / 255) ** BASE_MAP_GAMMA)
    for i in range(256)
) * 3
del _lo, _hi

# レーダー透過度 (0=完全透明, 255=不透明)
RADAR_OPACITY = 80
_OPACITY_LUT = tuple(x * RADAR_OPACITY // 255 for x in range(256))

# 県庁所在地 (name, lat, lon)
CAPITALS = [
    ("札幌", 43.0621, 141.3544), ("青森", 40.8244, 140.7400),
    ("盛岡", 39.7036, 141.1527), ("仙台", 38.2688, 140.8721),
    ("秋田", 39.7186, 140.1024), ("山形", 38.2405, 140.3634),
    ("福島", 37.7503, 140.4676), ("水戸", 36.3418, 140.4468),
    ("宇都宮", 36.5658, 139.8836), ("前橋", 36.3911, 139.0608),
    ("さいたま", 35.8569, 139.6489), ("千葉", 35.6047, 140.1233),
    ("東京", 35.6895, 139.6917), ("横浜", 35.4478, 139.6425),
    ("新潟", 37.9026, 139.0236), ("富山", 36.6953, 137.2114),
    ("金沢", 36.5946, 136.6256), ("福井", 36.0652, 136.2219),
    ("甲府", 35.6642, 138.5684), ("長野", 36.6513, 138.1810),
    ("岐阜", 35.3912, 136.7223), ("静岡", 34.9769, 138.3831),
    ("名古屋", 35.1815, 136.9066), ("津", 34.7303, 136.5086),
    ("大津", 35.0045, 135.8686), ("京都", 35.0116, 135.7681),
    ("大阪", 34.6863, 135.5200), ("神戸", 34.6913, 135.1830),
    ("奈良", 34.6851, 135.8049), ("和歌山", 34.2260, 135.1675),
    ("鳥取", 35.5039, 134.2383), ("松江", 35.4723, 133.0505),
    ("岡山", 34.6617, 133.9350), ("広島", 34.3966, 132.4596),
    ("山口", 34.1861, 131.4714), ("徳島", 34.0658, 134.5593),
    ("高松", 34.3401, 134.0434), ("松山", 33.8416, 132.7657),
    ("高知", 33.5597, 133.5311), ("福岡", 33.6064, 130.4183),
    ("佐賀", 33.2494, 130.2988), ("長崎", 32.7448, 129.8737),
    ("熊本", 32.7898, 130.7417), ("大分", 33.2382, 131.6126),
    ("宮崎", 31.9111, 131.4239), ("鹿児島", 31.5602, 130.5581),
    ("那覇", 26.2124, 127.6809),
]

# ボタン設定 (マップ上に半透明オーバーレイ)
BUTTON_WIDTH = 40
BUTTON_HEIGHT = 60
BUTTON_X = DISPLAY_WIDTH - BUTTON_WIDTH
BUTTON_DEFS = [
    "reload",
    "zoom_in",
    "zoom_out",
    "play",
]

# 768x512 → 480x320 リサイズ後に中央クロップで 480x240 に
CROP_Y_OFFSET = (320 - MAP_HEIGHT) // 2  # = 40

ZOOM_MIN = 6
ZOOM_MAX = 11
# hrpns タイルは偶数ズーム (6,8,10) のみデータあり
# 表示ズームに最も近い偶数ズームでレーダーを取得する

# アニメーション
FRAME_INTERVAL = 0.5
LAST_FRAME_PAUSE = 3.0
DATA_REFRESH_INTERVAL = 300

# URL
JMA_TIMES_N1_URL = "https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N1.json"
JMA_TIMES_N2_URL = "https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N2.json"
JMA_TILE_URL = (
    "https://www.jma.go.jp/bosai/jmatile/data/nowc/{basetime}/none/"
    "{validtime}/surf/hrpns/{z}/{x}/{y}.png"
)
JMA_FORECAST_URL = "https://www.jma.go.jp/bosai/forecast/data/forecast/130000.json"
BASE_TILE_URL = "https://a.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}.png"

CACHE_DIR = Path.home() / ".cache" / "weather_radar"

USER_AGENT = "RaspberryPi-WeatherRadar/1.0 (educational project)"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})
# コネクションプール拡大 (並列タイル取得用)
adapter = requests.adapters.HTTPAdapter(
    pool_connections=4, pool_maxsize=12, max_retries=1)
SESSION.mount("https://", adapter)
SESSION.mount("http://", adapter)

JST = timezone(timedelta(hours=9))
WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── タッチキャリブレーション (要実機調整) ─────────────

TOUCH_X_MIN = 200
TOUCH_X_MAX = 3900
TOUCH_Y_MIN = 200
TOUCH_Y_MAX = 3900
TOUCH_SWAP_XY = True
TOUCH_INVERT_X = True
TOUCH_INVERT_Y = False

# ── 状態管理 ─────────────────────────────────────────


class AppMode(Enum):
    IDLE = auto()
    PLAYING = auto()


@dataclass
class AppState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    wake: threading.Event = field(default_factory=threading.Event)
    mode: AppMode = AppMode.IDLE
    zoom: int = DEFAULT_ZOOM
    tile_x_start: int = DEFAULT_TILE_X
    tile_y_start: int = DEFAULT_TILE_Y
    needs_reload: bool = False
    needs_tile_refetch: bool = False
    animation_requested: bool = False
    stop_requested: bool = False
    swipe_offset_x: int = 0
    swipe_offset_y: int = 0
    # ドラッグ中のリアルタイム追跡
    is_dragging: bool = False
    drag_dx: int = 0  # ドラッグ中の画面ピクセルオフセット
    drag_dy: int = 0


# ── 座標変換 ─────────────────────────────────────────


def latlon_to_screen(lat: float, lon: float, zoom: int,
                     tile_x_start: int, tile_y_start: int) -> tuple[float, float] | None:
    """緯度経度 → マップエリア内の画面座標。範囲外ならNone。"""
    n = 2 ** zoom
    tile_x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    tile_y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad))
              / math.pi) / 2.0 * n

    pixel_x = (tile_x - tile_x_start) * TILE_SIZE
    pixel_y = (tile_y - tile_y_start) * TILE_SIZE

    composite_w = GRID_COLS * TILE_SIZE
    composite_h = GRID_ROWS * TILE_SIZE

    # 480x320 空間でのピクセル位置を計算し、クロップオフセットを引く
    screen_x = pixel_x * DISPLAY_WIDTH / composite_w
    screen_y = pixel_y * DISPLAY_HEIGHT / composite_h - CROP_Y_OFFSET

    if 0 <= screen_x < DISPLAY_WIDTH and 0 <= screen_y < MAP_HEIGHT:
        return screen_x, screen_y
    return None


def radar_zoom(display_zoom: int) -> int:
    """表示ズームに最も近い偶数ズームを返す (レーダータイル用)。"""
    return display_zoom if display_zoom % 2 == 0 else display_zoom - 1


def _latlon_to_tile(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """緯度経度 → タイル座標 (小数)。"""
    n = 2 ** zoom
    tx = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    ty = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad))
          / math.pi) / 2.0 * n
    return tx, ty


def _tile_center_latlon(tile_x_start: int, tile_y_start: int,
                        zoom: int) -> tuple[float, float]:
    """現在のグリッド中心のタイル座標 → 緯度経度。"""
    n = 2 ** zoom
    cx = tile_x_start + GRID_COLS / 2
    cy = tile_y_start + GRID_ROWS / 2
    lon = cx / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * cy / n))))
    return lat, lon


def zoom_to(new_zoom: int, current_zoom: int,
            tile_x_start: int, tile_y_start: int) -> tuple[int, int]:
    """ズーム変更時に中心の緯度経度を維持して新しいタイル開始座標を計算。"""
    # 現在の中心を緯度経度で取得
    lat, lon = _tile_center_latlon(tile_x_start, tile_y_start, current_zoom)
    # 新ズームでのタイル座標を計算
    tx, ty = _latlon_to_tile(lat, lon, new_zoom)
    new_x = int(tx - GRID_COLS / 2)
    new_y = int(ty - GRID_ROWS / 2)
    max_tile = 2 ** new_zoom
    new_x = max(0, min(new_x, max_tile - GRID_COLS))
    new_y = max(0, min(new_y, max_tile - GRID_ROWS))
    return new_x, new_y


# ── タイル取得 ────────────────────────────────────────


def fetch_tile(url: str, cache_path: Path | None = None) -> Image.Image:
    """タイル画像を取得。cache_pathがあればキャッシュを使う。"""
    if cache_path and cache_path.exists():
        return Image.open(cache_path).convert("RGBA")

    resp = SESSION.get(url, timeout=(3, 10))
    resp.raise_for_status()
    img = Image.open(io.BytesIO(resp.content)).convert("RGBA")

    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(cache_path, "PNG")

    return img


def fetch_tile_grid(url_template: str, zoom: int, tx_start: int, ty_start: int,
                    cache_subdir: str | None = None, **fmt) -> Image.Image:
    """タイルグリッドを並列取得して合成。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    w = GRID_COLS * TILE_SIZE
    h = GRID_ROWS * TILE_SIZE
    composite = Image.new("RGBA", (w, h))

    def _fetch_one(col, row):
        tx = tx_start + col
        ty = ty_start + row
        url = url_template.format(z=zoom, x=tx, y=ty, **fmt)
        cache_path = None
        if cache_subdir:
            cache_path = CACHE_DIR / cache_subdir / f"{zoom}_{tx}_{ty}.png"
        try:
            tile = fetch_tile(url, cache_path)
        except Exception as e:
            log.warning("タイル取得失敗 (z=%s,%s,%s): %s", zoom, tx, ty, e)
            tile = Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 0, 0))
        return col, row, tile

    tasks = [(col, row) for row in range(GRID_ROWS) for col in range(GRID_COLS)]

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(_fetch_one, c, r) for c, r in tasks]
        for future in as_completed(futures):
            col, row, tile = future.result()
            composite.paste(tile, (col * TILE_SIZE, row * TILE_SIZE))

    return composite


# ── 気象庁 レーダーAPI ────────────────────────────────


def get_current_radar_time() -> dict | None:
    """最新のレーダー時刻を1つ取得。"""
    try:
        resp = SESSION.get(JMA_TIMES_N1_URL, timeout=10)
        resp.raise_for_status()
        n1 = resp.json()
        if n1:
            entry = n1[0]
            return {
                "basetime": entry["basetime"],
                "validtime": entry["validtime"],
                "is_forecast": False,
            }
    except Exception as e:
        log.error("最新時刻取得失敗: %s", e)
    return None


def get_all_radar_times() -> list[dict]:
    """過去(N1) + 予報(N2) の全時刻を時系列順で返す。"""
    frames = []

    try:
        resp = SESSION.get(JMA_TIMES_N1_URL, timeout=10)
        resp.raise_for_status()
        for entry in reversed(resp.json()):
            frames.append({
                "basetime": entry["basetime"],
                "validtime": entry["validtime"],
                "is_forecast": False,
            })
    except Exception as e:
        log.error("N1時刻取得失敗: %s", e)
        return []

    try:
        resp = SESSION.get(JMA_TIMES_N2_URL, timeout=10)
        resp.raise_for_status()
        for entry in reversed(resp.json()):
            frames.append({
                "basetime": entry["basetime"],
                "validtime": entry["validtime"],
                "is_forecast": True,
            })
    except Exception as e:
        log.warning("N2時刻取得失敗: %s", e)

    log.info("フレーム数: 過去%d + 予報%d = 合計%d",
             sum(1 for f in frames if not f["is_forecast"]),
             sum(1 for f in frames if f["is_forecast"]),
             len(frames))
    return frames


# ── 天気予報 ─────────────────────────────────────────

# 天気コード → 絵文字マッピング
# "→" = のち (天気が変わる), "/" = 時々・一時 (2番目を小さく表示)
WEATHER_EMOJI = {
    "100": "☀", "101": "☀/☁", "102": "☀/☂", "103": "☀/☂", "104": "☀/❄",
    "110": "☀→☁", "111": "☀→☁", "112": "☀→☂", "113": "☀→☂", "114": "☀→❄",
    "115": "☀→❄", "116": "☀→☂", "117": "☀→☂",
    "200": "☁", "201": "☁/☀", "202": "☁/☂", "203": "☁/☂", "204": "☁/❄",
    "210": "☁→☀", "211": "☁→☀", "212": "☁→☂", "213": "☁→☂", "214": "☁→❄",
    "215": "☁→❄", "216": "☁→☂",
    "300": "☂", "301": "☂/☀", "302": "☂/☁", "303": "☂/❄", "308": "☂☂",
    "311": "☂→☀", "312": "☂→☁", "313": "☂→❄", "314": "☂→❄",
    "400": "❄", "401": "❄/☀", "402": "❄/☁", "403": "❄/☂",
    "411": "❄→☀", "412": "❄→☁", "413": "❄→☂",
}


def weathercode_to_emoji(code: str) -> str:
    """JMA天気コード → 絵文字文字列。"""
    return WEATHER_EMOJI.get(code, "?")


def fetch_weekly_forecast() -> list[dict]:
    """JMA 7日間天気予報を取得し、日ごとのリストで返す。

    Returns:
        [{"date": date, "code": str, "pop": str, "temp_min": str,
          "temp_max": str, "is_detail": bool}, ...]
    """
    today = datetime.now(JST).date()
    days = {}
    for i in range(7):
        d = today + timedelta(days=i)
        days[d] = {
            "date": d,
            "code": "",
            "pop": "",
            "pops_detail": [],  # [(時間ラベル, 確率), ...] 3日間詳細用
            "temp_min": "",
            "temp_max": "",
            "is_detail": i < 2,  # 今日・明日が詳細
        }

    try:
        resp = SESSION.get(JMA_FORECAST_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error("天気予報取得失敗: %s", e)
        return list(days.values())

    # ── data[0]: 3日間詳細 ──
    try:
        ts0 = data[0]["timeSeries"]

        # 天気コード (area 130010)
        tokyo_w = next(a for a in ts0[0]["areas"]
                       if a["area"]["code"] == "130010")
        for i, ds in enumerate(ts0[0]["timeDefines"]):
            d = datetime.fromisoformat(ds).date()
            if d in days and i < len(tokyo_w["weatherCodes"]):
                days[d]["code"] = tokyo_w["weatherCodes"][i]

        # 降水確率 (area 130010) → 時間帯ごと + 日最大値
        tokyo_p = next(a for a in ts0[1]["areas"]
                       if a["area"]["code"] == "130010")
        pops_raw = tokyo_p["pops"]
        pop_periods = {}  # date -> {hour: value}
        for j, ds in enumerate(ts0[1]["timeDefines"]):
            dt_pop = datetime.fromisoformat(ds)
            d = dt_pop.date()
            if d in days and j < len(pops_raw):
                if d not in pop_periods:
                    pop_periods[d] = {}
                pop_periods[d][dt_pop.hour] = pops_raw[j] if pops_raw[j] else ""
                if pops_raw[j]:
                    days[d]["pops_detail"].append((dt_pop.hour, pops_raw[j]))
                    cur = days[d]["pop"]
                    if not cur or int(pops_raw[j]) > int(cur):
                        days[d]["pop"] = pops_raw[j]

        # 今日・明日: 全4期間(0,6,12,18)で足りない分を"-"埋め
        for d in [today, today + timedelta(days=1)]:
            if d in pop_periods:
                full_pops = []
                for h in [0, 6, 12, 18]:
                    if h in pop_periods[d]:
                        full_pops.append(
                            (h, pop_periods[d][h] if pop_periods[d][h] else "-"))
                    else:
                        full_pops.append((h, "-"))
                days[d]["pops_detail"] = full_pops

        # 気温 (area 44132 = 東京) → [明日min, 明日max]
        tokyo_t = next(a for a in ts0[2]["areas"]
                       if a["area"]["code"] == "44132")
        temps = tokyo_t.get("temps", [])
        if len(temps) >= 2:
            tmrw = datetime.fromisoformat(ts0[2]["timeDefines"][0]).date()
            if tmrw in days:
                days[tmrw]["temp_min"] = temps[0]
                days[tmrw]["temp_max"] = temps[1]
    except Exception as e:
        log.warning("3日間予報解析失敗: %s", e)

    # ── data[1]: 7日間 (不足分を補完) ──
    try:
        ts1 = data[1]["timeSeries"]

        # 天気コード + 降水確率 (area 130010)
        tokyo_w2 = next(a for a in ts1[0]["areas"]
                        if a["area"]["code"] == "130010")
        w_codes = tokyo_w2["weatherCodes"]
        w_pops = tokyo_w2.get("pops", [])
        for i, ds in enumerate(ts1[0]["timeDefines"]):
            d = datetime.fromisoformat(ds).date()
            if d in days:
                if not days[d]["code"] and i < len(w_codes):
                    days[d]["code"] = w_codes[i]
                if not days[d]["pop"] and i < len(w_pops) and w_pops[i]:
                    days[d]["pop"] = w_pops[i]

        # 気温 (area 44132)
        tokyo_t2 = next(a for a in ts1[1]["areas"]
                        if a["area"]["code"] == "44132")
        t_min = tokyo_t2.get("tempsMin", [])
        t_max = tokyo_t2.get("tempsMax", [])
        for i, ds in enumerate(ts1[1]["timeDefines"]):
            d = datetime.fromisoformat(ds).date()
            if d in days:
                if not days[d]["temp_min"] and i < len(t_min) and t_min[i]:
                    days[d]["temp_min"] = t_min[i]
                if not days[d]["temp_max"] and i < len(t_max) and t_max[i]:
                    days[d]["temp_max"] = t_max[i]
    except Exception as e:
        log.warning("週間予報解析失敗: %s", e)

    result = [days[today + timedelta(days=i)] for i in range(7)
              if (today + timedelta(days=i)) in days]
    return result


# ── 画像合成 ──────────────────────────────────────────


def find_japanese_font(size: int = 14) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """日本語フォントを探す。"""
    candidates = [
        "/usr/share/fonts/opentype/ipaexfont-gothic/ipaexg.ttf",
        "/usr/share/fonts/truetype/takao-gothic/TakaoGothic.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


# フォントキャッシュ
_font_cache: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def get_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if size not in _font_cache:
        _font_cache[size] = find_japanese_font(size)
    return _font_cache[size]


def _bold_text(draw: ImageDraw.ImageDraw, x: int, y: int,
               text: str, fill, font):
    """擬似太字: 1pxずらして2回描画。"""
    draw.text((x, y), text, fill=fill, font=font)
    draw.text((x + 1, y), text, fill=fill, font=font)


def draw_weather_emoji(draw: ImageDraw.ImageDraw, x: int, y: int,
                       emoji_str: str, font_large, font_small):
    """天気絵文字を描画。→は小さく下付き太字、/は2番目の絵文字を小さく表示。"""
    import re
    tokens = re.split(r'(→|/)', emoji_str)
    cur_x = x
    for i, tok in enumerate(tokens):
        if tok == "→":
            arrow_y = y + font_large.size - font_small.size
            _bold_text(draw, cur_x, arrow_y, "→",
                       fill=(200, 200, 200), font=font_small)
            bbox = font_small.getbbox("→")
            cur_x += (bbox[2] - bbox[0]) + 2
        elif tok == "/":
            slash_y = y + font_large.size - font_small.size
            _bold_text(draw, cur_x, slash_y, "/",
                       fill=(200, 200, 200), font=font_small)
            bbox = font_small.getbbox("/")
            cur_x += (bbox[2] - bbox[0]) + 1
        else:
            # 絵文字ごとの色
            if "☀" in tok:
                emoji_color = (255, 150, 100)
            elif "☂" in tok:
                emoji_color = (120, 180, 255)
            else:
                emoji_color = (255, 255, 255)
            prev = tokens[i - 1] if i > 0 else ""
            if prev == "/":
                small_y = y + font_large.size - font_small.size - 2
                font_mid = get_font(11)
                draw.text((cur_x, small_y), tok,
                          fill=emoji_color, font=font_mid)
                bbox = font_mid.getbbox(tok)
                cur_x += (bbox[2] - bbox[0]) + 1
            else:
                draw.text((cur_x, y), tok,
                          fill=emoji_color, font=font_large)
                bbox = font_large.getbbox(tok)
                cur_x += (bbox[2] - bbox[0]) + 1


def _fit_radar_to_base(base: Image.Image, radar: Image.Image,
                       base_zoom: int, base_tx: int, base_ty: int,
                       radar_zoom_level: int, radar_tx: int, radar_ty: int
                       ) -> Image.Image:
    """レーダー(偶数zoom)を背景地図(任意zoom)の座標系に合わせてオーバーレイ。"""
    if base_zoom == radar_zoom_level:
        return Image.alpha_composite(base, radar)

    # レーダーと背景のスケール比
    scale = 2 ** (base_zoom - radar_zoom_level)
    # レーダー画像を背景と同じスケールにリサイズ
    rw = int(radar.width * scale)
    rh = int(radar.height * scale)
    radar_scaled = radar.resize((rw, rh), Image.BILINEAR)

    # 背景の左上タイル座標を、レーダーズームのピクセル座標で表現
    base_px_in_radar = base_tx * TILE_SIZE / scale - radar_tx * TILE_SIZE
    base_py_in_radar = base_ty * TILE_SIZE / scale - radar_ty * TILE_SIZE

    # オフセット計算: レーダー画像のどこが背景の左上に来るか
    offset_x = -int(base_px_in_radar * scale)
    offset_y = -int(base_py_in_radar * scale)

    # 背景サイズのレーダーオーバーレイを作成
    radar_aligned = Image.new("RGBA", base.size, (0, 0, 0, 0))
    radar_aligned.paste(radar_scaled, (offset_x, offset_y))

    return Image.alpha_composite(base, radar_aligned)


def compose_map(base: Image.Image, radar: Image.Image,
                base_zoom: int, base_tx: int, base_ty: int,
                radar_zoom_level: int = 0, radar_tx: int = 0, radar_ty: int = 0,
                validtime: str = "", is_forecast: bool = False,
                frame_idx: int = 0, total_frames: int = 1) -> Image.Image:
    """背景地図 + レーダーを合成して480x240マップ画像を返す。"""
    # レーダーを半透明にして背景地図が透けて見えるようにする
    if radar.mode == "RGBA":
        r, g, b, a = radar.split()
        a = a.point(_OPACITY_LUT)
        radar = Image.merge("RGBA", (r, g, b, a))
    if radar_zoom_level and radar_zoom_level != base_zoom:
        result = _fit_radar_to_base(base, radar,
                                    base_zoom, base_tx, base_ty,
                                    radar_zoom_level, radar_tx, radar_ty)
    else:
        result = Image.alpha_composite(base, radar)
    # アスペクト比維持: 768x512 → 480x320 → 中央クロップ 480x240
    result = result.resize((DISPLAY_WIDTH, DISPLAY_HEIGHT), Image.BILINEAR)
    result = result.crop((0, CROP_Y_OFFSET, DISPLAY_WIDTH, CROP_Y_OFFSET + MAP_HEIGHT))
    # 陸地の輝度範囲はそのまま、それ以外はガンマ補正で明るくする
    result = result.convert("RGB").point(_GAMMA_LUT)

    draw = ImageDraw.Draw(result)

    # 県庁所在地
    font_city = get_font(9)
    for name, lat, lon in CAPITALS:
        pos = latlon_to_screen(lat, lon,
                               base_zoom, base_tx, base_ty)
        if pos:
            px, py = int(pos[0]), int(pos[1])
            draw.ellipse((px - 2, py - 2, px + 2, py + 2),
                         fill=(180, 180, 180, 180))
            draw.text((px + 4, py - 5), name,
                      fill=(160, 160, 160), font=font_city)

    # デフォルト位置のマーカー (十字、県庁所在地より上に描画)
    pos = latlon_to_screen(DEFAULT_LAT, DEFAULT_LON,
                           base_zoom, base_tx, base_ty)
    if pos:
        cx, cy = int(pos[0]), int(pos[1])
        draw.line((cx - 5, cy, cx + 5, cy), fill=(255, 0, 0), width=2)
        draw.line((cx, cy - 5, cx, cy + 5), fill=(255, 0, 0), width=2)

    font = get_font(12)

    # 時刻表示 (左上)
    if validtime:
        try:
            dt = datetime.strptime(validtime, "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc)
            dt_jst = dt.astimezone(JST)
            time_str = dt_jst.strftime("%m/%d %H:%M")
        except Exception:
            time_str = validtime
        label = f"[予報] {time_str}" if is_forecast else time_str
        label_color = (0, 100, 255) if is_forecast else (255, 255, 255)
        draw.rectangle((0, 0, 130, 18), fill=(0, 0, 0, 180))
        draw.text((4, 2), label, fill=label_color, font=font)

    # プログレスバー (アニメーション中のみ)
    if total_frames > 1:
        bar_y = MAP_HEIGHT - 3
        progress = (frame_idx + 1) / total_frames
        draw.rectangle((0, bar_y, DISPLAY_WIDTH, MAP_HEIGHT), fill=(40, 40, 40))
        bar_color = (0, 100, 255) if is_forecast else (180, 180, 180)
        draw.rectangle((0, bar_y, int(DISPLAY_WIDTH * progress), MAP_HEIGHT),
                       fill=bar_color)

    return result


def _draw_button_icon(draw: ImageDraw.ImageDraw, name: str,
                      cx: int, cy: int, state: AppState):
    """ボタンアイコンをPIL図形で描画 (フォント依存なし)。"""
    # ズーム限界時は暗く
    dimmed = False
    if name == "zoom_in" and state.zoom >= ZOOM_MAX:
        dimmed = True
    elif name == "zoom_out" and state.zoom <= ZOOM_MIN:
        dimmed = True
    c = (80, 80, 80, 120) if dimmed else (255, 255, 255, 230)
    is_playing = state.mode == AppMode.PLAYING
    if name == "reload":
        # 円弧 + 矢印
        r = 10
        draw.arc((cx - r, cy - r, cx + r, cy + r), start=60, end=330,
                 fill=c, width=2)
        # 矢印: 円弧の終点 (330°) に、左45°傾けた三角形
        # PIL: 0°=右, 反時計回り。330° ≈ 右下
        end_rad = math.radians(330)
        ax = cx + int(r * math.cos(end_rad))
        ay = cy - int(r * math.sin(end_rad))
        # 三角形を左45°傾ける (時計回り方向を示す)
        s = 5
        a45 = math.radians(45)
        draw.polygon([
            (ax + int(s * math.cos(a45)), ay - int(s * math.sin(a45))),
            (ax - int(s * math.cos(a45 - math.radians(90))),
             ay + int(s * math.sin(a45 - math.radians(90)))),
            (ax - int(s * math.cos(a45 + math.radians(90))),
             ay + int(s * math.sin(a45 + math.radians(90)))),
        ], fill=c)
    elif name == "zoom_in":
        draw.line((cx - 8, cy, cx + 8, cy), fill=c, width=2)
        draw.line((cx, cy - 8, cx, cy + 8), fill=c, width=2)
    elif name == "zoom_out":
        draw.line((cx - 8, cy, cx + 8, cy), fill=c, width=2)
    elif name == "play":
        if is_playing:
            # ■ 停止
            draw.rectangle((cx - 7, cy - 7, cx + 7, cy + 7), fill=c)
        else:
            # ▶ 再生
            draw.polygon([(cx - 6, cy - 9), (cx - 6, cy + 9), (cx + 8, cy)],
                         fill=c)


def draw_buttons(image: Image.Image, state: AppState) -> Image.Image:
    """マップ上にボタンを半透明オーバーレイで描画。"""
    overlay = Image.new("RGBA", (DISPLAY_WIDTH, MAP_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    for i, name in enumerate(BUTTON_DEFS):
        x1 = BUTTON_X
        y1 = i * BUTTON_HEIGHT
        x2 = DISPLAY_WIDTH
        y2 = (i + 1) * BUTTON_HEIGHT

        draw.rectangle((x1, y1, x2, y2), fill=(0, 0, 0, 120))
        draw.rectangle((x1, y1, x2, y2), outline=(100, 100, 100, 200))

        cx = x1 + BUTTON_WIDTH // 2
        cy = y1 + BUTTON_HEIGHT // 2
        _draw_button_icon(draw, name, cx, cy, state)

    image_rgba = image.convert("RGBA")
    result = Image.alpha_composite(image_rgba, overlay)
    return result.convert("RGB")


def draw_forecast_bar(image: Image.Image, forecast: list[dict]):
    """画面下部80pxに7日間天気予報を描画。"""
    draw = ImageDraw.Draw(image)
    y0 = MAP_HEIGHT

    # 背景
    draw.rectangle((0, y0, DISPLAY_WIDTH, DISPLAY_HEIGHT), fill=(20, 20, 30))

    if not forecast:
        draw.text((10, y0 + 30), "天気予報取得中...",
                  fill=(150, 150, 150), font=get_font(12))
        return

    font_s = get_font(10)
    font_m = get_font(12)
    font_emoji = get_font(16)
    font_arrow = get_font(6)  # →用: 絵文字の1/3サイズ

    def _date_color(d):
        """土=青, 日=赤, 平日=グレー"""
        if d.weekday() == 5:
            return (100, 150, 255)
        if d.weekday() == 6:
            return (255, 80, 80)
        return (200, 200, 200)

    BG_EVEN = (20, 20, 30)
    BG_ODD = (30, 30, 42)

    # 今日・明日は幅広 (降水確率が長い)、残り5日は均等割り
    detail_col_w = 116
    n_detail = min(2, len([d for d in forecast if d.get("is_detail")]))
    detail_total_w = detail_col_w * n_detail
    n_weekly = min(5, len(forecast) - n_detail)
    weekly_col_w = (DISPLAY_WIDTH - detail_total_w) // max(n_weekly, 1)

    for day_idx, day in enumerate(forecast[:7]):
        is_detail = day.get("is_detail", False)
        if is_detail:
            col_w = detail_col_w
            x = (day_idx * detail_col_w)
        else:
            weekly_i = day_idx - n_detail
            col_w = weekly_col_w
            x = detail_total_w + weekly_i * weekly_col_w

        d = day["date"]
        wd = WEEKDAY_JP[d.weekday()]

        # 背景色を交互に変える
        bg = BG_ODD if day_idx % 2 else BG_EVEN
        draw.rectangle((x, y0, x + col_w, DISPLAY_HEIGHT), fill=bg)

        pad = 4
        font_date = font_m if is_detail else font_s

        # 日付 + 曜日 (上部に4pxマージン)
        draw.text((x + pad, y0 + 6), f"{d.day}({wd})",
                  fill=_date_color(d), font=font_date)

        # 天気絵文字
        emoji = weathercode_to_emoji(day["code"])
        emoji_y = y0 + 22 if is_detail else y0 + 20
        draw_weather_emoji(draw, x + pad, emoji_y, emoji, font_emoji, font_arrow)

        # 降水確率 (絵文字との間に4pxマージン)
        pops_detail = day.get("pops_detail", [])
        pop_y = y0 + 46 if is_detail else y0 + 42
        if pops_detail:
            pop_strs = [p[1] for p in pops_detail]
            pop_text = "/".join(pop_strs) + "%"
            numeric_pops = [int(p[1]) for p in pops_detail if p[1] != "-"]
            max_pop = max(numeric_pops) if numeric_pops else 0
            pop_color = (100, 180, 255) if max_pop >= 50 else (150, 150, 150)
            draw.text((x + pad, pop_y), pop_text, fill=pop_color, font=font_s)
        elif day.get("pop") and day["pop"] != "0":
            pop_val = int(day["pop"])
            pop_color = (100, 180, 255) if pop_val >= 50 else (150, 150, 150)
            draw.text((x + pad, pop_y), f"{day['pop']}%",
                      fill=pop_color, font=font_s)

        # 気温
        tmax = day.get("temp_max", "")
        tmin = day.get("temp_min", "")
        temp_y = y0 + 60 if is_detail else y0 + 56
        if tmax or tmin:
            temp_str = f"{tmax}/{tmin}" if tmin else tmax
            draw.text((x + pad, temp_y), temp_str,
                      fill=(255, 180, 100), font=font_s)


# ── フレームバッファ ──────────────────────────────────


def detect_fb_format(device: str) -> dict:
    """フレームバッファのピクセルフォーマットを検出。"""
    fb_name = os.path.basename(device)
    sys_path = f"/sys/class/graphics/{fb_name}"

    bpp = 16
    width, height = DISPLAY_WIDTH, DISPLAY_HEIGHT

    try:
        with open(f"{sys_path}/bits_per_pixel") as f:
            bpp = int(f.read().strip())
    except Exception:
        pass

    try:
        with open(f"{sys_path}/virtual_size") as f:
            parts = f.read().strip().split(",")
            width, height = int(parts[0]), int(parts[1])
    except Exception:
        pass

    log.info("フレームバッファ: %s (%dx%d, %dbpp)", device, width, height, bpp)
    return {"bpp": bpp, "width": width, "height": height}


def image_to_rgb565(image: Image.Image) -> bytes:
    """PIL ImageをRGB565バイト列に変換。"""
    arr = np.array(image)
    r = arr[:, :, 0].astype(np.uint16)
    g = arr[:, :, 1].astype(np.uint16)
    b = arr[:, :, 2].astype(np.uint16)
    rgb565 = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
    return rgb565.tobytes()


def write_to_framebuffer(image: Image.Image, device: str, fb_format: dict):
    """画像をフレームバッファに書き込む。"""
    if image.size != (fb_format["width"], fb_format["height"]):
        img = image.resize((fb_format["width"], fb_format["height"]), Image.BILINEAR)
    else:
        img = image
    img = img.convert("RGB")

    bpp = fb_format["bpp"]
    if bpp == 16:
        raw = image_to_rgb565(img)
    elif bpp == 32:
        raw = img.tobytes("raw", "BGRA")
    else:
        raw = image_to_rgb565(img)

    try:
        with open(device, "wb") as fb:
            fb.write(raw)
    except PermissionError:
        log.warning("フレームバッファ書き込み権限なし")


# ── タッチ入力 ────────────────────────────────────────


def adc_to_screen(adc_x: int, adc_y: int) -> tuple[int, int]:
    """ADC座標 → 画面座標。"""
    x, y = adc_x, adc_y
    if TOUCH_SWAP_XY:
        x, y = y, x
    if TOUCH_INVERT_X:
        x = 4095 - x
    if TOUCH_INVERT_Y:
        y = 4095 - y

    sx = int((x - TOUCH_X_MIN) / (TOUCH_X_MAX - TOUCH_X_MIN) * DISPLAY_WIDTH)
    sy = int((y - TOUCH_Y_MIN) / (TOUCH_Y_MAX - TOUCH_Y_MIN) * DISPLAY_HEIGHT)
    return max(0, min(DISPLAY_WIDTH - 1, sx)), max(0, min(DISPLAY_HEIGHT - 1, sy))


def button_rect(index: int) -> tuple[int, int, int, int]:
    """ボタンのインデックスから矩形を返す。"""
    return (BUTTON_X, index * BUTTON_HEIGHT,
            DISPLAY_WIDTH, (index + 1) * BUTTON_HEIGHT)


def handle_tap(state: AppState, x: int, y: int):
    """タップ処理。ボタン判定。"""
    for i, name in enumerate(BUTTON_DEFS):
        x1, y1, x2, y2 = button_rect(i)
        if x1 <= x <= x2 and y1 <= y <= y2:
            with state.lock:
                if name == "reload":
                    log.info("タップ: リロード")
                    state.needs_reload = True
                elif name == "zoom_in" and state.zoom < ZOOM_MAX:
                    log.info("タップ: ズームイン %d→%d", state.zoom, state.zoom + 1)
                    nx, ny = zoom_to(state.zoom + 1, state.zoom,
                                     state.tile_x_start, state.tile_y_start)
                    state.zoom += 1
                    state.tile_x_start = nx
                    state.tile_y_start = ny
                    state.needs_tile_refetch = True
                elif name == "zoom_out" and state.zoom > ZOOM_MIN:
                    log.info("タップ: ズームアウト %d→%d", state.zoom, state.zoom - 1)
                    nx, ny = zoom_to(state.zoom - 1, state.zoom,
                                     state.tile_x_start, state.tile_y_start)
                    state.zoom -= 1
                    state.tile_x_start = nx
                    state.tile_y_start = ny
                    state.needs_tile_refetch = True
                elif name == "play":
                    if state.mode == AppMode.IDLE:
                        log.info("タップ: アニメーション開始")
                        state.animation_requested = True
                    else:
                        log.info("タップ: アニメーション停止")
                        state.stop_requested = True
            return


def handle_swipe(state: AppState, dx: int, dy: int):
    """スワイプ処理。マップをパン。"""
    pixels_per_tile_x = DISPLAY_WIDTH / GRID_COLS
    pixels_per_tile_y = MAP_HEIGHT / GRID_ROWS

    tile_dx = round(-dx / pixels_per_tile_x)
    tile_dy = round(-dy / pixels_per_tile_y)

    if tile_dx == 0 and tile_dy == 0:
        return

    max_tile = 2 ** state.zoom
    with state.lock:
        new_x = max(0, min(state.tile_x_start + tile_dx, max_tile - GRID_COLS))
        new_y = max(0, min(state.tile_y_start + tile_dy, max_tile - GRID_ROWS))
        if new_x != state.tile_x_start or new_y != state.tile_y_start:
            log.info("スワイプ: タイル (%d,%d)→(%d,%d)",
                     state.tile_x_start, state.tile_y_start, new_x, new_y)
            state.swipe_offset_x = dx
            state.swipe_offset_y = dy
            state.tile_x_start = new_x
            state.tile_y_start = new_y
            state.needs_tile_refetch = True


def touch_thread(state: AppState):
    """タッチ入力スレッド (daemon)。"""
    try:
        import evdev
        from evdev import InputDevice, ecodes
    except ImportError:
        log.info("evdev未インストール、タッチ無効")
        return

    device = None
    for dev_path in evdev.list_devices():
        dev = InputDevice(dev_path)
        if "ads7846" in dev.name.lower() or "touch" in dev.name.lower():
            device = dev
            break

    if not device:
        log.info("タッチデバイス未検出")
        return

    log.info("タッチデバイス: %s (%s)", device.name, device.path)

    touch_down = False
    start_x = start_y = 0
    start_time = 0.0
    current_x = current_y = 0
    got_new_x = got_new_y = False

    for event in device.read_loop():
        if event.type == ecodes.EV_ABS:
            if event.code == ecodes.ABS_X:
                current_x = event.value
                if touch_down and not got_new_x:
                    got_new_x = True
            elif event.code == ecodes.ABS_Y:
                current_y = event.value
                if touch_down and not got_new_y:
                    got_new_y = True
            # X,Y 両方揃ったら start を確定
            if got_new_x and got_new_y and start_x == 0 and start_y == 0:
                start_x, start_y = current_x, current_y
            # ドラッグ中: リアルタイムオフセット更新
            if touch_down and start_x != 0 and start_y != 0:
                sx_s, sy_s = adc_to_screen(start_x, start_y)
                sx_c, sy_c = adc_to_screen(current_x, current_y)
                with state.lock:
                    state.drag_dx = sx_c - sx_s
                    state.drag_dy = sy_c - sy_s
                    state.is_dragging = True
                state.wake.set()
        elif event.type == ecodes.EV_KEY and event.code == ecodes.BTN_TOUCH:
            if event.value == 1:
                touch_down = True
                got_new_x = got_new_y = False
                start_x = start_y = 0
                start_time = time.time()
                with state.lock:
                    state.is_dragging = False
                    state.drag_dx = state.drag_dy = 0
            elif event.value == 0 and touch_down:
                touch_down = False
                with state.lock:
                    state.is_dragging = False
                    state.drag_dx = state.drag_dy = 0

                sx_s, sy_s = adc_to_screen(start_x, start_y)
                sx_e, sy_e = adc_to_screen(current_x, current_y)
                duration = time.time() - start_time
                dist = ((sx_e - sx_s) ** 2 + (sy_e - sy_s) ** 2) ** 0.5

                log.info("タッチ: ADC(%d,%d)→(%d,%d) 画面(%d,%d)→(%d,%d) d=%.0f t=%.2f",
                         start_x, start_y, current_x, current_y,
                         sx_s, sy_s, sx_e, sy_e, dist, duration)

                if duration < 0.3 and dist < 15:
                    handle_tap(state, sx_e, sy_e)
                elif dist >= 15:
                    handle_swipe(state, sx_e - sx_s, sy_e - sy_s)
                state.wake.set()


# ── メイン ────────────────────────────────────────────


def display_frame(image: Image.Image, fb_format: dict | None,
                  frame_name: str = "current"):
    """フレームを表示 (FB書き込み or PNG保存)。"""
    if fb_format:
        write_to_framebuffer(image, FRAMEBUFFER_DEVICE, fb_format)
    else:
        Path("frames").mkdir(exist_ok=True)
        image.save(f"frames/{frame_name}.png")


def build_full_frame(map_img: Image.Image, state: AppState,
                     forecast: list[dict]) -> Image.Image:
    """マップ + ボタン + 天気予報バーを合成して480x320画像を返す。"""
    map_with_buttons = draw_buttons(map_img, state)
    full = Image.new("RGB", (DISPLAY_WIDTH, DISPLAY_HEIGHT), (20, 20, 30))
    full.paste(map_with_buttons, (0, 0))
    draw_forecast_bar(full, forecast)
    return full


def main():
    state = AppState()

    fb_format = None
    if os.path.exists(FRAMEBUFFER_DEVICE):
        fb_format = detect_fb_format(FRAMEBUFFER_DEVICE)
    else:
        log.info("フレームバッファなし (PCモード)")

    # タッチスレッド起動
    t = threading.Thread(target=touch_thread, args=(state,), daemon=True)
    t.start()

    # 初期データ取得 (背景地図・レーダー・天気予報を並列)
    log.info("初期データ取得中...")
    snap_zoom = state.zoom
    snap_tx = state.tile_x_start
    snap_ty = state.tile_y_start
    current_meta = get_current_radar_time()
    current_radar = None
    rz = radar_zoom(snap_zoom)
    rtx, rty = zoom_to(rz, snap_zoom, snap_tx, snap_ty) if rz != snap_zoom else (snap_tx, snap_ty)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as pool:
        base_future = pool.submit(
            fetch_tile_grid, BASE_TILE_URL, snap_zoom, snap_tx, snap_ty,
            cache_subdir=f"base_dark_{snap_zoom}")
        radar_future = None
        if current_meta:
            radar_future = pool.submit(
                fetch_tile_grid, JMA_TILE_URL, rz, rtx, rty,
                basetime=current_meta["basetime"], validtime=current_meta["validtime"])
        forecast_future = pool.submit(fetch_weekly_forecast)
        base_map = base_future.result()
        if radar_future:
            current_radar = radar_future.result()
        forecast = forecast_future.result()
    log.info("初期データ取得完了 (radar zoom=%d)", rz)

    last_fetch_time = time.time()
    radar_frames: list[tuple[dict, Image.Image]] = []
    last_map_img: Image.Image | None = None
    cached_bottom: Image.Image | None = None  # 予報バー+ボタンキャッシュ

    while True:
        try:
            remaining = max(0, DATA_REFRESH_INTERVAL - (time.time() - last_fetch_time))
            state.wake.wait(timeout=remaining)
            state.wake.clear()
            now = time.time()

            # ── リロード ──
            with state.lock:
                if state.needs_reload:
                    log.info("リロード実行")
                    state.zoom = DEFAULT_ZOOM
                    state.tile_x_start = DEFAULT_TILE_X
                    state.tile_y_start = DEFAULT_TILE_Y
                    state.mode = AppMode.IDLE
                    state.needs_reload = False
                    state.needs_tile_refetch = True
                    state.stop_requested = False
                    state.animation_requested = False
                    radar_frames = []
                    last_fetch_time = 0

            # ── タイル再取得 (パン/ズーム後) ──
            with state.lock:
                needs_refetch = state.needs_tile_refetch
                state.needs_tile_refetch = False
                swipe_ox = state.swipe_offset_x
                swipe_oy = state.swipe_offset_y
                state.swipe_offset_x = 0
                state.swipe_offset_y = 0
                if needs_refetch:
                    snap_zoom = state.zoom
                    snap_tx = state.tile_x_start
                    snap_ty = state.tile_y_start

            if needs_refetch:
                cached_bottom = None  # キャッシュクリア
                # スワイプ: マップ部分だけずらして即表示 (予報バー・ボタンは固定)
                if last_map_img and (swipe_ox or swipe_oy):
                    shifted_map = Image.new("RGB", (DISPLAY_WIDTH, MAP_HEIGHT),
                                            (20, 20, 30))
                    shifted_map.paste(last_map_img, (swipe_ox, swipe_oy))
                    shifted_map = draw_buttons(shifted_map, state)
                    shifted = Image.new("RGB", (DISPLAY_WIDTH, DISPLAY_HEIGHT),
                                        (20, 20, 30))
                    shifted.paste(shifted_map, (0, 0))
                    draw_forecast_bar(shifted, forecast)
                    display_frame(shifted, fb_format, "shift")
                log.info("タイル再取得中 (z=%d, x=%d, y=%d)...",
                         snap_zoom, snap_tx, snap_ty)
                rz = radar_zoom(snap_zoom)
                rtx, rty = zoom_to(rz, snap_zoom, snap_tx, snap_ty) if rz != snap_zoom else (snap_tx, snap_ty)
                current_meta = get_current_radar_time()
                # 背景地図とレーダーを並列取得
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=2) as pool:
                    base_future = pool.submit(
                        fetch_tile_grid, BASE_TILE_URL, snap_zoom, snap_tx, snap_ty,
                        cache_subdir=f"base_dark_{snap_zoom}")
                    radar_future = None
                    if current_meta:
                        radar_future = pool.submit(
                            fetch_tile_grid, JMA_TILE_URL, rz, rtx, rty,
                            basetime=current_meta["basetime"],
                            validtime=current_meta["validtime"])
                    base_map = base_future.result()
                    if radar_future:
                        current_radar = radar_future.result()
                radar_frames = []
                last_fetch_time = time.time()
                log.info("タイル再取得完了")
                # 取得中に新たな操作があった場合、古いデータで描画せず次のループで再取得
                with state.lock:
                    if state.needs_tile_refetch:
                        log.info("取得中に新たな操作あり、再取得へ")
                        continue
                last_map_img = None  # 再描画トリガー

            # ── 定期データ更新 (5分ごと) ──
            if now - last_fetch_time >= DATA_REFRESH_INTERVAL:
                log.info("定期データ更新中...")
                current_meta = get_current_radar_time()
                rz = radar_zoom(snap_zoom)
                rtx, rty = zoom_to(rz, snap_zoom, snap_tx, snap_ty) if rz != snap_zoom else (snap_tx, snap_ty)
                if current_meta:
                    current_radar = fetch_tile_grid(
                        JMA_TILE_URL, rz, rtx, rty,
                        basetime=current_meta["basetime"],
                        validtime=current_meta["validtime"])
                forecast = fetch_weekly_forecast()
                cached_bottom = None
                radar_frames = []
                last_map_img = None  # 再描画トリガー
                last_fetch_time = time.time()
                log.info("定期データ更新完了")

            # ── アニメーション開始要求 ──
            with state.lock:
                if state.animation_requested:
                    state.animation_requested = False
                    state.mode = AppMode.PLAYING
                    state.stop_requested = False

            # ── ドラッグ中: 30fps でマップをシフト描画 ──
            with state.lock:
                dragging = state.is_dragging
                ddx = state.drag_dx
                ddy = state.drag_dy

            if dragging and last_map_img:
                # 予報バーをキャッシュ (ドラッグ中は変化しない)
                if cached_bottom is None:
                    cached_bottom = Image.new(
                        "RGB", (DISPLAY_WIDTH, DISPLAY_HEIGHT), (20, 20, 30))
                    draw_forecast_bar(cached_bottom, forecast)
                # 高速パス: マップシフト + キャッシュ済み下部
                shifted_map = Image.new("RGB", (DISPLAY_WIDTH, MAP_HEIGHT),
                                        (20, 20, 30))
                shifted_map.paste(last_map_img, (ddx, ddy))
                full = cached_bottom.copy()
                full.paste(shifted_map, (0, 0))
                display_frame(full, fb_format, "drag")
                state.wake.wait(timeout=1 / 30)
                state.wake.clear()
                continue

            # ── IDLE: 現在のレーダー1枚表示 ──
            if state.mode == AppMode.IDLE:
                if last_map_img is None:
                    log.info("IDLE描画: snap=(%d,%d,%d) rz=%d rtx=%d rty=%d state=(%d,%d,%d)",
                             snap_zoom, snap_tx, snap_ty, rz, rtx, rty,
                             state.zoom, state.tile_x_start, state.tile_y_start)
                    empty_radar = Image.new(
                        "RGBA", (GRID_COLS * TILE_SIZE, GRID_ROWS * TILE_SIZE))
                    radar = current_radar if current_radar else empty_radar
                    vt = current_meta["validtime"] if current_meta else ""
                    map_img = compose_map(base_map, radar,
                                          snap_zoom, snap_tx, snap_ty,
                                          rz, rtx, rty, vt, False)
                    last_map_img = map_img
                    full = build_full_frame(map_img, state, forecast)
                    display_frame(full, fb_format, "current")

            # ── PLAYING: アニメーション ──
            elif state.mode == AppMode.PLAYING:
                if not radar_frames:
                    from concurrent.futures import ThreadPoolExecutor, as_completed
                    log.info("アニメーションフレーム取得中...")
                    frames_meta = get_all_radar_times()
                    rz_anim = radar_zoom(state.zoom)
                    rtx_a, rty_a = zoom_to(rz_anim, state.zoom, state.tile_x_start, state.tile_y_start) if rz_anim != state.zoom else (state.tile_x_start, state.tile_y_start)

                    def _fetch_anim_frame(idx, meta):
                        try:
                            r = fetch_tile_grid(
                                JMA_TILE_URL, rz_anim, rtx_a, rty_a,
                                basetime=meta["basetime"],
                                validtime=meta["validtime"])
                            return idx, meta, r
                        except Exception:
                            return idx, meta, None

                    results = {}
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        futs = {pool.submit(_fetch_anim_frame, i, m): i
                                for i, m in enumerate(frames_meta)}
                        for fut in as_completed(futs):
                            if state.stop_requested:
                                break
                            idx, meta, img = fut.result()
                            if img:
                                results[idx] = (meta, img)
                    radar_frames = [results[i] for i in sorted(results)]
                    log.info("アニメーションフレーム取得完了 (%d)",
                             len(radar_frames))

                if not radar_frames or state.stop_requested:
                    with state.lock:
                        state.mode = AppMode.IDLE
                        state.stop_requested = False
                    continue

                total = len(radar_frames)
                ANIM_LOOPS = 2
                stopped = False
                for loop in range(ANIM_LOOPS):
                    if stopped:
                        break
                    for i, (meta, radar) in enumerate(radar_frames):
                        if state.stop_requested:
                            stopped = True
                            break

                        map_img = compose_map(
                            base_map, radar,
                            snap_zoom, snap_tx, snap_ty,
                            rz_anim, rtx_a, rty_a,
                            meta["validtime"], meta["is_forecast"],
                            i, total)
                        last_map_img = map_img
                        full = build_full_frame(map_img, state, forecast)
                        display_frame(full, fb_format, f"frame_{i:03d}")

                        if i == total - 1:
                            time.sleep(LAST_FRAME_PAUSE)
                        else:
                            time.sleep(FRAME_INTERVAL)

                # 2ループ完了 or 手動停止 → IDLE に戻る
                with state.lock:
                    state.mode = AppMode.IDLE
                    state.stop_requested = False
                log.info("アニメーション終了")

        except KeyboardInterrupt:
            log.info("終了")
            sys.exit(0)
        except Exception as e:
            log.error("エラー: %s", e, exc_info=True)
            time.sleep(30)


if __name__ == "__main__":
    main()
