# -*- coding: utf-8 -*-
"""
VPS上运行的Dola SD2.5 30秒视频生成脚本（完整去水印版）
参考星空管理器去水印核心：
1. 从video_model提取fallback_api
2. 调用fallback_api设置logo_type=unwatermarked
3. qAAB加密URL用AES-CBC解密
用法: python3 -u dola_gen_clean.py <cookie_json> <输出目录> <提示词> <名称>
"""
import sys, json, time, os, re, uuid, base64, requests, hashlib, hmac, binascii
from urllib.parse import urlencode, urlparse, parse_qs, parse_qsl, urlunparse, quote
from datetime import datetime, timezone

DOLA_HOST = "www.dola.com"
DOLA_BOT_ID = "7339470689562525703"

# 星空管理器QAAB固定salt
DOLA_QAAB_SALT_B64 = 'TdTC5rgxYgkOUrPHpnM7pByyRiuCmrWKGWs521cXdST0m69/COjWjSanLjfBqVovHwWlGJKu8pSXMrYqOKrdWA=='

import os as _os
_PROXY_URL = _os.environ.get("DOLA_PROXY", "")
PROXIES = {"http": _PROXY_URL, "https": _PROXY_URL} if _PROXY_URL else None

# b2proxy 香港住宅代理 — 和注册扫码保持一致，避免IP不一致导致风控
# 同任务同 sid = 同一个出口 IP，持续 45 分钟；不同任务不同 sid = 不同 IP
UDEALPROXY_USER = "userId-6642-custom-17941"
UDEALPROXY_PASS = "alMLlU"
UDEALPROXY_PORT = 6666
UDEALPROXY_HOST = "us.udealproxy.com"
UDEALPROXY_STICKY_MIN = 50
_B2PROXY_HOSTS = {"hk": "hk.rrp.bestgo.work", "sg": "sg.rrp.bestgo.work",
                   "jp": "jp.rrp.bestgo.work", "tw": "tw.rrp.bestgo.work",
                   "any": "hk.rrp.bestgo.work"}
_B2PROXY_REGIONS = ["tw", "tw", "hk", "sg"]  # 优先香港，备选新加坡、日本
_CLIPROXY_SEQ = "/tmp/dola_sticky.seq"
_CLIPROXY_LOCK = "/tmp/dola_sticky.lock"
SESSION_PROXIES = None
SESSION_SID = ""
SESSION_REGION = ""


def _gen_sid():
    import random
    import string
    return "".join(random.choices(string.ascii_letters + string.digits, k=10))


_IP_BUCKET_FILE = "/tmp/dola_ip_bucket_v1.json"


def _detect_model_bucket():
    model = ""
    duration = 30
    try:
        if len(sys.argv) > 9:
            model = str(sys.argv[9]).strip().lower()
        if len(sys.argv) > 6:
            duration = int(sys.argv[6])
    except Exception:
        pass
    if "v2.0" in model or "sd2.0" in model or duration <= 15:
        return "sd20", 45
    return "sd25", 45


