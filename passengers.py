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
import logging
import time

import appcommon
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

LOG = logging.getLogger("monitor")

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


# ----------------------------- 公开 secrets API -----------------------------
# Task 60: 统一的敏感串（SMTP 授权码等）加解密公开接口。
# notify.py / gui.py / 测试一律调这里，不再直调私有的 _dpapi_*。

_DPAPI_PREFIX = "dpapi1:"


def _looks_like_encrypted_blob(text):
    """启发式：text 是否像 protect_secret 加密分支产出的值。

    加密分支只产出 "dpapi1:" + 标准 base64（b64encode），故哨兵后为
    严格 base64 时视为已加密（原样返回，避免二次包裹）。
    字面以哨兵开头、但其后非 base64 的，不可能是加密输出 → 一定是
    与哨兵字面冲突的明文（Task 80d），走加密分支把它真正加密，
    unprotect 解密后可完整还原。
    残留（已文档化，概率可忽略）：字面 "dpapi1:"+严格 base64 的明文
    仍会被当成密文；其读取失败走 SecretDecryptError 诚实报错，不静默错密。
    """
    if not text.startswith(_DPAPI_PREFIX):
        return False
    rest = text[len(_DPAPI_PREFIX):]
    if not rest:
        return False
    try:
        base64.b64decode(rest.encode("ascii"), validate=True)
        return True
    except Exception:
        return False


class SecretDecryptError(Exception):
    """敏感串解密失败：数据损坏，或密文属于其他 Windows 用户。"""


def protect_secret(text):
    """公开 API：敏感串用 Windows DPAPI 加密后落盘。

    返回带 "dpapi1:" 前缀的密文；非 Windows / 加密失败时原样返回明文
    （保持向后兼容）并记 error 日志显式告警——不再静默降级。
    已加密的原样返回，避免二次包裹（Task 80d：用"哨兵+严格 base64"
    的结构启发式识别已加密值；字面冲突的明文会被真正加密，可还原）。
    """
    if not text or _looks_like_encrypted_blob(text):
        return text
    try:
        blob = _dpapi_protect(text.encode("utf-8"))
        return _DPAPI_PREFIX + base64.b64encode(blob).decode("ascii")
    except Exception as e:
        LOG.error("[安全] 敏感串加密失败，已回退明文保存"
                  "（仅建议在可信的私人机器上使用）: %s", e)
        return text


def unprotect_secret(text):
    """公开 API：取回敏感串明文。带 dpapi1: 前缀则解密；旧明文原样返回。

    解密失败抛 SecretDecryptError（不再吞成空串，避免上层拿空密码
    去登录而误报 535 认证失败）。"""
    if text and text.startswith(_DPAPI_PREFIX):
        try:
            return _dpapi_unprotect(
                base64.b64decode(text[len(_DPAPI_PREFIX):])).decode("utf-8")
        except Exception as e:
            raise SecretDecryptError(
                "敏感串解密失败（数据可能损坏或属于其他 Windows 用户）: %s" % e)
    return text or ""


def _fernet_key_path():
    return os.path.join(HERE, ".passengers_key")


def _read_valid_fernet_key(key_path):
    """读取并校验 Fernet 密钥。缺失抛 FileNotFoundError，非法抛 ValueError。

    末尾空白（换行符等）会被 strip——合法密钥本身不含空白，strip 只救
    "编辑器顺手加了换行"这类小损坏，不会把真损坏洗成合法。"""
    from cryptography.fernet import Fernet
    with open(key_path, "rb") as f:
        key = f.read().strip()
    Fernet(key)  # 非法密钥（半截/损坏）抛 ValueError
    return key


def _read_key_with_retry(key_path, tries=60, interval=0.02):
    """读回已存在的密钥；ValueError（半截/损坏）短暂重试，缺失直接抛。

    防"并发写一半时的半截读"：写方（O_EXCL 创建后写 44 字节）是微秒级，
    重试只为跨过这个窗口；稳定损坏由调用方走自愈，不在这里误判。"""
    last = None
    for _ in range(tries):
        try:
            return _read_valid_fernet_key(key_path)
        except FileNotFoundError:
            raise
        except ValueError as e:
            last = e
            time.sleep(interval)
    raise last


def _generate_fernet_key(key_path):
    """O_EXCL 原子创建密钥文件；返回 key bytes。

    双首跑竞态：恰好一个胜者创建成功；败者（FileExistsError）绝不覆盖，
    直接读回胜者的密钥（带校验重试，防读到胜者写一半的半截文件）。
    """
    from cryptography.fernet import Fernet
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_key_with_retry(key_path)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(Fernet.generate_key())
    except BaseException:
        # 创建后写失败：删掉半截文件，避免后人读到坏密钥
        try:
            os.unlink(key_path)
        except OSError:
            pass
        raise
    try:
        os.chmod(key_path, 0o600)
    except Exception:
        pass
    return _read_valid_fernet_key(key_path)


