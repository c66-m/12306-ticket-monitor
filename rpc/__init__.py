# -*- coding: utf-8 -*-
"""rpc 包：原 api.py 按域拆分（A-3a）。

common 为共享基础设施；tasks/grab/orders/passengers/settings/history/
session 为七个 RPC 域。api.py 是门面，re-export 全部符号，外部
import 路径（import api）与符号面不变。本包 __init__ 不做任何导入，
避免包初始化阶段的循环依赖。
"""
