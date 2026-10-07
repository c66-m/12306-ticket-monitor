# -*- coding: utf-8 -*-
"""
乘车人信息管理模块：添加 / 编辑 / 删除 / 设置默认乘车人，数据加密存储。

加密方案（按环境自动选择，优先生成可用的最安全方式）
    1. Windows：使用系统 DPAPI（CryptProtectData）按当前用户加密，密钥由系统托管，无需额外依赖
    2. 安装了 cryptography：使用 Fernet + 本地密钥文件（首次运行自动生成）
    3. 都不满足：明文存储（会打印醒目警告，仅建议在可信的私人机器上使用）

存储文件
    passengers.json   —— 结构 {"version":1,"enc":"dpapi|fernet|none","data":"base64 密文"}

数据字段（每位乘车人）
    name          姓名
    id_type_code  证件类型代码（"1"=二代身份证, "C"=港澳通行证, "G"=台湾通行证, "B"=护照）
    id_no         证件号码（加密存储）
    mobile        手机号（加密存储）
    is_default    是否默认乘车人（自动购票时优先使用）
    is_adult      是否成人（False 为儿童/学生等）

用途
    1. 交互菜单里维护常用乘车人 / 乘车人组合
    2. 自动下单时优先使用"默认乘车人"（见 order.py 的 select_passengers 调用方）
    3. 实际下单仍以 12306 账号内已保存的乘车人数据为准（本模块负责"选谁"的偏好）

注意
    - 隐私数据只在本地加密保存，任何情况下都不会上传到第三方
"""

import base64
import json

import appcommon
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, "passengers.json")

ID_TYPE_NAMES = {
    "1": "二代身份证",
    "2": "一代身份证",
    "C": "港澳通行证",
    "G": "台湾通行证",
    "B": "护照",
    "H": "外国人永久居留证",
    "L": "其他",
}
ID_TYPE_CODES = {v: k for k, v in ID_TYPE_NAMES.items()}


# ----------------------------- 加密底层 -----------------------------

def _is_windows():
    return sys.platform == "win32"


def _dpapi_protect(data):
    """Windows DPAPI 加密。data: bytes -> bytes"""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    def _blob(raw):
        buf = ctypes.create_string_buffer(raw)
        st = DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        # DATA_BLOB 只存 pbData 的裸地址，不持有 buffer 对象；若此处不锚定，
        # _blob 返回后 buf 引用计数归零被释放，pbData 即成悬垂指针，
        # 后续 CryptProtectData 将从已释放内存读取（use-after-free，未定义行为）。
        # 把 buffer 挂在 struct 上，生命周期随 data_in 延至 API 调用结束之后。
        st._buf = buf
        return st

    data_in = _blob(data)
    data_out = DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(data_in), u"12306-passengers", None, None, None, 0x01, ctypes.byref(data_out))
    if not ok:
        raise RuntimeError("DPAPI 加密失败（GetLastError=%s）" % ctypes.get_last_error())
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData)
    finally:
        kernel32.LocalFree(data_out.pbData)


def _dpapi_unprotect(blob):
    """Windows DPAPI 解密。blob: bytes -> bytes"""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    def _blob(raw):
        buf = ctypes.create_string_buffer(raw)
        st = DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        # 同 _dpapi_protect：锚定 backing buffer，防止 pbData 悬垂
        # （use-after-free，未定义行为）。
        st._buf = buf
        return st

    data_in = _blob(blob)
    data_out = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(data_in), None, None, None, None, 0x01, ctypes.byref(data_out))
    if not ok:
        raise RuntimeError("DPAPI 解密失败（GetLastError=%s，数据可能属于其他 Windows 用户）" % ctypes.get_last_error())
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData)
    finally:
        kernel32.LocalFree(data_out.pbData)