def _ip_bucket_state():
    try:
        with open(_IP_BUCKET_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _mark_ip_bucket_consumed():
    try:
        bucket, _minutes = _detect_model_bucket()
        st = _ip_bucket_state()
        ent = st.get(bucket) or {}
        if ent.get("sid"):
            ent["consumed"] = True
            st[bucket] = ent
            with open(_IP_BUCKET_FILE, "w") as f:
                json.dump(st, f)
            print(f"[stickyIP] {bucket} 出片成功标记消耗，下任务换IP")
    except Exception as e:
        print(f"[stickyIP] mark consumed err: {e}")


def _build_sticky_proxies(tag="task"):
    import fcntl
    sid = _gen_sid()
    if _os.environ.get("DOLA_DYN_PROXY", "1") != "1":
        return None
    bucket, bucket_minutes = _detect_model_bucket()
    bucket_idx = int(time.time() // (bucket_minutes * 60))
    region = "tw"
    try:
        with open(_CLIPROXY_LOCK, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            st = _ip_bucket_state()
            ent = st.get(bucket) or {}
            if ent.get("bucket") == bucket_idx and not ent.get("consumed") and ent.get("sid"):
                sid = ent["sid"]
                region = ent["region"]
                print(f"[stickyIP] {tag} {bucket} 复用桶#{bucket_idx} region={region} sid={sid} (每{bucket_minutes}分钟换IP/出片后换)")
            else:
                seq = 0
                try:
                    seq = int(open(_CLIPROXY_SEQ).read().strip() or "0")
                except Exception:
                    pass
                region = "tw"
                try:
                    open(_CLIPROXY_SEQ, "w").write(str(seq + 1))
                except Exception:
                    pass
                st[bucket] = {"bucket": bucket_idx, "sid": sid, "region": region, "consumed": False}
                with open(_IP_BUCKET_FILE, "w") as f:
                    json.dump(st, f)
                print(f"[stickyIP] {tag} {bucket} 新桶#{bucket_idx} region={region} sid={sid} (每{bucket_minutes}分钟换IP/出片后换)")
    except Exception as e:
        print(f"[stickyIP] bucket err: {e}")
    host = UDEALPROXY_HOST
    user = f"{UDEALPROXY_USER}-region-tw-session-{sid}-sessTime-{UDEALPROXY_STICKY_MIN}"
    url = f"http://{user}:{UDEALPROXY_PASS}@{host}:{UDEALPROXY_PORT}"
    proxies = {"http": url, "https": url}
    global SESSION_SID, SESSION_REGION
    SESSION_SID = sid
    SESSION_REGION = region
    return proxies


def _active_proxies():
    global SESSION_PROXIES
    if SESSION_PROXIES is None:
        if _os.environ.get("DOLA_DYN_PROXY", "1") == "1":
            SESSION_PROXIES = _build_sticky_proxies("init")
        elif _PROXY_URL:
            SESSION_PROXIES = PROXIES
    if not SESSION_PROXIES:
        print("[代理] \u274c 无可用代理，强制终止（禁止直连裸IP）")
        sys.exit(1)
    return SESSION_PROXIES


def _rotate_proxies(reason=""):
    """强制换到新代理session(新出口IP)，当前出口连不上dola/被重置时用；始终走代理，绝不裸连"""
    global SESSION_PROXIES
    try:
        bucket, _m = _detect_model_bucket()
        st = _ip_bucket_state()
        if st.get(bucket):
            st[bucket]["consumed"] = True
            with open(_IP_BUCKET_FILE, "w") as _f:
                json.dump(st, _f)
    except Exception as _re:
        print(f"[stickyIP] rotate mark err: {_re}")
    SESSION_PROXIES = None
    print(f"[stickyIP] 因({reason})强制换新出口IP...")
    return _active_proxies()


def base64_decode_bytes(value):
    encoded = str(value or '').strip().replace('-', '+').replace('_', '/')
    while len(encoded) % 4:
        encoded += '='
    return base64.b64decode(encoded)

def decode_qaab_token(token, key_seed):
    """解密qAAB加密的URL（参考星空管理器）"""
    try:
        encrypted = base64_decode_bytes(token)
        seed = base64_decode_bytes(key_seed)
        salt = base64_decode_bytes(DOLA_QAAB_SALT_B64)

        if len(encrypted) <= 4 or encrypted[0] != 0xa8 or encrypted[1] != 0x00 or encrypted[2] != 0x01 or encrypted[3] != 0x00:
            return ''

        # SHA-512派生密钥
        first_hash = hashlib.sha512(seed).digest()
        material = first_hash + salt
        derived = hashlib.sha512(material).digest()
        key = derived[:16]
        iv = derived[16:32]

        # AES-CBC解密
        from Crypto.Cipher import AES
        cipher = AES.new(key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted[4:])
        # 去除PKCS7填充
        pad_len = decrypted[-1]
        if pad_len <= 16:
            decrypted = decrypted[:-pad_len]
        text = decrypted.decode('utf-8', errors='ignore').rstrip('\x00').strip()
        return text if text.startswith('http') else ''
    except Exception as e:
        print(f"  [QAAB解密] 失败: {e}")
        return ''

def decode_media_value(value):
    """解码base64或直接返回URL"""
    if not value or not isinstance(value, str):
        return ''
    if value.startswith('http'):
        return value
    try:
        decoded = base64_decode_bytes(value).decode('utf-8', errors='ignore').rstrip('\x00').strip()
        return decoded if decoded.startswith('http') else ''
    except:
        return ''

def is_clean_url(url):
    """判断是否是无水印URL"""
    if not url:
        return False
    lower = url.lower()
    return ('logo_type=unwatermarked' in lower or
            'lr=unwatermarked' in lower or
            'video_gen_no_watermark' in lower) and \
           'logo_type=watermarked' not in lower and \
           'watermark=true' not in lower

def replace_fallback_params(url):
    """设置fallback_api参数为无水印（参考星空管理器）"""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    params['channel'] = ['no']
    params['codec_type'] = ['8']
    params['logo_type'] = ['unwatermarked']
    new_query = '&'.join(f'{k}={v[0]}' for k, v in params.items())
    return urlunparse(parsed._replace(query=new_query))

def _cookie_kv(cookie_header, key):
    m = re.search(key + r"=([^;]+)", cookie_header or "")
    return m.group(1) if m else ""


def _task_browser_identity():
    """设备指纹随机化：每次任务进程随机生成一套自洽的浏览器身份（UA+client hints+语言）。
    同一进程（同一任务）内保持一致，跨任务随机变化，降低跨账号设备关联风控。"""
    import random
    chrome_versions = ["126.0.0.0", "127.0.0.0", "128.0.0.0", "129.0.0.0", "130.0.0.0", "131.0.0.0"]
    ver = random.choice(chrome_versions)
    major = ver.split(".")[0]
    nab = random.choice(["24", "99", "8"])
    # 改为苹果移动端（iPhone），风控更宽松
    ios_versions = ["15_0", "15_4", "15_6", "16_0", "16_1", "16_2", "16_3", "16_4", "16_5", "16_6", "17_0", "17_1", "17_2"]
    ios_ver = random.choice(ios_versions)
    safari_ver = ios_ver.split("_")[0] + ".0"
    
    user_agent = (
        f"Mozilla/5.0 (iPhone; CPU iPhone OS {ios_ver} like Mac OS X) "
        f"AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{safari_ver} "
        f"Mobile/15E148 Safari/604.1"
    )
    ch_platform = '"iOS"'
    sec_ch_ua = f'"Safari";v="{safari_ver.split(".")[0]}", "iOS";v="{ios_ver.split("_")[0]}", "WebKit";v="605"'
    
    accept_language = random.choice([
        "zh-CN,zh;q=0.9,en;q=0.8",
        "zh-CN,zh;q=0.9",
        "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "en-US,en;q=0.9,zh-CN;q=0.8",
        "en,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    ])

    return {
        "user_agent": user_agent,
        "sec_ch_ua": sec_ch_ua,
        "sec_ch_ua_platform": ch_platform,
        "sec_ch_ua_mobile": "?1",
        "accept_language": accept_language,
    }


_TASK_IDENTITY = _task_browser_identity()


def build_submit_url(cookie_header=""):
    """浏览器风格提交参数（方案实测版）：aid=495671 + region=JP + browser + fp/msToken"""
    params = {
        "aid": "495671", "channel": "g", "device_platform": "web",
        "language": "zh-Hant", "region": "SG", "sys_region": "SG",
        "samantha_web": "1", "use-olympus-account": "1",
        "version_code": "20800", "web_platform": "browser",
        "web_tab_id": str(uuid.uuid4()),
    }
    ms = _cookie_kv(cookie_header, "msToken")
    fp = _cookie_kv(cookie_header, "s_v_web_id")
    if ms:
        params["msToken"] = ms
    if fp:
        params["fp"] = fp
    return f"https://{DOLA_HOST}/chat/completion?{urlencode(params)}"


def build_pc_submit_url(duration=30, cookie_header=""):
    """PC客户端风格提交URL（30秒解锁三要素：pc_version=3.28.8 + runtime=pc_client + 全新web_tab_id）"""
    version = "3.28.8"  # 风控修复：统一PC版本
    params = {
        "aid": "582465", "real_aid": "582465",
        "pc_version": version, "doubao_pc_version": version,
        "device_platform": "web", "doubao_device_platform": "desktop",
        "web_platform": "desktop", "client_platform": "desktop",
        "runtime": "pc_client", "language": "zh",
        "region": "SG", "sys_region": "SG", "tz_name": "Asia/Shanghai",
        "pkg_type": "release_version", "version_code": "20800",
        "samantha_web": "1", "use-olympus-account": "1",
        "web_tab_id": str(uuid.uuid4()),
    }
    ms = _cookie_kv(cookie_header, "msToken")
    fp = _cookie_kv(cookie_header, "s_v_web_id")
    if ms:
        params["msToken"] = ms
    if fp:
        params["fp"] = fp
    return f"https://{DOLA_HOST}/chat/completion?{urlencode(params)}"

def build_poll_url(cookie_header=""):
    # 风控修复：轮询也走PC客户端风格，全链路aid=582465
    params = {
        "version_code": "20800", "language": "zh",
        "device_platform": "web", "doubao_device_platform": "desktop",
        "aid": "582465", "real_aid": "582465",
        "pkg_type": "release_version",
        "pc_version": "3.28.8", "doubao_pc_version": "3.28.8",
        "region": "SG", "sys_region": "SG",
        "samantha_web": "1", "web_platform": "desktop",
        "client_platform": "desktop", "runtime": "pc_client",
        "use-olympus-account": "1", "web_tab_id": str(uuid.uuid4()),
    }
    ms = _cookie_kv(cookie_header, "msToken")
    fp = _cookie_kv(cookie_header, "s_v_web_id")
    if ms:
        params["msToken"] = ms
    if fp:
        params["fp"] = fp
    return f"https://{DOLA_HOST}/im/chain/single?{urlencode(params)}"

def crc32_hex(data):
    return format(binascii.crc32(data) & 0xFFFFFFFF, "x")


def aws4_sign_headers(method, url, ak, sk, sts_token):
    """AWS4-HMAC-SHA256 签名（火山引擎 ImageX ApplyImageUpload）"""
    u = urlparse(url)
    now = datetime.now(timezone.utc)
    date = now.strftime("%Y%m%dT%H%M%SZ")
    date_short = date[:8]
    params = sorted(parse_qsl(u.query, keep_blank_values=True))
    canonical_query = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in params)
    canonical_headers = f"x-amz-date:{date}\nx-amz-security-token:{sts_token}\n"
    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical_request = "\n".join([
        method, u.path, canonical_query, canonical_headers,
        "x-amz-date;x-amz-security-token", payload_hash
    ])
    scope = f"{date_short}/us-east-1/imagex/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", date, scope,
        hashlib.sha256(canonical_request.encode()).hexdigest()
    ])
    k_date = hmac.new(("AWS4" + sk).encode(), date_short.encode(), hashlib.sha256).digest()
    k_region = hmac.new(k_date, b"us-east-1", hashlib.sha256).digest()
    k_service = hmac.new(k_region, b"imagex", hashlib.sha256).digest()
    k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "Authorization": f"AWS4-HMAC-SHA256 Credential={ak}/{scope}, SignedHeaders=x-amz-date;x-amz-security-token, Signature={signature}",
        "x-amz-date": date,
        "x-amz-security-token": sts_token,
    }


def upload_image_to_dola(image_path, cookie_header):
    """本地图片/音频 → 火山 ImageX 上传，返回 StoreUri"""
    with open(image_path, "rb") as f:
        file_buf = f.read()
    ext = os.path.splitext(image_path)[1].lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".mp3", ".wav", ".m4a"):
        ext = ".png"

    prep_params = {
        "aid": "582465", "real_aid": "582465",
        "device_platform": "web", "doubao_device_platform": "desktop",
        "language": "zh-Hant",
        "region": "SG", "sys_region": "SG", "samantha_web": "1",
        "use-olympus-account": "1", "version_code": "20800",
        "web_platform": "desktop", "client_platform": "desktop",
        "runtime": "pc_client", "pkg_type": "release_version",
        "pc_version": "3.28.8", "doubao_pc_version": "3.28.8",
        "web_tab_id": str(uuid.uuid4()),
    }
    ms = _cookie_kv(cookie_header, "msToken")
    fp = _cookie_kv(cookie_header, "s_v_web_id")
    if ms:
        prep_params["msToken"] = ms
    if fp:
        prep_params["fp"] = fp
    prep_headers = {
        "User-Agent": _TASK_IDENTITY["user_agent"],
        "Accept": "*/*", "Accept-Encoding": "identity",
        "Content-Type": "application/json", "agw-js-conv": "str, str",
        "Origin": "https://www.dola.com", "Referer": "https://www.dola.com/chat/",
        "Cookie": cookie_header,
    }
    r1 = requests.post(f"https://{DOLA_HOST}/alice/resource/prepare_upload", params=prep_params,
                       json={"tenant_id": "5", "scene_id": "4", "resource_type": 2},
                       headers=prep_headers, timeout=20, proxies=_active_proxies())
    p1 = r1.json()
    if p1.get("code") != 0:
        raise RuntimeError(f"prepare_upload失败: {json.dumps(p1, ensure_ascii=False)[:200]}")
    d = p1["data"]
    auth = d["upload_auth_token"]
    apply_url = (f"https://{d['upload_host']}/?Action=ApplyImageUpload&Version=2018-08-01"
                 f"&ServiceId={d['service_id']}&FileSize={len(file_buf)}&FileExtension={ext}")
    sig = aws4_sign_headers("GET", apply_url, auth["access_key"], auth["secret_key"], auth["session_token"])
    r2 = requests.get(apply_url, headers=sig, timeout=20, proxies=_active_proxies())
    p2 = r2.json()
    if p2.get("ResponseMetadata", {}).get("Error"):
        raise RuntimeError(f"ApplyImageUpload失败: {p2['ResponseMetadata']['Error'].get('Message', '')[:150]}")
    addr = p2["Result"]["UploadAddress"]
    store_info = addr["StoreInfos"][0]
    tos_url = f"https://{addr['UploadHosts'][0]}/{store_info['StoreUri']}"
    tos_headers = {
        "Authorization": store_info["Auth"],
        "Content-Type": "application/octet-stream",
        "content-crc32": crc32_hex(file_buf),
        "Content-Disposition": f'attachment; filename="file{ext}"',
    }
    r3 = requests.put(tos_url, data=file_buf, headers=tos_headers, timeout=60, proxies=_active_proxies())
    if r3.status_code != 200:
        raise RuntimeError(f"PUT TOS失败: HTTP {r3.status_code} {r3.text[:150]}")
    return store_info["StoreUri"]


