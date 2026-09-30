#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行者 (XOSS / imxingzhe.com) 数据源客户端

背景
----
SyncOnelapToXoss 原项目以 OneLap(顽鹿) 为数据源、行者(XOSS) 为下游之一。
本模块把行者变成"数据源"，用于把行者上的骑行记录同步到 OneLap / iGPSport 等平台。

行者网页版内部 API（已通过 www.imxingzhe.com/workouts/{id} 页面前端 bundle 确认）
------------------------------------------------------------------------------
* 活动详情 : GET /api/v1/pgworkout/{workout_id}/          （公开记录免登录可读）
* 导出 Fit : GET /api/v1/workout/{workout_id}/fit/        （前端“导出Fit”按钮 href，需登录）
* 导出 GPX : GET /api/v1/pgworkout/{workout_id}/gpx/      （前端“导出GPX”按钮 href，需登录）
* 活动列表 : GET /api/v1/pgworkout/?offset=&limit=        （新版列表接口）
* 月度列表 : GET /api/v4/user_month_info/?user_id=&year=&month=  （旧版稳定接口，作为回退）
* 用户信息 : GET /api/v1/user/information/ 或 /api/v4/account/get_user_info/

鉴权方式：浏览器登录后的 Cookie（sessionid 等），与官方 OAuth 无关。
"""

import base64
import json
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 常量
XOSS_BASE = 'https://www.imxingzhe.com'
XOSS_LOGIN_URL = f'{XOSS_BASE}/login'
XOSS_WORKOUT_PAGE = f'{XOSS_BASE}/workouts/{{workout_id}}'

XOSS_USER_INFO_APIS = (
    '/api/v1/user/information/',
    '/api/v4/account/get_user_info/',
    '/api/v1/user/user_info/',
)
XOSS_LOGIN_API = '/api/v1/user/login/'
XOSS_USER_INFO_API = '/api/v1/user/user_info/'
XOSS_LIST_API = '/api/v1/pgworkout/'
XOSS_MONTH_API = '/api/v4/user_month_info/'
XOSS_DETAIL_API = '/api/v1/pgworkout/{workout_id}/'
XOSS_FIT_API = '/api/v1/workout/{workout_id}/fit/'
XOSS_GPX_API = '/api/v1/pgworkout/{workout_id}/gpx/'
XOSS_POINTS_API = '/api/v1/pgworkout/{workout_id}/points/'

# 行者 start_time 语义：毫秒级 UTC 时间戳，需 +8 小时才是行者页面展示的北京时间。
# 实测校验（两条真实记录）：
#   36838086  -> 1511574741000 -> UTC 01:52 -> +8 = 2017-11-25 09:52，页面标题“上午 骑行” ✓
#   224048647 -> 1790771700000 -> UTC 12:35 -> +8 = 2026-09-30 20:35，页面标题“晚上 骑行”，
#               且上传时间戳 21:45:57、运动时长 4124s 完全吻合 ✓
# 若你的时区不同或发现偏差，可通过 [xoss] time_offset_hours 调整。
DEFAULT_TIME_OFFSET_HOURS = 8

DEFAULT_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Referer': XOSS_BASE + '/',
}


# ---------------------------------------------------------------- 工具函数
# ---------------------------------------------------------------- 标准库 HTTP 回退
# 未安装 requests 时，用标准库拼一个最小兼容层，使行者数据源可以零第三方依赖运行。
class _UrllibCookie:
    def __init__(self, name, value):
        self.name = name
        self.value = value


class _UrllibCookies:
    def __init__(self):
        self._items = {}

    def set(self, name, value, domain=None, path=None):
        self._items[str(name)] = str(value)

    def get(self, name, default=None):
        return self._items.get(name, default)

    def __iter__(self):
        return iter([_UrllibCookie(k, v) for k, v in self._items.items()])

    def __len__(self):
        return len(self._items)

    def __bool__(self):
        return bool(self._items)

    def update_from_headers(self, headers):
        try:
            raw_list = headers.get_all('Set-Cookie') or []
        except AttributeError:
            raw_list = []
        for raw in raw_list:
            first = raw.split(';')[0].strip()
            if '=' in first:
                name, value = first.split('=', 1)
                self._items[name.strip()] = value.strip()


class _UrllibResponse:
    def __init__(self, status_code, headers, content):
        self.status_code = status_code
        self.headers = headers
        self.content = content or b''

    @property
    def text(self):
        return self.content.decode('utf-8', 'replace')

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')

    def iter_content(self, chunk_size=65536):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start:start + chunk_size]


class _UrllibSession:
    """requests.Session 的最小兼容实现（只覆盖本模块用到的能力）。"""

    def __init__(self):
        self.headers = {}
        self.cookies = _UrllibCookies()
        self._context = ssl.create_default_context()

    def _prepare(self, url, params, headers):
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            if query:
                url = f"{url}{'&' if '?' in url else '?'}{query}"
        merged = dict(self.headers)
        merged.update(headers or {})
        if len(self.cookies):
            merged['Cookie'] = '; '.join(f'{c.name}={c.value}' for c in self.cookies)
        return url, merged

    def _send(self, request, timeout):
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=self._context) as resp:
                content = resp.read()
                self.cookies.update_from_headers(resp.headers)
                return _UrllibResponse(resp.status, dict(resp.headers), content)
        except urllib.error.HTTPError as exc:
            content = exc.read()
            self.cookies.update_from_headers(exc.headers)
            return _UrllibResponse(exc.code, dict(exc.headers), content)

    def get(self, url, params=None, headers=None, timeout=40, stream=False):
        url, merged = self._prepare(url, params, headers)
        return self._send(urllib.request.Request(url, headers=merged), timeout)

    def post(self, url, json=None, data=None, headers=None, timeout=40):  # noqa: A002
        import json as _json  # 参数名 json 会遮蔽模块，这里用别名

        body = None
        extra = dict(headers or {})
        if json is not None:
            body = _json.dumps(json).encode('utf-8')
            extra.setdefault('Content-Type', 'application/json')
        elif data is not None:
            body = data if isinstance(data, bytes) else urllib.parse.urlencode(data).encode('utf-8')
            extra.setdefault('Content-Type', 'application/x-www-form-urlencoded')
        url, merged = self._prepare(url, None, extra)
        request = urllib.request.Request(url, data=body, method='POST')
        for key, value in merged.items():
            request.add_header(key, value)
        return self._send(request, timeout)


def build_http_session():
    """优先用 requests（与其它平台一致），不可用时退回标准库实现。"""
    try:
        import requests
        return requests.Session(), 'requests'
    except ImportError:
        return _UrllibSession(), 'urllib'


def normalize_cookies(cookies):
    """把 DrissionPage / requests 各种 cookie 结构统一成 {name: value}。"""
    result = {}
    if not cookies:
        return result
    if isinstance(cookies, dict):
        for key, value in cookies.items():
            if key and value is not None:
                result[str(key)] = str(value)
        return result
    if isinstance(cookies, (list, tuple)):
        for item in cookies:
            if isinstance(item, dict):
                name = item.get('name')
                value = item.get('value')
                if name:
                    result[str(name)] = '' if value is None else str(value)
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                result[str(item[0])] = str(item[1])
    return result


def parse_xoss_time(value, offset_hours=DEFAULT_TIME_OFFSET_HOURS):
    """解析行者时间字段为 naive datetime。

    * 数值（毫秒或秒级 epoch）按 UTC 解释后叠加 offset_hours（默认 +8）得到北京时间。
      已在真实数据上校验：224048647 -> 1790771700000 -> 20:35（页面显示“晚上 骑行”）。
    * 字符串（如 upload_time="2026-09-30 21:45:57"）本身就是站点本地时间，**不再**叠加偏移。
    """
    if value is None or value == '':
        return None

    parsed = None
    is_epoch = False

    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        number = float(value)
        # 毫秒 -> 秒
        if number > 1e11:
            number = number / 1000.0
        try:
            parsed = datetime.fromtimestamp(number, tz=timezone.utc).replace(tzinfo=None)
            is_epoch = True
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        text = value.strip().replace('/', '-').replace('T', ' ')
        text = re.sub(r'(\.\d+)?(Z|[+-]\d{2}:?\d{2})$', '', text).strip()
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None

    if parsed and is_epoch and offset_hours:
        parsed = parsed + timedelta(hours=offset_hours)
    return parsed


def sanitize_filename(name, fallback='xoss_activity'):
    """生成安全的文件名片段。"""
    text = re.sub(r'[\\/:*?"<>|\r\n\t]+', '_', str(name or '')).strip(' .')
    text = re.sub(r'\s+', '_', text)
    return text[:80] or fallback


# ---------------------------------------------------------------- 登录密码加密
# 行者前端登录时用 JSEncrypt 对密码做 RSAES-PKCS1-v1_5 加密，公钥硬编码在
# https://www.imxingzhe.com/home/static/js/app.*.js 的 window.Vue.prototype.$getRsaCode 里。
# 这里用纯 Python 复现（无需额外依赖），从而支持“免浏览器”的账号密码登录。
XOSS_RSA_MODULUS = 0xe6b909016e28ee74300981f7df0de78806eb6a58766f530711baf59d83cf77f77152773dc82541dcd2b808fd2dd6b94f0dd1578f24b995019b4238ae2a7b8158fde2a65a92669fb25f5c1e074cc5a33d5a324599926235b82fc1ab5a6879d508fb0f0a5300369d1776aad38a3eb356fc2d725af2bebe4c8aaa2f95941ed46a99
XOSS_RSA_EXPONENT = 65537


def xoss_rsa_encrypt(password):
    """按 JSEncrypt 的方式加密登录密码，返回 base64 字符串。"""
    message = str(password).encode('utf-8')
    key_size = (XOSS_RSA_MODULUS.bit_length() + 7) // 8
    if len(message) > key_size - 11:
        raise ValueError('密码过长，无法进行 RSA 加密')

    padding_length = key_size - len(message) - 3
    padding = bytearray()
    while len(padding) < padding_length:
        for byte in os.urandom(padding_length - len(padding)):
            if byte != 0:
                padding.append(byte)
                if len(padding) >= padding_length:
                    break

    block = b'\x00\x02' + bytes(padding) + b'\x00' + message
    encrypted = pow(int.from_bytes(block, 'big'), XOSS_RSA_EXPONENT, XOSS_RSA_MODULUS)
    return base64.b64encode(encrypted.to_bytes(key_size, 'big')).decode('ascii')


def looks_like_fit(raw_bytes):
    """按 FIT 规范校验文件头：第 8-11 字节应为 b'.FIT'，且长度不小于 14 字节。"""
    if not raw_bytes or len(raw_bytes) < 14:
        return False
    return raw_bytes[8:12] == b'.FIT'


def looks_like_gpx(raw_bytes):
    if not raw_bytes:
        return False
    head = raw_bytes[:400].lstrip()
    return head.startswith(b'<?xml') and b'<gpx' in raw_bytes[:2000]


# ---------------------------------------------------------------- 轨迹点重建
# /api/v1/pgworkout/{id}/points/ 返回 {"points": [...], "encoding_points": "..."}：
#   * points[i] = {heartrate, power, time(UTC 毫秒), altitude, speed(m/s), cadence}
#   * encoding_points 是标准 Google Encoded Polyline（precision=5），经纬度在此
# 二者按索引一一对应（已在真实数据上验证：4127 点 <-> 4127 坐标，坐标落在柳州市）。

def decode_polyline(encoded_str, precision=5):
    """解码 Google Encoded Polyline，返回 [(lat, lng), ...]。"""
    if not encoded_str:
        return []
    coords = []
    index = 0
    lat = 0
    lng = 0
    factor = float(10 ** precision)
    length = len(encoded_str)

    while index < length:
        for is_lng in (False, True):
            shift = 0
            result = 0
            while True:
                if index >= length:
                    return coords
                byte = ord(encoded_str[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if is_lng:
                lng += delta
            else:
                lat += delta
        coords.append((lat / factor, lng / factor))
    return coords


def _gpx_time_from_epoch_ms(value):
    """UTC 毫秒 -> GPX 所需 ISO8601 UTC 字符串（注意：写真实 UTC，不叠加时区偏移）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1e11:
        number = number / 1000.0
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    except (OverflowError, OSError, ValueError):
        return None


