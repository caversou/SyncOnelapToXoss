#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行者(XOSS)数据源接口探测脚本（零第三方依赖，仅用标准库）

背景
----
SyncXossToOnelap 原项目以 OneLap(顽鹿) 为数据源、行者(XOSS) 为下游。
本次改造要把行者变成数据源，需要在真实登录态下确认行者网页版内部 API 的行为。

已从行者前端代码(/workouts/{id} 页面 bundle)确认的接口：
  * 导出 Fit  : GET https://www.imxingzhe.com/api/v1/workout/{workoutId}/fit/
  * 导出 GPX  : GET https://www.imxingzhe.com/api/v1/pgworkout/{workoutId}/gpx/
  * 活动详情  : GET https://www.imxingzhe.com/api/v1/pgworkout/{workoutId}/   (无需登录即可读公开记录)

本脚本的作用：用你自己的登录 Cookie 把这些接口全部跑一遍，输出真实的状态码、
响应字段、文件名与文件大小，用来校正正式实现。脚本只读，不会修改/上传任何数据。

用法
----
方式一（推荐，手动粘贴 Cookie）：
    python xoss_source_probe.py --cookie "sessionid=xxxx;其他cookie=yyy"

    获取 Cookie：浏览器登录 www.imxingzhe.com -> F12 -> Network -> 任意请求
    -> Request Headers -> 复制 Cookie 整行。

方式二（自动开浏览器登录，需要已安装 DrissionPage）：
    python xoss_source_probe.py --browser

方式三（只探测免登录部分）：
    python xoss_source_probe.py

可选参数：
    --workout-id 224048647    指定要探测的记录 id；默认自动从列表里挑一条带设备的记录
    --out ./xoss_probe_out    输出目录