def _heal_corrupt_fernet_key(key_path):
    """损坏密钥自愈：备份旧文件 → 重新生成 → 明确日志。

    数据代价（必须明确）：旧密钥丢失后，此前用它加密的数据
    （乘车人证件号/手机号）将永久不可解密——备份仅留证，无法恢复旧数据。
    自愈只恢复"继续可用"，不恢复旧数据。"""
    bad = "%s.bad-%s-%d" % (key_path, time.strftime("%Y%m%d-%H%M%S"), os.getpid())
    try:
        os.replace(key_path, bad)
    except OSError as e:
        raise RuntimeError("Fernet 密钥文件损坏且无法备份（%s），拒绝重建：%s"
                           % (key_path, e))
    LOG.error("[安全] Fernet 密钥文件损坏，已备份为 %s 并重新生成；"
              "此前用旧密钥加密的数据将不可解密", bad)
    return _generate_fernet_key(key_path)


def _get_fernet(create=False):
    """返回 (fernet 实例, key_path)。

    create=True（加密路径）：缺失则 O_EXCL 原子生成；损坏则备份+重建自愈。
    create=False（解密路径）：缺失/损坏一律抛明确异常，绝不生成——
        生成会掩盖"密钥丢失"，让用户拿到误导性的解密失败。
    cryptography 未安装 → (None, None)（调用方按旧语义处理）。"""
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None, None

    key_path = _fernet_key_path()
    if create:
        try:
            key = _read_key_with_retry(key_path)
        except FileNotFoundError:
            key = _generate_fernet_key(key_path)
        except ValueError:
            key = _heal_corrupt_fernet_key(key_path)
    else:
        try:
            key = _read_valid_fernet_key(key_path)
        except FileNotFoundError:
            raise RuntimeError("Fernet 密钥文件缺失（%s），无法解密已加密数据；"
                               "密钥丢失请从备份恢复" % key_path)
        except ValueError as e:
            raise RuntimeError("Fernet 密钥文件损坏（%s），无法解密：%s"
                               % (key_path, e))
    return Fernet(key), key_path


# ----------------------------- 读写封装 -----------------------------

def _encrypt(payload_text):
    """把明文 JSON 文本加密。返回 (enc_name, data_text)。data_text 为 base64 或明文。"""
    raw = payload_text.encode("utf-8")
    if _is_windows():
        try:
            return "dpapi", base64.b64encode(_dpapi_protect(raw)).decode("ascii")
        except Exception as e:
            # Task 63(a)：DPAPI 降级 Fernet 不再静默——记 error 明确告知。
            # Fernet 是文件密钥，不具备 DPAPI 的"绑定当前 Windows 用户"
            # 特性，弱于用户预期，降级必须让用户感知。
            LOG.error("[安全] DPAPI 加密失败，已降级为 Fernet 本地密钥加密"
                      "（弱于用户绑定的 DPAPI，请检查 Windows 用户/权限）: %s", e)
    fernet, key_path = _get_fernet(create=True)
    if fernet is not None:
        return "fernet", fernet.encrypt(raw).decode("ascii")
    # Task 80(c)：明文回退不再 print（Task 63 reviewer 已确认 print 在 GUI 下
    # 直接消失）——改记 error 日志（三路可见：控制台/文件/GUI 日志面板），
    # 明确告知用户"未加密保存"。
    LOG.error("[安全] 未找到可用加密组件（非 Windows 且未安装 cryptography）。"
              "乘车人信息将以【明文】保存，请勿在公共电脑上使用。")
    return "none", payload_text


def _decrypt(enc_name, data_text):
    if enc_name == "dpapi":
        return _dpapi_unprotect(base64.b64decode(data_text)).decode("utf-8")
    if enc_name == "fernet":
        # Task 63(b)：解密路径绝不触发密钥"生成"——缺密钥即明确报错。
        # 旧代码在此生成新密钥，只会把"密钥丢失"掩盖成解密失败。
        fernet, _ = _get_fernet(create=False)
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


def _is_undecryptable_box(path):
    """磁盘盒子在本环境是否不可解密（拒写守卫用）。

    文件存在、非空，但解密失败（dpapi 盒子拷到别的机器/用户、
    fernet 密钥丢失等）→ True。文件不存在/为空/可解密 → False。
    注意：盒子信封 JSON 本身损坏也判 True——同样无法证明可恢复，
    宁可拒写（可用 force=True 显式覆盖）。
    """
    try:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return False
        with open(path, "r", encoding="utf-8") as f:
            box = json.load(f)
        _decrypt(box.get("enc", "none"), box.get("data", "[]"))
        return False
    except Exception:
        return True


def save_passengers(passengers, path=None, force=False):
    """加密保存乘车人列表。成功返回 True。

    若磁盘上已有文件且在本环境不可解密（dpapi 盒子拷到 Linux /
    换了 Windows 用户），默认拒绝覆写——否则源机器可恢复的密文会被
    永久销毁。确需放弃旧数据时传 force=True（对应 --force）。
    """
    path = path or DEFAULT_PATH
    if not force and _is_undecryptable_box(path):
        LOG.error("[安全] 拒绝覆盖不可解密的 passengers 数据（%s），"
                  "请在原机器解密后迁移；如确认放弃请用 --force", path)
        return False
    payload = json.dumps({"passengers": list(passengers)}, ensure_ascii=False, indent=2)
    enc_name, data_text = _encrypt(payload)
    box = {"version": 1, "enc": enc_name, "data": data_text}
    # 原子写：写一半被杀不留半截密文（密文损坏 = 乘车人数据全丢）
    appcommon.atomic_write_json(path, box)
    return True


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