def submit_video(prompt, ratio, duration, model, cookie_header, image_paths=None, audio_path=""):
    duration = int(duration)
    if model == "seedance_v2.5":
        if duration not in (10, 30):
            duration = 10
    elif duration not in (5, 10, 15):
        duration = 10
    ref_image_uris = []
    for p in (image_paths or []):
        try:
            _uri = upload_image_to_dola(p, cookie_header)
            ref_image_uris.append(_uri)
            print(f"[参考图] 上传成功 {os.path.basename(p)} -> {_uri[:60]}")
        except Exception as e:
            print(f"[参考图] 上传失败({os.path.basename(p)}): {e}")
    ref_audio_uri = ""
    if audio_path:
        try:
            ref_audio_uri = upload_image_to_dola(audio_path, cookie_header)
            print(f"[参考音频] 上传成功 -> {ref_audio_uri[:60]}")
        except Exception as e:
            print(f"[参考音频] 上传失败: {e}")

    now = int(time.time() * 1000)
    text = f"生成影片：{prompt}，{ratio}"
    unique_key = uuid.uuid4().hex

    ability_param = {"ratio": ratio, "model": model, "duration": duration, "camera_movement": "fixed"}
    content = {"text_block": {"text": text, "icon_url": "", "icon_url_dark": "", "summary": ""}, "pc_event_block": ""}

    def _ref_attach_block(att):
        return {
            "local_message_id": str(uuid.uuid4()),
            "content_block": [{
                "block_type": 10052,
                "content": {"attachment_block": {"attachments": [att]}, "pc_event_block": ""},
                "block_id": str(uuid.uuid4()), "parent_id": "",
                "meta_info": [], "append_fields": [],
            }],
            "message_status": 0,
        }

    image_msgs = []
    for _uri in ref_image_uris:
        _att = {"type": 1, "identifier": str(uuid.uuid4()),
                "image": {"name": "image.png", "uri": _uri,
                          "image_ori": {"url": "", "width": 1024, "height": 1024, "format": "", "url_formats": {}}},
                "parse_state": 0, "review_state": 1, "upload_status": 1, "progress": 100, "src": ""}
        image_msgs.append(_ref_attach_block(_att))
    audio_msg = None
    if ref_audio_uri:
        _att = {"type": 2, "identifier": str(uuid.uuid4()),
                "audio": {"name": "audio.mp3", "uri": ref_audio_uri},
                "parse_state": 0, "review_state": 1, "upload_status": 1, "progress": 100, "src": ""}
        audio_msg = _ref_attach_block(_att)
    text_msg = {
        "local_message_id": str(uuid.uuid4()),
        "content_block": [{
            "block_type": 10000, "content": content,
            "block_id": str(uuid.uuid4()), "parent_id": "",
            "meta_info": [], "append_fields": [],
        }],
        "message_status": 0,
    }
    payload_messages = image_msgs + ([audio_msg] if audio_msg else []) + [text_msg]
    print(f"[参考] 参考图消息: {len(image_msgs)} 条, 音频: {'有' if audio_msg else '无'}")

    payload = {
        "client_meta": {
            "local_conversation_id": f"local_{int(time.time()*1e6)}{uuid.uuid4().hex[:8]}",
            "conversation_id": "", "bot_id": DOLA_BOT_ID,
            "last_section_id": "", "last_message_index": None,
        },
        "messages": list(payload_messages),
        "chat_ability": {"ability_type": 17, "ability_param": json.dumps(ability_param, ensure_ascii=False)},
        "user_context": [],
        "option": {
            "send_message_scene": "", "create_time_ms": now, "collect_id": "",
            "is_audio": False, "answer_with_suggest": False, "tts_switch": False,
            "need_deep_think": 0, "click_clear_context": False, "from_suggest": False,
            "is_regen": False, "is_replace": False, "is_from_click_option": False,
            "is_from_click_softlink": False, "disable_sse_cache": False,
            "select_text_action": "", "is_select_text": False, "resend_for_regen": False,
            "scene_type": 0, "unique_key": unique_key, "start_seq": 0,
            "need_create_conversation": True, "regen_query_id": [], "edit_query_id": [],
            "regen_instruction": "", "no_replace_for_regen": False, "message_from": 0,
            "shared_app_name": "", "shared_app_id": "", "is_ai_playground": False,
            "is_old_user": False, "message_storage_type": 0,
            "conversation_init_option": {"need_ack_conversation": True},
            "sse_recv_event_options": {"support_chunk_delta": True},
            "recovery_option": {"is_recovery": False, "req_create_time_sec": int(now/1000), "append_sse_event_scene": 0},
        },
        "ext": {
            "fp": _cookie_kv(cookie_header, "s_v_web_id"),
            "answer_with_suggest": "0", "sub_conv_firstmet_type": "1", "collection_id": "",
            "conversation_init_option": json.dumps({"need_ack_conversation": True}),
            "commerce_credit_config_enable": "0",
        },
    }

    headers = {
        "Accept": "*/*", "Accept-Encoding": "identity",
        "Accept-Language": _TASK_IDENTITY["accept_language"],
        "Cache-Control": "no-cache", "Pragma": "no-cache",
        "Origin": "https://www.dola.com", "Referer": "https://www.dola.com/chat/",
        "Sec-Ch-Ua": _TASK_IDENTITY["sec_ch_ua"],
        "Sec-Ch-Ua-Mobile": _TASK_IDENTITY["sec_ch_ua_mobile"], "Sec-Ch-Ua-Platform": _TASK_IDENTITY["sec_ch_ua_platform"],
        "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
        "User-Agent": _TASK_IDENTITY["user_agent"],
        "Content-Type": "application/json", "agw-js-conv": "str, str",
        "Cookie": cookie_header,
    }

    use_pc_url = True  # 风控修复：所有时长走PC客户端
    if use_pc_url:
        submit_url = build_pc_submit_url(30, cookie_header)
        headers["Accept"] = "text/event-stream"
        headers["agw-js-conv"] = "str"
    else:
        submit_url = build_submit_url(cookie_header)
    print(f"[提交] 模型: {model}, 时长: {duration}s, 比例: {ratio}, URL风格: {'PC客户端(30秒解锁)' if use_pc_url else '浏览器'}")

    max_attempts = int(_os.environ.get("DOLA_SUBMIT_RETRIES", "10"))
    risk_rejects = 0
    conversation_id = ""
    for attempt in range(1, max_attempts + 1):
        if attempt == 1:
            import random as _random
            _wait = _random.uniform(2, 8)
            print(f"[提交] 错峰等待 {_wait:.1f} 秒")
            time.sleep(_wait)
        if use_pc_url:
            submit_url = build_pc_submit_url(30, cookie_header)
        sess_proxies = _active_proxies()
        try:
            resp = requests.post(submit_url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers=headers, stream=True, timeout=120, proxies=sess_proxies)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError) as _ce:
            print(f"[提交] [警告] 出口节点{type(_ce).__name__}(坏IP/断连)，换新IP重试 {attempt}/{max_attempts}")
            sess_proxies = _rotate_proxies("connfail")
            time.sleep(3)
            continue
        except requests.exceptions.ProxyError as _pe:
            print(f"[提交] [警告] 代理握手失败，换新IP重试 {attempt}/{max_attempts}")
            sess_proxies = _rotate_proxies("proxyerr")
            time.sleep(3)
            continue
        print(f"[提交] 第{attempt}次 响应状态: {resp.status_code}")
        buf = b""
        for chunk in resp.iter_content(chunk_size=None):
            buf += chunk
            if len(buf) > 500000 or re.search(rb'"conversation_id\s*":\s*"\d{6,}"', buf):
                break
        try:
            resp.close()
        except Exception:
            pass
        full_text = buf.decode("utf-8", "replace")
        if resp.status_code != 200:
            print("[提交] 非200响应，15秒后重试")
            time.sleep(15)
            continue

        cid_found = ""
        sse_data = ""
        for line in full_text.splitlines():
            if not line:
                if sse_data:
                    try:
                        val = json.loads(sse_data)
                        def find_cid(obj, seen=None):
                            if seen is None: seen = set()
                            if not obj or id(obj) in seen: return ""
                            seen.add(id(obj))
                            if isinstance(obj, dict):
                                for k, v in obj.items():
                                    if re.match(r'^conversation_?id$', k, re.I) and re.match(r'^\d{6,}$', str(v)):
                                        return str(v)
                                    if isinstance(v, (dict, list)):
                                        r2 = find_cid(v, seen)
                                        if r2: return r2
                            elif isinstance(obj, list):
                                for item in obj:
                                    r2 = find_cid(item, seen)
                                    if r2: return r2
                            return ""
                        cid = find_cid(val)
                        if cid:
                            cid_found = cid
                            break
                    except Exception:
                        pass
                    sse_data = ""
            else:
                if line.startswith("data:"):
                    sse_data += line[5:].strip()
                elif '"conversation_id"' in line:
                    match = re.search(r'"conversation_id"\s*:\s*"(\d+)"', line)
                    if match:
                        cid_found = match.group(1)
                        break

        if cid_found:
            conversation_id = cid_found
            print(f"[提交] ✅ conversation_id: {conversation_id}")
            break

        ec_match_early = re.search(r'"error_code"\s*:\s*(\d+)', full_text)
        _is_rate_limit = (
            "710022002" in full_text or "710022004" in full_text
            or "使用量过大" in full_text or "访问频繁" in full_text
            or (ec_match_early and ec_match_early.group(1) in ("710022002","710022004"))
        )
        fatal_msg = ""
        fatal_patterns = [
            (r"无法使用\s*(Seedance|seedance)", "账号无法使用该视频模型，需开通或升级会员"),
            (r"开通或升级会员", "账号需开通或升级会员才能生成视频"),
            (r"会员(已)?过期", "账号会员已过期"),
            (r"视频生成.*(次数|额度).*(不足|用完)", "账号视频生成额度不足"),
            (r"账号.*被(封禁|限制|停用)", "账号被封禁或限制使用"),
            (r"登录状态.*(失效|过期)", "Cookie 登录状态已失效"),
            (r"login invalid", "Cookie 无效或已过期"),
            (r"712012001", "登录校验不合法（死号）"),
        ]
        for pat, msg in fatal_patterns:
            if re.search(pat, full_text):
                fatal_msg = msg
                break
        if not fatal_msg:
            m = re.search(r'"error_msg"\s*:\s*"([^"]{4,80})"', full_text)
            if m:
                fatal_msg = "豆包返回错误：" + m.group(1)
        # 风控修复：限流错误不当致命，继续重试换IP
        if _is_rate_limit:
            fatal_msg = ""
        # 710020702 system error(is_replay) 是豆包临时错误，不当致命，换IP重试
        if ec_match_early and ec_match_early.group(1) == "710020702":
            print("[提交] 710020702 system error(临时)，不致命，换IP重试")
            fatal_msg = ""
        if fatal_msg:
            print(f"[提交] ❌ 致命错误，停止重试：{fatal_msg}")
            print(f"[提交] 响应前500字符：{full_text[:500]}")
            return ""

        if audio_msg is not None and attempt >= 2:
            audio_msg = None
            payload["messages"] = list(image_msgs) + [text_msg]
            print("[提交] 连续失败，去掉参考音频重试")
        elif image_msgs and attempt >= 4:
            image_msgs = []
            payload["messages"] = [text_msg]
            print("[提交] 连续失败，去掉参考图重试")

        ec_match = re.search(r'"error_code"\s*:\s*(\d+)', full_text)
        if ec_match:
            print(f"[提交] 豆包错误码: {ec_match.group(1)}")
        if ("710022002" in full_text or "710022004" in full_text
                or "访问频繁" in full_text
                or (ec_match and ec_match.group(1) in ("710022002", "710022004"))):
            risk_rejects += 1
            if risk_rejects >= 3:
                # 已换IP重试2次仍限流 → 判定账号级风控，交给server换号
                print("[提交] ❌ 换新IP后仍连续被限流(710022)，判定账号级，停止重试")
                _mark_ip_bucket_consumed()
                return ""
            # 关键修复：限流先换新出口IP（当前IP可能被风控），再重试
            print(f"[提交] 限频（第{risk_rejects}次），先换新出口IP再重试")
            sess_proxies = _rotate_proxies("ratelimit")
            wait = min(45, 15 * risk_rejects)
        else:
            risk_rejects = 0
            wait = min(120, 15 * attempt)
            print(f"[提交] 未取到会话ID，响应前300字符: {full_text[:300]}")
        print(f"[提交] {wait}秒后重试...")
        time.sleep(wait)
    if not conversation_id:
        print("[提交] ❌ 多次尝试后仍未成功")
    return conversation_id