def build_gpx_xml(points, coords, title='', sport=3, creator='SyncOnelapToXoss'):
    """由行者 points + polyline 坐标合成 GPX（含心率/踏频/功率扩展）。

    points/coords 按索引对应；数量不一致时按较短的截断。
    """
    count = min(len(points or []), len(coords or []))
    if count <= 0:
        return b''

    name = (title or 'Activity').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    sport_type = {1: 'hiking', 2: 'running', 3: 'cycling'}.get(sport, 'cycling')

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="%s"' % creator,
        '     xmlns="http://www.topografix.com/GPX/1/1"',
        '     xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">',
        '  <metadata>',
        '    <link href="https://www.imxingzhe.com/"><text>XOSS</text></link>',
        '  </metadata>',
        '  <trk>',
        '    <name>%s</name>' % name,
        '    <type>%s</type>' % sport_type,
        '    <trkseg>',
    ]

    for i in range(count):
        point = points[i] if isinstance(points[i], dict) else {}
        try:
            lat, lng = coords[i]
            lat = float(lat)
            lng = float(lng)
        except (TypeError, ValueError, IndexError):
            # 单个点坐标异常不应中断整条轨迹的生成
            continue

        lines.append('      <trkpt lat="%.6f" lon="%.6f">' % (lat, lng))

        altitude = point.get('altitude')
        if isinstance(altitude, (int, float)):
            lines.append('        <ele>%.1f</ele>' % altitude)

        time_text = _gpx_time_from_epoch_ms(point.get('time'))
        if time_text:
            lines.append('        <time>%s</time>' % time_text)

        heartrate = point.get('heartrate')
        cadence = point.get('cadence')
        power = point.get('power')
        has_hr = isinstance(heartrate, (int, float)) and heartrate > 0
        has_cad = isinstance(cadence, (int, float)) and cadence > 0
        has_power = isinstance(power, (int, float)) and power > 0

        if has_hr or has_cad or has_power:
            lines.append('        <extensions>')
            if has_hr or has_cad:
                lines.append('          <gpxtpx:TrackPointExtension>')
                if has_hr:
                    lines.append('            <gpxtpx:hr>%d</gpxtpx:hr>' % int(round(heartrate)))
                if has_cad:
                    lines.append('            <gpxtpx:cad>%d</gpxtpx:cad>' % int(round(cadence)))
                lines.append('          </gpxtpx:TrackPointExtension>')
            if has_power:
                lines.append('          <power>%d</power>' % int(round(power)))
            lines.append('        </extensions>')

        lines.append('      </trkpt>')

    lines.extend(['    </trkseg>', '  </trk>', '</gpx>', ''])
    return '\n'.join(lines).encode('utf-8')