def _get_fernet():
    """返回 (fernet 实例, key_path)。cryptography 未安装时返回 (None, None)。"""
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None, None

    key_path = os.path.join(HERE, ".passengers_key")
    if not os.path.exists(key_path):
        with open(key_path, "wb") as f:
            f.write(Fernet.generate_key())
        try:
            os.chmod(key_path, 0o600)
        except Exception:
            pass
    with open(key_path, "rb") as f:
        key = f.read()
    return Fernet(key), key_path


# ----------------------------- 读写封装 -----------------------------

def _encrypt(payload_text):
    """把明文 JSON 文本加密。返回 (enc_name, data_text)。data_text 为 base64 或明文。"""
    raw = payload_text.encode("utf-8")
    if _is_windows():
        try:
            return "dpapi", base64.b64encode(_dpapi_protect(raw)).decode("ascii")
        except Exception as e:
            print("[警告] DPAPI 加密失败（%s），尝试其他方式" % e)
    fernet, key_path = _get_fernet()
    if fernet is not None:
        return "fernet", fernet.encrypt(raw).decode("ascii")
    print("[警告] 未找到可用加密组件（非 Windows 且未安装 cryptography）。")
    print("        乘车人信息将以【明文】保存到 passengers.json，请勿在公共电脑上使用。")
    return "none", payload_text


def _decrypt(enc_name, data_text):
    if enc_name == "dpapi":
        return _dpapi_unprotect(base64.b64decode(data_text)).decode("utf-8")
    if enc_name == "fernet":
        fernet, _ = _get_fernet()
        if fernet is None:
            raise RuntimeError("密文由 Fernet 加密，但当前环境未安装 cryptography，无法解密")
        return fernet.decrypt(data_text.encode("ascii")).decode("utf-8")
    return data_text


def load_passengers(path=None):
    """读取并解密乘车人列表。文件不存在或损坏时返回 []。"""
    path = path or DEFAULT_PATH
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            box = json.load(f)
    except Exception as e:
        print("[警告] 乘车人文件 %s 读取失败：%s" % (path, e))
        return []
    try:
        text = _decrypt(box.get("enc", "none"), box.get("data", "[]"))
        data = json.loads(text)
        if isinstance(data, list):
            return data
        return data.get("passengers", []) if isinstance(data, dict) else []
    except Exception as e:
        print("[警告] 乘车人文件 %s 解密失败：%s" % (path, e))
        return []


def save_passengers(passengers, path=None):
    """加密保存乘车人列表。"""
    path = path or DEFAULT_PATH
    payload = json.dumps({"passengers": list(passengers)}, ensure_ascii=False, indent=2)
    enc_name, data_text = _encrypt(payload)
    box = {"version": 1, "enc": enc_name, "data": data_text}
    # 原子写：写一半被杀不留半截密文（密文损坏 = 乘车人数据全丢）
    appcommon.atomic_write_json(path, box)


# ----------------------------- 业务辅助 -----------------------------

def default_names(path=None):
    """返回默认乘车人姓名列表（用于自动下单优先选择）。"""
    passengers = load_passengers(path)
    names = [p.get("name") for p in passengers
             if p.get("is_default") and p.get("name")]
    if names:
        return names
    return [p.get("name") for p in passengers if p.get("name")][:1]


def find_by_name(name, passengers):
    for p in passengers:
        if p.get("name") == name:
            return p
    return None


if __name__ == "__main__":
    # 自检：读写一个临时测试数据，验证加解密链路
    tmp = os.path.join(HERE, "passengers_test_tmp.json")
    test = [{"name": "测试", "id_type_code": "1", "id_no": "110101199001011234",
             "mobile": "13800000000", "is_default": True, "is_adult": True}]
    save_passengers(test, tmp)
    back = load_passengers(tmp)
    w = back[0] if back else None
    if w and w.get("id_no") == "110101199001011234":
        print("[自检通过] 加密存储链路正常，共 %d 条" % len(back))
    else:
        print("[自检失败] 读回数据不一致：%r" % back)
    try:
        os.remove(tmp)
    except OSError:
        pass