def extract_video_model(text):
    """从轮询响应中提取video_model"""
    vm_matches = re.findall(r'"video_model"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    for vm_str in vm_matches:
        try:
            vm_decoded = vm_str.replace("\\u002F", "/").replace("\\/", "/").replace('\\"', '"').replace("\\\\", "\\")
            return json.loads(vm_decoded)
        except:
            continue
    return None

def get_clean_video_url(video_model, cookie_header):
    """获取无水印视频URL（参考星空管理器完整流程）"""
    if not video_model:
        return None, "无video_model"

    # 1. 先找直接无水印URL
    video_list = video_model.get("video_list", {})
    for quality, vinfo in video_list.items():
        main_url = decode_media_value(vinfo.get("main_url", ""))
        if is_clean_url(main_url):
            print(f"  [去水印] ✅ 找到直接无水印URL ({quality})")
            return main_url, "直接无水印"

    # 2. 找fallback_api
    fallback_api = video_model.get("fallback_api", "")
    if not fallback_api:
        # 尝试从其他字段找
        for key in ["fallbackApi", "fallback_url", "origin_url"]:
            if key in video_model:
                fallback_api = video_model[key]
                break

    if not fallback_api:
        print("  [去水印] ⚠️  无fallback_api，使用有水印URL")
        # 返回第一个有水印URL
        for quality, vinfo in video_list.items():
            main_url = decode_media_value(vinfo.get("main_url", ""))
            if main_url:
                return main_url, f"有水印({quality})"
        return None, "无URL"

    # 3. 调用fallback_api（设置无水印参数）
    print(f"  [去水印] 调用fallback_api获取无水印...")
    clean_api_url = replace_fallback_params(fallback_api)
    print(f"  [去水印] API URL: {clean_api_url[:100]}")

    try:
        headers = {
            "User-Agent": _TASK_IDENTITY["user_agent"],
            "Accept": "application/json",
            "Referer": "https://www.dola.com/",
            "Cookie": cookie_header,
        }
        resp = requests.get(clean_api_url, headers=headers, timeout=30, proxies=_active_proxies())
        print(f"  [去水印] API响应状态: {resp.status_code}")

        if resp.status_code != 200:
            print(f"  [去水印] ⚠️  API请求失败，使用有水印URL")
            for quality, vinfo in video_list.items():
                main_url = decode_media_value(vinfo.get("main_url", ""))
                if main_url:
                    return main_url, f"有水印({quality})"
            return None, "API失败"

        data = resp.json()

        # 提取video_info
        video_info = data.get("video_info", data.get("data", data))
        if isinstance(video_info, dict) and "data" in video_info:
            video_info = video_info["data"]

        # 提取key_seed
        key_seed = video_info.get("key_seed", video_model.get("key_seed", ""))
        print(f"  [去水印] key_seed: {key_seed[:20] if key_seed else '空'}")

        # 从video_list找最佳质量
        fb_video_list = video_info.get("video_list", {})
        if not fb_video_list and isinstance(video_info, dict):
            # 递归找video_list
            def find_vl(obj, depth=0):
                if depth > 5 or not isinstance(obj, dict):
                    return None
                if "video_list" in obj and isinstance(obj["video_list"], dict):
                    return obj["video_list"]
                for v in obj.values():
                    if isinstance(v, dict):
                        result = find_vl(v, depth+1)
                        if result:
                            return result
                return None
            fb_video_list = find_vl(video_info)

        best_token = ""
        best_quality = ""
        if fb_video_list:
            # 按分辨率排序，选最高质量
            for quality, vinfo in fb_video_list.items():
                token = vinfo.get("main_url", vinfo.get("man_url", ""))
                if token:
                    # 优先选original或最高分辨率
                    if "original" in quality.lower() or "origin" in quality.lower():
                        best_token = token
                        best_quality = quality
                        break
                    if not best_token:
                        best_token = token
                        best_quality = quality

        if not best_token:
            print("  [去水印] ⚠️  fallback无token，使用有水印URL")
            for quality, vinfo in video_list.items():
                main_url = decode_media_value(vinfo.get("main_url", ""))
                if main_url:
                    return main_url, f"有水印({quality})"
            return None, "无token"

        print(f"  [去水印] 最佳质量: {best_quality}")

        # 4. 解码URL（先试base64，再试QAAB解密）
        decoded = decode_media_value(best_token)
        if is_clean_url(decoded):
            print(f"  [去水印] ✅ base64解码成功，无水印")
            return decoded, "fallback无水印(base64)"

        # QAAB解密
        if key_seed:
            print(f"  [去水印] 尝试QAAB解密...")
            qaab_url = decode_qaab_token(best_token, key_seed)
            if qaab_url and is_clean_url(qaab_url):
                print(f"  [去水印] ✅ QAAB解密成功，无水印")
                return qaab_url, "fallback无水印(QAAB)"
            elif qaab_url:
                print(f"  [去水印] ⚠️  QAAB解密成功但非无水印URL")
                return qaab_url, "fallback有水印(QAAB)"

        # 如果都失败，返回解码后的URL（可能有水印）
        if decoded:
            print(f"  [去水印] ⚠️  使用解码后的URL（可能有水印）")
            return decoded, "fallback解码"

        return None, "全部失败"

    except Exception as e:
        print(f"  [去水印] ❌ fallback_api异常: {e}")
        # 返回第一个有水印URL
        for quality, vinfo in video_list.items():
            main_url = decode_media_value(vinfo.get("main_url", ""))
            if main_url:
                return main_url, f"有水印({quality})"
        return None, f"异常: {e}"

def send_confirm_reply(conversation_id, cookie_header, ratio="9:16", duration=30, model=""):
    """豆包新交互：提交后机器人先征求确认，此函数在同一会话回复确认触发真正生成"""
    try:
        now = int(time.time() * 1000)
        content = {"text_block": {"text": "确认，请直接生成", "icon_url": "", "icon_url_dark": "", "summary": ""}, "pc_event_block": ""}
        text_msg = {
            "local_message_id": str(uuid.uuid4()),
            "content_block": [{
                "block_type": 10000, "content": content,
                "block_id": str(uuid.uuid4()), "parent_id": "",
                "meta_info": [], "append_fields": [],
            }],
            "message_status": 0,
        }
        confirm_model = model if model in ("seedance_v2.0", "seedance_v2.5") else ("seedance_v2.0" if int(duration) == 15 else "seedance_v2.5")
        ability_param = {"ratio": ratio, "model": confirm_model, "duration": int(duration), "camera_movement": "fixed"}
        payload = {
            "client_meta": {
                "local_conversation_id": "local_%d%s" % (int(time.time() * 1e6), uuid.uuid4().hex[:8]),
                "conversation_id": conversation_id, "bot_id": DOLA_BOT_ID,
                "last_section_id": "", "last_message_index": None,
            },
            "messages": [text_msg],
            "chat_ability": {"ability_type": 17, "ability_param": json.dumps(ability_param, ensure_ascii=False)},
            "user_context": [],
            "option": {
                "send_message_scene": "", "create_time_ms": now, "collect_id": "",
                "is_audio": False, "answer_with_suggest": False, "tts_switch": False,
                "need_deep_think": 0, "click_clear_context": False, "from_suggest": False,
                "is_regen": False, "is_replace": False, "is_from_click_option": False,
                "is_from_click_softlink": False, "disable_sse_cache": False,
                "select_text_action": "", "is_select_text": False, "resend_for_regen": False,
                "scene_type": 0, "unique_key": uuid.uuid4().hex, "start_seq": 0,
                "need_create_conversation": False, "regen_query_id": [], "edit_query_id": [],
                "regen_instruction": "", "no_replace_for_regen": False, "message_from": 0,
                "shared_app_name": "", "shared_app_id": "", "is_ai_playground": False,
                "is_old_user": False, "message_storage_type": 0,
                "conversation_init_option": {"need_ack_conversation": True},
                "sse_recv_event_options": {"support_chunk_delta": True},
                "recovery_option": {"is_recovery": False, "req_create_time_sec": int(now / 1000), "append_sse_event_scene": 0},
            },
            "ext": {
                "fp": "", "answer_with_suggest": "0", "sub_conv_firstmet_type": "1", "collection_id": "",
                "conversation_init_option": json.dumps({"need_ack_conversation": True}),
                "commerce_credit_config_enable": "0",
            },
        }
        headers = {
            "Accept": "text/event-stream", "Accept-Encoding": "identity",
            "Accept-Language": _TASK_IDENTITY["accept_language"],
            "Cache-Control": "no-cache", "Pragma": "no-cache",
            "Origin": "https://www.dola.com", "Referer": "https://www.dola.com/chat/",
            "User-Agent": _TASK_IDENTITY["user_agent"],
            "Content-Type": "application/json", "agw-js-conv": "str",
            "Cookie": cookie_header,
        }
        # 风控修复：确认回复所有时长都走PC客户端URL
        url = build_pc_submit_url(30, cookie_header)
        resp = requests.post(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                             headers=headers, stream=True, timeout=120, proxies=_active_proxies())
        buf = b""
        for chunk in resp.iter_content(chunk_size=None):
            buf += chunk
            if len(buf) > 200000:
                break
        try:
            resp.close()
        except Exception:
            pass
        sse = buf.decode("utf-8", "replace")
        if "STREAM_ERROR" in sse:
            print(f"[确认] ❌ 确认回复被拒: {sse[:300]}")
            return False
        started = ("正在创作" in sse) or ("预计等待" in sse)
        print(f"[确认] ✅ 已回复确认，豆包应答: {'已开始生成' if started else sse[:150]}")
        return True
    except Exception as e:
        print(f"[确认] ❌ 回复异常: {e}")
        return False


def poll_and_download(conversation_id, cookie_header, output_dir, name="video", ratio="9:16", duration=30, model=""):
    poll_url = build_poll_url(cookie_header)
    headers = {
        "User-Agent": _TASK_IDENTITY["user_agent"],
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": _TASK_IDENTITY["accept_language"],
        "Content-Type": "application/json; encoding=utf-8",
        "agw-js-conv": "str",
        "Origin": "https://www.dola.com",
        "Referer": f"https://www.dola.com/chat/{conversation_id}",
        "Cookie": cookie_header,
    }

    print(f"\n[轮询] 开始轮询（最多44分钟，含确认交互）...")
    max_wait = 44 * 60
    start = time.time()
    _stall_last_len = -1
    _stall_change_at = time.time()
    _confirm_sent = False
    _gen_started = False

    while time.time() - start < max_wait:
        elapsed = int(time.time() - start)
        try:
            body = {
                "cmd": 3100,
                "uplink_body": {"pull_singe_chain_uplink_body": {
                    "conversation_id": conversation_id,
                    "anchor_index": 9007199254740991,
                    "conversation_type": 3, "direction": 1, "limit": 20,
                    "ext": {}, "filter": {"index_list": []},
                    "evaluate_ab_params": "", "evaluate_common_params": "",
                }},
                "sequence_id": str(uuid.uuid4()), "channel": 2, "version": "1",
            }
            resp = requests.post(poll_url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), proxies=_active_proxies(),
                                 headers=headers, timeout=30)
            text = resp.text

            if (not _confirm_sent) and ("确认后我直接生成" in text or "确认后直接生成" in text or "确认后我将开始" in text):
                print("[轮询] 检测到豆包等待确认（新交互流程），自动回复确认...")
                send_confirm_reply(conversation_id, cookie_header, ratio=ratio, duration=duration, model=model)
                _confirm_sent = True
                _stall_last_len = -1
                _stall_change_at = time.time()
                time.sleep(5)
                continue
            if ("正在创作" in text) or ("预计等待" in text):
                _gen_started = True

            _stall_limit = 1500 if (_confirm_sent or _gen_started) else 600
            if len(text) != _stall_last_len:
                _stall_last_len = len(text)
                _stall_change_at = time.time()
            elif elapsed > 600 and time.time() - _stall_change_at > _stall_limit:
                print(f"[轮询] ❌ 响应长度卡死超过{_stall_limit//60}分钟({len(text)})，判定生成已被豆包终止")
                return None

            if "不支持编码类型" in text:
                if elapsed % 60 == 0:
                    print(f"[轮询] API错误: {text[:200]}")
                time.sleep(5)
                continue

            if "你的视频生成好了" in text or "video_model" in text:
                print(f"[轮询] ✅ 视频已生成！")
                vid_match = re.search(r'"vid"\s*:\s*"([^"]+)"', text)
                vid = vid_match.group(1) if vid_match else "unknown"
                print(f"[轮询] vid: {vid}")

                # 提取video_model
                video_model = extract_video_model(text)
                if video_model:
                    print(f"[轮询] video_model提取成功")
                else:
                    print(f"[轮询] ⚠️  video_model提取失败，尝试正则提取URL")

                # 获取无水印URL
                video_url, clean_type = get_clean_video_url(video_model, cookie_header)
                print(f"[轮询] 视频来源: {clean_type}")
                print(f"[轮询] URL: {video_url[:100] if video_url else '空'}")

                # 如果没找到，用正则
                if not video_url:
                    urls = re.findall(r'https?://[^"\'\s\\<>]+\.(?:mp4|mov|webm)[^"\'\s\\<>]*', text)
                    if urls:
                        video_url = urls[-1]
                        print(f"[轮询] 从正则找到URL: {video_url[:100]}")

                if video_url:
                    os.makedirs(output_dir, exist_ok=True)
                    timestamp = time.strftime("%Y%m%d_%H%M%S")
                    clean_tag = "无水印" if "无水印" in clean_type else "有水印"
                    filename = f"{name}_{clean_tag}_sd25_30s_{timestamp}.mp4"
                    output_path = os.path.join(output_dir, filename)

                    print(f"[下载] 下载中...")
                    resp_dl = requests.get(video_url, timeout=300, stream=True, proxies=_active_proxies())
                    if resp_dl.status_code == 200:
                        with open(output_path, "wb") as f:
                            for chunk in resp_dl.iter_content(chunk_size=65536):
                                f.write(chunk)
                        size = os.path.getsize(output_path) / (1024*1024)
                        print(f"[下载] ✅ 成功: {output_path} ({size:.2f} MB)")
                        _mark_ip_bucket_consumed()
                        print(f"[下载] 视频类型: {clean_type}")
                        return output_path
                    else:
                        print(f"[下载] ❌ 失败: HTTP {resp_dl.status_code}")
                else:
                    print(f"[轮询] ❌ 未找到视频URL")
                break

            if elapsed % 60 == 0:
                print(f"[轮询] 等待中... {elapsed//60}分{elapsed%60}秒 (响应长度: {len(text)})")

        except Exception as e:
            if elapsed % 60 == 0:
                print(f"[轮询] 异常: {e}")

        time.sleep(15)

    print(f"[轮询] ❌ 超时")
    return None