# ---------------------------------------------------------------- 客户端
class XossClient:
    """行者网页版内部 API 客户端（Cookie 鉴权）。"""

    def __init__(self, cookies=None, session=None, time_offset_hours=DEFAULT_TIME_OFFSET_HOURS,
                 request_interval=0.4):
        self.time_offset_hours = time_offset_hours
        self.request_interval = request_interval
        self._user_id = None
        self._last_request_at = 0.0
        self.cookies = normalize_cookies(cookies)

        if session is not None:
            self.session = session
            self.http_backend = 'external'
        else:
            self.session, self.http_backend = build_http_session()
            if self.http_backend == 'urllib':
                logger.info('[行者] 未安装 requests，已使用标准库 HTTP 后端')
        self.session.headers.update(DEFAULT_HEADERS)
        for name, value in self.cookies.items():
            try:
                self.session.cookies.set(name, value, domain='.imxingzhe.com')
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ 构造
    @classmethod
    def from_tab(cls, tab, **kwargs):
        """从已登录的 DrissionPage 标签页提取 Cookie 构造客户端。"""
        cookies = None
        try:
            cookies = tab.cookies()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f'读取行者 Cookie 失败: {exc}')
        # DrissionPage 的 CookiesList 提供 as_dict()，优先使用
        if cookies is not None and hasattr(cookies, 'as_dict'):
            try:
                cookies = cookies.as_dict()
            except Exception:  # noqa: BLE001
                pass
        client = cls(cookies=cookies, **kwargs)
        logger.info(f'[行者] 已从浏览器提取 {len(client.cookies)} 个 Cookie')
        return client

    # ------------------------------------------------------------ 请求
    def _throttle(self):
        if self.request_interval <= 0:
            return
        elapsed = time.time() - self._last_request_at
        if elapsed < self.request_interval:
            time.sleep(self.request_interval - elapsed)
        self._last_request_at = time.time()

    def _request(self, path, params=None, accept='application/json', timeout=40, stream=False,
                 referer=None):
        url = path if path.startswith('http') else XOSS_BASE + path
        self._throttle()
        headers = {'Accept': accept}
        if referer:
            headers['Referer'] = referer
        response = self.session.get(
            url, params=params, headers=headers, timeout=timeout, stream=stream,
        )
        return response

    def _get_json(self, path, params=None, timeout=40, referer=None):
        response = self._request(path, params=params, timeout=timeout, referer=referer)
        if response.status_code in (401, 403):
            # 行者对未登录请求：v1 部分接口返回 401，v4 接口返回 403
            raise PermissionError(f'行者接口需要登录（{response.status_code}）：{path}')
        if response.status_code == 404:
            return None
        if response.status_code == 400:
            # /api/v1/pgworkout/ 在未登录/参数不被接受时会返回 400 {"msg": "not allowed"}
            logger.warning(f'[行者] 接口返回 400 not allowed：{path}（通常表示未登录或参数不被接受）')
            return None
        response.raise_for_status()
        try:
            return response.json()
        except ValueError:
            logger.debug(f'[行者] 非 JSON 响应: {path} -> {response.text[:200]}')
            return None

    # ------------------------------------------------------------ 登录
    def login_with_password(self, account, password):
        """用账号密码直接登录（免浏览器）。

        复现行者前端登录流程：RSA 加密密码 -> POST /api/v1/user/login/，
        响应 code == 0 表示成功；会话 Cookie（sessionid）由 requests 自动保存，
        之后所有接口即可直接访问。
        """
        if not account or not password:
            logger.error('[行者] 账号或密码为空，无法登录')
            return False

        try:
            encrypted_password = xoss_rsa_encrypt(password)
        except Exception as exc:  # noqa: BLE001
            logger.error(f'[行者] 密码 RSA 加密失败: {exc}')
            return False

        try:
            response = self.session.post(
                XOSS_BASE + XOSS_LOGIN_API,
                json={'account': account, 'password': encrypted_password},
                headers={'Accept': 'application/json', 'Referer': f'{XOSS_BASE}/login'},
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f'[行者] 登录请求异常: {exc}')
            return False

        if response.status_code != 200:
            logger.warning(f'[行者] 登录接口返回 HTTP {response.status_code}: {response.text[:200]}')
            return False

        try:
            data = response.json()
        except ValueError:
            logger.warning(f'[行者] 登录响应不是 JSON: {response.text[:200]}')
            return False

        code = data.get('code') if isinstance(data, dict) else None
        if code != 0:
            message = data.get('msg') if isinstance(data, dict) else ''
            logger.error(f'[行者] 账号密码登录失败: code={code} msg={message}')
            return False

        # 只有真正拿到会话 Cookie 才算登录成功，否则让调用方回退浏览器登录
        cookie_names = sorted({cookie.name for cookie in self.session.cookies})
        if not cookie_names:
            logger.warning('[行者] 登录接口返回成功但未下发任何 Cookie，视为登录失败')
            return False

        self._user_id = None
        logger.info(f"[行者] 账号密码登录成功（Cookie: {', '.join(cookie_names)}）")
        return True

    # ------------------------------------------------------------ 用户
    def get_user_id(self):
        """获取当前登录用户 id（用于列表/月度接口）。"""
        if self._user_id:
            return self._user_id
        for path in XOSS_USER_INFO_APIS:
            try:
                data = self._get_json(path)
            except PermissionError:
                logger.debug(f'[行者] {path} 需要登录')
                continue
            except Exception as exc:  # noqa: BLE001
                logger.debug(f'[行者] {path} 请求失败: {exc}')
                continue
            user_id = self._extract_user_id(data)
            if user_id:
                self._user_id = user_id
                logger.info(f'[行者] 当前用户 id = {user_id}（来源 {path}）')
                return user_id
        logger.warning('[行者] 未能获取用户 id，活动列表接口可能不可用')
        return None

    @staticmethod
    def _extract_user_id(data):
        if not isinstance(data, dict):
            return None
        candidates = [data]
        for key in ('data', 'user', 'result'):
            inner = data.get(key)
            if isinstance(inner, dict):
                candidates.append(inner)
                if isinstance(inner.get('user'), dict):
                    candidates.append(inner['user'])
        for holder in candidates:
            for key in ('id', 'user_id', 'userid', 'userID'):
                value = holder.get(key)
                if value:
                    return str(value)
        return None

    # ------------------------------------------------------------ 列表
    @staticmethod
    def _extract_activity_list(data):
        """从多种可能结构里抽出活动数组。

        已知结构：
          * /api/v4/user_month_info/ -> {"data": {"st_info": {...}, "wo_info": [ {...}, ... ]}}
          * /api/v1/pgworkout/       -> {"results"|"data": [...]}
        """
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return []
        list_keys = ('results', 'workouts', 'list', 'items', 'records', 'wo_info', 'data')
        for key in list_keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                for inner_key in list_keys:
                    inner = value.get(inner_key)
                    if isinstance(inner, list):
                        return inner
        return []

    @staticmethod
    def _activity_id(item):
        if not isinstance(item, dict):
            return None
        for key in ('workout_id', 'id', 'workouts_id'):
            value = item.get(key)
            if value:
                return str(value)
        return None

    @staticmethod
    def _activity_start_time(item, offset_hours):
        if not isinstance(item, dict):
            return None
        for key in ('start_time', 'startTime', 'start_timestamp', 'time', 'timestamp', 'ctime'):
            if key in item:
                parsed = parse_xoss_time(item.get(key), offset_hours)
                if parsed:
                    return parsed
        return None

    def _normalize_activity(self, item):
        activity_id = self._activity_id(item)
        if not activity_id:
            return None
        start_time = self._activity_start_time(item, self.time_offset_hours)
        distance = item.get('distance') or item.get('totalDistance') or item.get('total_distance') or 0
        try:
            distance = float(distance)
        except (TypeError, ValueError):
            distance = 0.0
        return {
            'workout_id': activity_id,
            'start_time': start_time,
            'distance_m': distance,
            'title': item.get('title') or item.get('name') or '',
            'is_fit': item.get('is_fit'),
            'equipment_info': item.get('equipment_info'),
            'raw': item,
        }

    def list_activities(self, stop_before=None, page_size=20, max_pages=100, page_delay=0.3):
        """
        拉取活动列表（按时间倒序）。

        stop_before: datetime；列表按时间倒序，遇到早于该时间的记录即停止翻页（增量）。
        返回 (activities, reach_limit) —— reach_limit 表示因触达 stop_before 而提前停止。
        """
        activities, reach_limit = self._list_activities_paged(
            stop_before=stop_before, page_size=page_size, max_pages=max_pages, page_delay=page_delay,
        )
        if activities:
            return activities, reach_limit

        logger.warning('[行者] /api/v1/pgworkout/ 列表接口无结果，回退到月度接口')
        return self._list_activities_by_month(stop_before=stop_before, max_months=36), False

    def _list_activities_paged(self, stop_before=None, page_size=20, max_pages=100, page_delay=0.3):
        activities = []
        seen = set()
        reach_limit = False

        for page in range(max_pages):
            params = {'offset': page * page_size, 'limit': page_size}
            user_id = self.get_user_id()
            if user_id:
                params['user_id'] = user_id
            try:
                data = self._get_json(XOSS_LIST_API, params=params)
            except PermissionError as exc:
                logger.warning(f'[行者] {exc}')
                return [], False
            except Exception as exc:  # noqa: BLE001
                logger.warning(f'[行者] 列表接口请求失败（第 {page + 1} 页）: {exc}')
                break

            items = self._extract_activity_list(data)
            if not items:
                break

            page_newest = None
            added = 0
            for item in items:
                activity = self._normalize_activity(item)
                if not activity or activity['workout_id'] in seen:
                    continue
                seen.add(activity['workout_id'])
                activities.append(activity)
                added += 1
                if activity['start_time'] and (page_newest is None or activity['start_time'] > page_newest):
                    page_newest = activity['start_time']

            if stop_before and page_newest and page_newest < stop_before:
                reach_limit = True
                break
            if added == 0:
                # 本页没有任何新记录：接口可能忽略 offset 而整页返回同一批数据，继续翻页没有意义
                logger.debug(f'[行者] 第 {page + 1} 页无新记录，停止翻页')
                break
            if len(items) < page_size:
                break
            time.sleep(page_delay)

        logger.info(f'[行者] 列表接口取到 {len(activities)} 条活动')
        return activities, reach_limit

    def _list_activities_by_month(self, stop_before=None, max_months=36):
        """回退方案：按月拉取（/api/v4/user_month_info/）。"""
        user_id = self.get_user_id()
        if not user_id:
            logger.error('[行者] 缺少 user_id，无法使用月度接口')
            return []

        activities = []
        seen = set()
        cursor = datetime.now().replace(day=1)
        for _ in range(max_months):
            params = {'user_id': user_id, 'year': cursor.year, 'month': cursor.month}
            try:
                data = self._get_json(XOSS_MONTH_API, params=params)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f'[行者] 月度接口 {cursor.year}-{cursor.month:02d} 失败: {exc}')
                data = None

            items = self._extract_activity_list(data)
            month_newest = None
            for item in items:
                activity = self._normalize_activity(item)
                if not activity or activity['workout_id'] in seen:
                    continue
                seen.add(activity['workout_id'])
                activities.append(activity)
                if activity['start_time'] and (month_newest is None or activity['start_time'] > month_newest):
                    month_newest = activity['start_time']

            if stop_before and month_newest and month_newest < stop_before:
                break

            cursor = (cursor - timedelta(days=1)).replace(day=1)
            time.sleep(0.3)

        activities.sort(key=lambda a: a['start_time'] or datetime.min, reverse=True)
        logger.info(f'[行者] 月度接口取到 {len(activities)} 条活动')
        return activities

    # ------------------------------------------------------------ 详情
    def get_activity_detail(self, workout_id):
        """获取活动详情（公开记录免登录可读）。"""
        try:
            data = self._get_json(XOSS_DETAIL_API.format(workout_id=workout_id))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f'[行者] 详情接口失败 {workout_id}: {exc}')
            return None
        if not isinstance(data, dict):
            return None
        inner = data.get('data') if isinstance(data.get('data'), dict) else data
        workout = inner.get('workout') if isinstance(inner, dict) else None
        return workout if isinstance(workout, dict) else None

    def describe_activity(self, workout_id):
        """返回用于日志展示的活动摘要（时间/距离/是否可导出 Fit）。"""
        workout = self.get_activity_detail(workout_id)
        if not workout:
            return None
        start_time = parse_xoss_time(workout.get('start_time'), self.time_offset_hours)
        equipment = workout.get('equipment_info')
        return {
            'workout_id': str(workout_id),
            'title': workout.get('title') or '',
            'start_time': start_time,
            'distance_m': workout.get('distance') or 0,
            'is_fit': workout.get('is_fit'),
            'has_equipment': bool(isinstance(equipment, list) and equipment),
            'product_name': workout.get('product_name') or '',
        }

    # ------------------------------------------------------------ 下载
    def download_fit(self, workout_id, out_dir, filename=None):
        """下载行者原始 Fit 文件；成功返回文件路径，失败返回 None。"""
        return self._download_file(
            XOSS_FIT_API.format(workout_id=workout_id),
            out_dir,
            filename or f'xoss_{workout_id}.fit',
            validator=looks_like_fit,
            kind='Fit',
            workout_id=workout_id,
        )

    def download_gpx(self, workout_id, out_dir, filename=None):
        """下载行者 GPX 文件（Fit 不可用时的回退）。"""
        return self._download_file(
            XOSS_GPX_API.format(workout_id=workout_id),
            out_dir,
            filename or f'xoss_{workout_id}.gpx',
            validator=looks_like_gpx,
            kind='GPX',
            workout_id=workout_id,
        )

    def get_activity_points(self, workout_id):
        """获取轨迹点数据（points 接口；公开记录免登录可读）。

        返回 (points, coords)；points 为逐点传感器数据，coords 为解码后的经纬度。
        """
        referer = f'{XOSS_BASE}/workouts/{workout_id}/'
        try:
            data = self._get_json(
                XOSS_POINTS_API.format(workout_id=workout_id), timeout=60, referer=referer,
            )
        except PermissionError as exc:
            logger.warning(f'[行者] 轨迹点接口需要登录：{exc}')
            return None, None
        except Exception as exc:  # noqa: BLE001
            logger.warning(f'[行者] 轨迹点接口请求失败 {workout_id}: {exc}')
            return None, None

        if not isinstance(data, dict):
            return None, None

        points = data.get('points')
        encoded = data.get('encoding_points')
        if not isinstance(points, list) or not points:
            return None, None

        coords = decode_polyline(encoded) if isinstance(encoded, str) else []
        if not coords:
            logger.warning(f'[行者] 轨迹点缺少可解码的坐标 {workout_id}')
            return None, None

        logger.info(f'[行者] 轨迹点 {len(points)} 个，解码坐标 {len(coords)} 个')
        return points, coords

    def download_points_gpx(self, workout_id, out_dir, filename=None, title=None, sport=3):
        """由 points + encoding_points 合成 GPX（Fit 与 GPX 接口都不可用时的兜底）。

        这条通路对公开记录免登录，是把行者数据带出来的最后一道保险。
        """
        points, coords = self.get_activity_points(workout_id)
        if not points or not coords:
            return None

        raw = build_gpx_xml(points, coords, title=title or f'XOSS {workout_id}', sport=sport)
        if not raw:
            return None

        os.makedirs(out_dir, exist_ok=True)
        target = os.path.join(
            out_dir, sanitize_filename(filename or f'xoss_{workout_id}.gpx', fallback=f'xoss_{workout_id}.gpx'),
        )
        part = target + '.part'
        try:
            with open(part, 'wb') as fh:
                fh.write(raw)
            os.replace(part, target)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f'[行者] 合成 GPX 失败 {workout_id}: {exc}')
            if os.path.exists(part):
                try:
                    os.remove(part)
                except OSError:
                    pass
            return None

        logger.info(
            f'[行者] 已由轨迹点合成 GPX: {os.path.basename(target)} '
            f'({min(len(points), len(coords))} 点, {len(raw) // 1024} KB)'
        )
        return target

    def _download_file(self, path, out_dir, filename, validator, kind, workout_id):
        os.makedirs(out_dir, exist_ok=True)
        target = os.path.join(out_dir, sanitize_filename(filename, fallback=f'xoss_{workout_id}'))
        part = target + '.part'
        try:
            # 带上真实来源页 Referer，与浏览器点击“导出Fit/导出GPX”按钮的行为一致
            response = self._request(
                path, accept='*/*', timeout=120, stream=True,
                referer=f'{XOSS_BASE}/workouts/{workout_id}/',
            )
            if response.status_code == 401:
                logger.warning(f'[行者] 下载{kind} 需要登录（401）：{path}')
                return None
            if response.status_code == 404:
                logger.info(f'[行者] 记录 {workout_id} 没有可导出的{kind}（404）')
                return None
            response.raise_for_status()

            with open(part, 'wb') as fh:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        fh.write(chunk)

            with open(part, 'rb') as fh:
                raw = fh.read()

            if not validator(raw):
                head = raw[:120].decode('utf-8', 'replace') if raw else ''
                logger.warning(f'[行者] {workout_id} 的{kind}响应格式异常（长度 {len(raw)}）：{head[:100]}')
                os.remove(part)
                return None

            os.replace(part, target)
            logger.info(f'[行者] {kind} 下载完成: {os.path.basename(target)} ({len(raw) // 1024} KB)')
            return target
        except Exception as exc:  # noqa: BLE001
            logger.warning(f'[行者] 下载{kind} 失败 {workout_id}: {exc}')
            for candidate in (part,):
                if os.path.exists(candidate):
                    try:
                        os.remove(candidate)
                    except OSError:
                        pass
            return None

    def download_activity(self, workout_id, out_dir, prefer_fit=True, filename=None,
                          title=None, sport=3):
        """
        下载单条活动的运动文件。

        取数优先级：
          1. Fit 接口 /api/v1/workout/{id}/fit/ —— 含完整传感器数据的原始文件
          2. points 接口自行合成 GPX —— 免登录，且带心率/踏频扩展
          3. GPX 接口 /api/v1/pgworkout/{id}/gpx/ —— 最后兜底（实测该文件不含传感器扩展）

        filename 可传“文件名主干”（如 20260930_203500_224048647）或完整文件名，
        本方法会按实际格式统一补上 .fit / .gpx 扩展名——缺少扩展名会导致下游平台拒收。
        返回 (文件路径, 格式) 或 (None, None)。
        """
        stem = sanitize_filename(
            os.path.splitext(os.path.basename(filename))[0] if filename else f'xoss_{workout_id}',
            fallback=f'xoss_{workout_id}',
        )

        if prefer_fit:
            path = self.download_fit(workout_id, out_dir, filename=f'{stem}.fit')
            if path:
                return path, 'fit'

        # Fit 不可用时优先自行合成：points 接口免登录，而行者 GPX 接口返回的文件不含心率/踏频
        path = self.download_points_gpx(
            workout_id, out_dir, filename=f'{stem}.gpx', title=title, sport=sport,
        )
        if path:
            return path, 'gpx'

        logger.info(f'[行者] 轨迹点合成失败，改用 GPX 接口（{workout_id}）')
        path = self.download_gpx(workout_id, out_dir, filename=f'{stem}.gpx')
        if path:
            return path, 'gpx'
        return None, None


# ---------------------------------------------------------------- 增量工具
def filter_incremental(activities, latest_time):
    """筛出 start_time 晚于 latest_time 的活动（latest_time 为 None 时返回全部）。"""
    if latest_time is None:
        return list(activities)
    return [a for a in activities if a.get('start_time') and a['start_time'] > latest_time]


def pick_download_floor(latest_times):
    """给定各目标平台最新活动时间，取最早的一个作为下载下限，确保不漏记录。"""
    known = [t for t in latest_times if t is not None]
    if not known:
        return None
    return min(known)
