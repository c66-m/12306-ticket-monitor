# -*- coding: utf-8 -*-
"""
行程身份 TripKey（core/domain，仅依赖标准库）。

全系统判定链路的身份坐标：任何「这个行程是否已购 / 是否重复」的判定，都
必须先有 TripKey，再拿它去比对官方快照与台账。比现有「(date|train) + 名字
字符串包含」的零散匹配强在哪：

- 乘车人集合从「字符串包含」收紧为「全部目标乘车人命中订单乘车人」，
  杜绝同车次部分重叠的他人订单误归因（旧 find_recent_order 的坑）；
- 空乘车人集合在构造期直接抛异常——从根上删掉旧代码「空集恒不命中 →
  误判官方无此订单 → 无限重试」的路径；
- 指纹基于乘车人 token（姓名或证件尾4）排序后哈希，任何排序无关的判定
  都确定可复现。
"""

import dataclasses
import hashlib
import re

# 姓名规范化：去首尾空白、压缩中间连续空白（中文姓名常见全角空格）
_SPACE_RE = re.compile(r"\s+")


def normalize_name(name):
    """乘车人姓名规范化：strip + 压缩空白。空输入返回空串。"""
    if name is None:
        return ""
    return _SPACE_RE.sub("", str(name).strip())


def pax_fingerprint(tokens):
    """乘车人指纹（核心函数）。

    tokens: 乘车人身份 token 序列（姓名 或 "姓名/证件尾4"）。排序后哈希
    前 12 位 hex。空集抛 ValueError——不允许无身份的行程判定。
    """
    cleaned = sorted(normalize_name(t) for t in (tokens or []))
    cleaned = [t for t in cleaned if t]
    if not cleaned:
        raise ValueError("乘车人集合为空，无法构造行程身份指纹")
    digest = hashlib.sha1("|".join(cleaned).encode("utf-8")).hexdigest()
    return digest[:12]


def passenger_id_token(name, id_no=None):
    """构造乘车人身份 token：有证件号则用 "姓名/尾4"，否则仅姓名。

    证件尾4是比姓名更强的区分度（同名同行程不同人）。id_no 传完整证件号，
    内部自取尾 4 位——不在 token 里留下完整证件号（日志脱敏友好）。
    """
    nm = normalize_name(name)
    if not nm:
        return ""
    if id_no:
        tail = str(id_no)[-4:]
        if tail:
            return "%s/%s" % (nm, tail)
    return nm


@dataclasses.dataclass(frozen=True)
class TripKey:
    """行程身份：乘车日期 + 车次 + 乘车人指纹。全部字段规范化后存储。"""
    date: str          # 乘车日期 YYYY-MM-DD
    train: str         # 车次（大写、去空格）
    pax_key: str       # pax_fingerprint 结果（12 位 hex）

    def __post_init__(self):
        # frozen dataclass 里做规范化，用 object.__setattr__
        object.__setattr__(self, "date", (self.date or "").strip())
        object.__setattr__(self, "train",
                           re.sub(r"\s+", "", (self.train or "").upper()))
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", self.date):
            raise ValueError("乘车日期应为 YYYY-MM-DD: %r" % (self.date,))
        if not self.train:
            raise ValueError("车次不能为空")
        if not re.match(r"^[0-9a-f]{12}$", self.pax_key):
            raise ValueError("乘车人指纹非法: %r" % (self.pax_key,))

    def composite(self):
        """台账/日志的复合键。"""
        return "%s|%s|%s" % (self.date, self.train, self.pax_key)

    @classmethod
    def from_tokens(cls, date, train, pax_tokens):
        """pax_tokens: 身份 token 序列（见 pax_fingerprint）。"""
        return cls(date=date, train=train,
                   pax_key=pax_fingerprint(pax_tokens))

    @classmethod
    def from_task(cls, date, train, passenger_names):
        """直接从任务的 passenger_names 构造（下单链路最常见的入口）。
        空乘车人列表在这里直接抛 ValueError，拒绝进判定。"""
        return cls.from_tokens(date, train, passenger_names)

    def match_trip_fields(self, order_train, order_date):
        """车次/日期两个身份字段的精确匹配（不含乘车人）。"""
        return (re.sub(r"\s+", "", (order_train or "").upper()) == self.train
                and (order_date or "")[:10] == self.date)

    def match_passengers(self, order_passengers, target_names=None):
        """乘车人匹配：订单乘车人集合必须包含全部目标乘车人（收紧语义）。

        target_names 缺省时用 TripKey 的指纹 tokens 反解——指纹是哈希，无法
        反解出姓名，因此调用方须显式传 target_names（通常即任务
        passenger_names）。返回 bool。
        """
        if target_names is None:
            raise ValueError("match_passengers 需要显式传入 target_names"
                             "（指纹为哈希不可反解）")
        want = [normalize_name(n) for n in target_names if normalize_name(n)]
        if not want:
            return False  # 空目标不匹配（防御：正常路径已拦）
        have = {normalize_name(p) for p in (order_passengers or [])}
        return all(w in have for w in want)