def main():
    if len(sys.argv) < 5:
        print("用法: python3 -u dola_gen_clean.py <cookie_json> <输出目录> <提示词> <名称>")
        sys.exit(1)

    cookie_json = sys.argv[1]
    output_dir = sys.argv[2]
    prompt = sys.argv[3]
    name = sys.argv[4]
    ratio = sys.argv[5] if len(sys.argv) > 5 else "9:16"
    duration = int(sys.argv[6]) if len(sys.argv) > 6 else 30
    image_paths = []
    for p in (sys.argv[7].split(",") if len(sys.argv) > 7 else []):
        p = p.strip()
        if p and os.path.exists(p):
            image_paths.append(p)
        elif p:
            print(f"[参考图] 文件不存在，跳过: {p}")
    audio_path = sys.argv[8].strip() if len(sys.argv) > 8 else ""
    model_arg = sys.argv[9].strip() if len(sys.argv) > 9 else ""
    if audio_path and not os.path.exists(audio_path):
        print(f"[参考音频] 文件不存在，跳过: {audio_path}")
        audio_path = ""

    with open(cookie_json, "r", encoding="utf-8") as f:
        data = json.load(f)
    cookie_header = data["cookie_header"]

    print("=" * 70)
    print(f"Dola SD2.5 30秒视频生成（完整去水印版）")
    print(f"名称: {name}")
    print("=" * 70)
    print(f"cookie长度: {len(cookie_header)}")

    try:
        ip_resp = requests.get("https://api.ipify.org?format=json", timeout=10)
        print(f"VPS IP: {ip_resp.json()}")
    except:
        pass

    dola_model = model_arg if model_arg in ("seedance_v2.0", "seedance_v2.5") else ("seedance_v2.0" if int(duration) == 15 else "seedance_v2.5")
    # STICKY_BY_MODEL_DONE: SD2.5(30s) 35min / SD2.0 15min
    globals()["CLIPROXY_STICKY_MIN"] = 45

    conversation_id = submit_video(prompt, ratio, duration, dola_model, cookie_header,
                                   image_paths=image_paths, audio_path=audio_path)

    if not conversation_id:
        print("\n❌ 提交失败")
        _mark_ip_bucket_consumed()  # 标记当前IP已消耗，换号新进程拿新IP
        sys.exit(1)

    output_path = poll_and_download(conversation_id, cookie_header, output_dir, name, ratio=ratio, duration=duration, model=dola_model)

    if output_path:
        print(f"\n✅ 完成！视频已保存到: {output_path}")
    else:
        print(f"\n❌ 生成失败")

if __name__ == "__main__":
    main()