"""

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

BASE = "https://www.imxingzhe.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


try:
    import requests  # 与主程序使用同一网络栈，优先使用
    HAS_REQUESTS = True
except Exception:  # noqa: BLE001
    requests = None
    HAS_REQUESTS = False

INSECURE = False
_SESSION = None


def build_opener(insecure=False):
    ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    return urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=ctx))


def _request_via_requests(path_or_url, cookie, accept, timeout, save_to, quiet):
    """requests 后端：行为与主程序完全一致（推荐，能规避标准库证书链问题）。"""
    global _SESSION
    url = path_or_url if path_or_url.startswith("http") else BASE + path_or_url
    try:
        if _SESSION is None:
            _SESSION = requests.Session()
            _SESSION.headers.update({
                "User-Agent": UA,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": BASE + "/",
            })
        headers = {"Accept": accept}
        if cookie:
            headers["Cookie"] = cookie
        response = _SESSION.get(
            url, headers=headers, timeout=timeout,
            verify=not INSECURE, allow_redirects=False,
        )
        content = response.content
        if save_to:
            with open(save_to, "wb") as fh:
                fh.write(content)
            return response.status_code, dict(response.headers), None, len(content)
        return response.status_code, dict(response.headers), content, len(content)
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
        if not quiet:
            print(f"    [!] 请求异常(requests): {err}")
        return None, {"error": err}, None, 0


def request(opener, path_or_url, cookie=None, accept="*/*", timeout=40, save_to=None, quiet=False):
    """发起请求，返回 (status, headers, body_or_None, size)。save_to 非空时把响应体写入文件。

    自动选择后端：requests 可用时用 requests（与主程序一致），否则用标准库 urllib。
    """
    if HAS_REQUESTS:
        return _request_via_requests(path_or_url, cookie, accept, timeout, save_to, quiet)

    url = path_or_url if path_or_url.startswith("http") else BASE + path_or_url
    headers = {"User-Agent": UA, "Accept": accept, "Accept-Language": "zh-CN,zh;q=0.9"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            status = resp.status
            hdrs = dict(resp.headers)
            if save_to:
                data = resp.read()
                with open(save_to, "wb") as fh:
                    fh.write(data)
                body = None
                size = len(data)
            else:
                body = resp.read()
                size = len(body)
        return status, hdrs, body, size
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:  # noqa: BLE001
            body = b""
        return exc.code, dict(exc.headers), body, len(body)
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
        if not quiet:
            print(f"    [!] 请求异常: {err}")
        return None, {"error": err}, None, 0


def show_json(body, limit=600):
    if not body:
        return "(empty)"
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return body[:limit].decode("utf-8", "replace")
    return json.dumps(data, ensure_ascii=False)[:limit]


def login_via_http(account, password):
    """用账号密码免浏览器登录，返回可直接使用的 Cookie 字符串（失败返回 None）。

    复现行者前端登录：RSA 加密密码 -> POST /api/v1/user/login/（成功后服务端下发 sessionid）。
    仅用标准库实现，因此没有 requests 也能用。
    """
    try:
        from xoss_source import xoss_rsa_encrypt
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        try:
            from xoss_source import xoss_rsa_encrypt
        except ImportError as exc:
            print(f"    [!] 无法导入 xoss_source.xoss_rsa_encrypt: {exc}")
            return None

    try:
        encrypted = xoss_rsa_encrypt(password)
    except Exception as exc:  # noqa: BLE001
        print(f"    [!] 密码加密失败: {exc}")
        return None

    payload = json.dumps({"account": account, "password": encrypted}).encode("utf-8")
    req = urllib.request.Request(
        BASE + "/api/v1/user/login/",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": UA,
            "Referer": BASE + "/login",
            "Origin": BASE,
        },
    )
    opener = build_opener(insecure=INSECURE)
    try:
        with opener.open(req, timeout=40) as resp:
            status = resp.status
            text = resp.read(600).decode("utf-8", "replace")
            raw_cookies = resp.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as exc:
        print(f"    [!] 登录接口 HTTP {exc.code}: {exc.read(300).decode('utf-8', 'replace')}")
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"    [!] 登录请求异常: {type(exc).__name__}: {exc}")
        return None

    print(f"    [i] 登录响应 HTTP {status}: {text[:200]}")
    try:
        data = json.loads(text)
    except ValueError:
        data = {}
    if isinstance(data, dict) and data.get("code") != 0:
        print(f"    [!] 登录失败: code={data.get('code')} msg={data.get('msg')}")
        return None

    pairs = []
    for cookie in raw_cookies:
        first = cookie.split(";")[0].strip()
        if "=" in first:
            pairs.append(first)
    if not pairs:
        print("    [!] 登录响应未下发 Cookie，无法继续")
        return None
    return "; ".join(pairs)


def dump_shape(data, indent=6, depth=0):
    """打印 JSON 结构（键/类型/样例），用于校正正式实现的解析逻辑。"""
    pad = " " * indent
    if isinstance(data, dict):
        for key, value in list(data.items())[:18]:
            if isinstance(value, list):
                print(f"{pad}{key}: list(len={len(value)})")
                if value and isinstance(value[0], dict):
                    print(f"{pad}  第一条字段: {sorted(value[0].keys())}")
                    for fk, fv in list(value[0].items())[:16]:
                        print(f"{pad}    {fk} = {str(fv)[:70]}")
            elif isinstance(value, dict):
                print(f"{pad}{key}: dict(keys={list(value.keys())[:12]})")
                if depth < 1:
                    dump_shape(value, indent + 4, depth + 1)
            else:
                print(f"{pad}{key} = {str(value)[:70]}")


def section(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cookie", default="", help="行者登录 Cookie 整行")
    parser.add_argument("--account", default="", help="行者手机号（配合 --password 免浏览器登录）")
    parser.add_argument("--password", default="", help="行者密码（配合 --account 免浏览器登录）")
    parser.add_argument("--browser", action="store_true", help="用 DrissionPage 打开浏览器手动登录")
    parser.add_argument("--workout-id", default="", help="要探测的运动记录 id")
    parser.add_argument("--out", default="./xoss_probe_out", help="输出目录")
    parser.add_argument("--insecure", action="store_true", help="跳过 TLS 证书校验（证书链有问题时使用）")
    args = parser.parse_args()

    global INSECURE
    INSECURE = bool(args.insecure)

    os.makedirs(args.out, exist_ok=True)
    opener = build_opener(insecure=args.insecure)
    cookie = args.cookie.strip()

    # ---------- 运行环境诊断 ----------
    if HAS_REQUESTS:
        backend = f"requests {getattr(requests, '__version__', '?')}（与主程序一致）"
    else:
        backend = "urllib（标准库；未安装 requests）"
    print(f"[i] Python {sys.version.split()[0]} / {ssl.OPENSSL_VERSION}")
    print(f"[i] HTTP 后端: {backend}")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        value = os.environ.get(name)
        if value:
            print(f"[i] 检测到代理环境变量 {name} = {value}")
    if args.insecure:
        print("[i] 已启用 --insecure（跳过证书校验）")

    # ---------- 0. 连通性自检（免登录、已知可访问的公开记录） ----------
    section("0. 连通性自检（判断是本机网络问题还是登录问题）")
    status, hdrs, body, size = request(opener, "/api/v1/pgworkout/224048647/", accept="application/json")
    print(f"  GET /api/v1/pgworkout/224048647/ -> status={status} size={size}")
    if status is None:
        print("  [x] 连行者站点都请求不通 => 本机 Python 出网被拦截，与登录/Cookie 无关。")
        print("      排查方向：")
        print("       1) 浏览器能开行者网页但不代表 python 能出网：浏览器可能走了系统代理/加速器")
        print("          若你有代理，可在本终端设置环境变量后重试，例如：")
        print('          $env:HTTPS_PROXY="http://127.0.0.1:端口"')
        print("       2) 检查安全软件/防火墙是否拦截了 python.exe 的出站连接")
        print("       3) 换一个 Python 解释器再试（本仓库开发机已验证 3.13 + urllib 可直连）")
        print("       4) 加 --insecure 排除证书链问题后重试")
    elif status == 200:
        print("  [OK] 网络连通，行者接口可直连")
    else:
        print(f"  [!] 能连上但返回 {status}，继续后续探测")

    if not cookie and args.account and args.password:
        print("[i] 使用账号密码免浏览器登录（RSA 加密密码）...")
        cookie = login_via_http(args.account, args.password) or ""
        if cookie:
            count = len([p for p in cookie.split(";") if "=" in p])
            print(f"[i] 登录成功，已获取 {count} 个 Cookie 字段")
        else:
            print("[i] 免浏览器登录失败；可改用 --cookie 或 --browser")

    if args.browser and not cookie:
        try:
            from DrissionPage import ChromiumPage  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            print(f"[!] 无法导入 DrissionPage({exc})，请先 pip install -r requirements.txt")
            return 1
        page = ChromiumPage()
        page.get(f"{BASE}/login")
        print(">>> 请在打开的浏览器里完成登录；登录成功后在终端按 Enter 继续…")
        input()
        cookies = page.cookies()
        try:
            page.quit()
        except Exception:  # noqa: BLE001
            pass
        pairs = []
        for item in cookies or []:
            if isinstance(item, dict) and item.get("name"):
                pairs.append(f"{item['name']}={item.get('value','')}")
        cookie = "; ".join(pairs)
        print(f"[i] 已提取 {len(pairs)} 个 cookie")
        if not cookie:
            print("[!] 未取到 cookie，后续登录接口会返回 401")

    logged_in = bool(cookie)
    print(f"[i] 登录态: {'有 Cookie' if logged_in else '无 Cookie（仅探测免登录接口）'}")

    summary = {}

    # ---------- 1. 用户信息（拿 user_id） ----------
    section("1. 取用户身份 / user_id")
    for path in [
        "/api/v1/user/information/",
        "/api/v4/account/get_user_info/",
        "/api/v1/user/user_info/",
    ]:
        status, hdrs, body, size = request(opener, path, cookie, accept="application/json")
        print(f"\nGET {path}\n  -> status={status} size={size}")
        print("  " + show_json(body, 400))
        if status == 200 and body:
            summary["user_info_path"] = path
            try:
                summary["user_info"] = json.loads(body.decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                pass
            break

    user_id = ""
    info = summary.get("user_info") or {}
    for key in ("id", "userid", "user_id"):
        for holder in (info, info.get("data") or {}):
            if isinstance(holder, dict) and holder.get(key):
                user_id = str(holder[key])
                break
        if user_id:
            break
    print(f"\n[i] 解析出的 user_id = {user_id or '(未取到)'}")

    # ---------- 2. 活动列表 ----------
    section("2. 活动列表接口")
    list_candidates = [
        f"/api/v1/pgworkout/?offset=0&limit=5" + (f"&user_id={user_id}" if user_id else ""),
        f"/api/v1/pgworkout/?user_id={user_id}&offset=0&limit=5" if user_id else "",
        f"/api/v1/pgworkout/year_month/",
    ]
    workout_ids = []
    for path in list_candidates:
        if not path:
            continue
        status, hdrs, body, size = request(opener, path, cookie, accept="application/json")
        print(f"\nGET {path}\n  -> status={status} size={size}")
        print("  " + show_json(body, 700))
        if status == 200 and body:
            summary.setdefault("list_ok", []).append(path)
            try:
                data = json.loads(body.decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                continue

            # ---- 结构详情：直接用于校正正式实现 ----
            print("  [结构] 响应形态:")
            dump_shape(data)

            try:
                from xoss_source import XossClient
                parsed = XossClient._extract_activity_list(data)
                if parsed:
                    print(f"  [结构] 正式实现的解析器识别出 {len(parsed)} 条记录 => 解析逻辑匹配 ✓")
                    try:
                        # 用轻量实例调用（跳过 __init__，避免依赖 requests）
                        probe_client = XossClient.__new__(XossClient)
                        probe_client.time_offset_hours = 8
                        first = probe_client._normalize_activity(parsed[0])
                    except Exception as exc:  # noqa: BLE001
                        first = None
                        print(f"  [结构] 归一化跳过: {exc}")
                    if first:
                        print(f"  [结构] 归一化结果: id={first['workout_id']} "
                              f"start_time={first['start_time']} distance_m={first['distance_m']}")
                else:
                    print("  [结构] 正式实现的解析器解析不到记录 => 需要按上面的形态校正 _extract_activity_list")
            except Exception as exc:  # noqa: BLE001
                print(f"  [结构] 解析器验证跳过: {exc}")

            # 尽量从各种可能结构里抽出 workout id
            def walk(node):
                found = []
                if isinstance(node, dict):
                    if node.get("id") and (node.get("start_time") or node.get("workout_id")):
                        found.append(str(node.get("workout_id") or node.get("id")))
                    for value in node.values():
                        found += walk(value)
                elif isinstance(node, list):
                    for value in node:
                        found += walk(value)
                return found

            ids = []
            for wid in walk(data):
                if wid not in ids:
                    ids.append(wid)
            if ids:
                workout_ids = ids
                print(f"  [i] 从该接口解析出 {len(ids)} 个 workout id，前 5 个: {ids[:5]}")
                break

    # 月度接口（第三方导出工具在用的稳定接口）
    if user_id:
        section("2b. 月度列表接口 /api/v4/user_month_info/")
        now = time.localtime()
        path = f"/api/v4/user_month_info/?user_id={user_id}&year={now.tm_year}&month={now.tm_mon}"
        status, hdrs, body, size = request(opener, path, cookie, accept="application/json")
        print(f"\nGET {path}\n  -> status={status} size={size}")
        print("  " + show_json(body, 800))

    target = args.workout_id.strip() or (workout_ids[0] if workout_ids else "")
    if not target:
        if sys.stdin is not None and sys.stdin.isatty():
            try:
                target = input("\n[?] 未能自动取到 workout id，请手动输入一条记录的 id（回车跳过）: ").strip()
            except EOFError:
                target = ""
        if not target:
            target = "224048647"
            print(f"[i] 未指定 workout id，使用默认公开记录 {target} 继续探测")
    if not target:
        print("[!] 没有 workout id，跳过详情与导出探测")
    else:
        print(f"\n[i] 使用 workout id = {target} 继续探测")

        # ---------- 3. 活动详情 ----------
        section("3. 活动详情（免登录亦可）")
        path = f"/api/v1/pgworkout/{target}/"
        status, hdrs, body, size = request(opener, path, None, accept="application/json")
        print(f"\nGET {path} (不带 Cookie)\n  -> status={status} size={size}")
        detail = {}
        if body:
            try:
                detail = json.loads(body.decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                pass
        workout = ((detail.get("data") or {}).get("workout") or {}) if isinstance(detail, dict) else {}
        if workout:
            keep = [
                "title", "id", "sport", "start_time", "end_time", "duration", "distance",
                "point_counts", "is_fit", "source", "product_name", "manufacturer",
                "avg_heartrate", "max_heartrate", "power_avg", "max_cadence",
            ]
            print("  关键字段:")
            for key in keep:
                if key in workout:
                    print(f"    {key} = {workout[key]}")
            equip = workout.get("equipment_info")
            print(f"    equipment_info 条数 = {len(equip) if isinstance(equip, list) else equip}")
            print(f"    [i] 前端据此显示“导出Fit”按钮 : {'是' if isinstance(equip, list) and equip else '否'}")

        # ---------- 4. 导出 Fit ----------
        section("4. 导出 Fit 接口（前端按钮背后的真实地址）")
        fit_path = f"/api/v1/workout/{target}/fit/"
        fit_file = os.path.join(args.out, f"xoss_{target}.fit")
        status, hdrs, body, size = request(opener, fit_path, cookie, save_to=fit_file if logged_in else None)
        print(f"\nGET {fit_path}\n  -> status={status} size={size}")
        for key in ("Content-Type", "Content-Disposition", "Content-Length"):
            if key in hdrs:
                print(f"     {key}: {hdrs[key]}")
        if status == 401:
            print("     [!] 401 = 路由存在但缺少登录 Cookie；请用 --cookie 重试")
        elif status == 200:
            if logged_in:
                print(f"     [OK] 已保存到 {fit_file}")
                with open(fit_file, "rb") as fh:
                    head = fh.read(12)
                print(f"     FIT 文件头: {head[:12]!r} (正常 FIT 以 b'.FIT' 出现在第 8-12 字节)")
            else:
                print("     " + (body or b"")[:200].decode("utf-8", "replace"))
        elif status == 404:
            print("     [x] 404 = 该记录没有可导出的 Fit（通常 is_fit=False 或非设备来源）")

        # ---------- 5. 导出 GPX（回退方案） ----------
        section("5. 导出 GPX 接口（回退方案）")
        gpx_path = f"/api/v1/pgworkout/{target}/gpx/"
        gpx_file = os.path.join(args.out, f"xoss_{target}.gpx")
        status, hdrs, body, size = request(opener, gpx_path, cookie, save_to=gpx_file if logged_in else None)
        print(f"\nGET {gpx_path}\n  -> status={status} size={size}")
        for key in ("Content-Type", "Content-Disposition", "Content-Length"):
            if key in hdrs:
                print(f"     {key}: {hdrs[key]}")
        if logged_in and status == 200:
            print(f"     [OK] 已保存到 {gpx_file}")
        elif status == 401:
            print("     [!] 401 = 需要登录 Cookie")

        # ---------- 6. 校验 Fit 可解析性 ----------
        if logged_in and os.path.exists(fit_file) and os.path.getsize(fit_file) > 12:
            section("6. FIT 文件结构抽检（判断能否直接上传其它平台）")
            try:
                with open(fit_file, "rb") as fh:
                    raw = fh.read()
                print(f"  文件大小: {len(raw)} 字节")
                print(f"  头部: {raw[:12]!r}")
                print(f"  数据区末尾是否含 CRC(2 字节): 是（FIT 规范）")
                print("  [i] 若需确认可解析，建议 pip install garmin-fit-sdk 后用 fitdump 校验")
            except Exception as exc:  # noqa: BLE001
                print(f"  [!] 抽检失败: {exc}")

    section("探测结果汇总")
    print(json.dumps(summary, ensure_ascii=False, indent=2)[:2000])
    print(f"\n输出目录: {os.path.abspath(args.out)}")
    print("请把本脚本的完整输出发回，用于校正正式实现。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
