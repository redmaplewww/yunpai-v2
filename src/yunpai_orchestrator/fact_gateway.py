"""M0 canonical 事实网关：读口 + 形状归一（裁决 R1/R2 的单一实现点）。

> INT2 收口 1.1（`_migration/exec/PROMPT-INT2-R2.md`）。V2 原本没有 INT 的
> ``fact_gateway.py``，6 个分片各自在读取处复制了一份「``payload.attributes`` 并入
> 顶层、**顶层优先**」的归一化（父会话裁决 R2）。本模块把该口径收成单点：
> 分片内的局部实现改为薄适配层，行为逐字节等价（含扁平记录兜底、异常/空结果语义）。

事实源与红线：
- 读 canonical 一律经 ``m0_backend.M0Store.list_entities``（裁决 R1）；
  **禁止**直连 sqlite3 读 ``canonical_entities``/``canonical_entity_versions``；
- 读不到（库文件不存在 / 读口异常 / 形状非法）返回空，**不静默造数、不隐式建库**
  （``M0Store.__init__`` 会建库文件，故本模块先判 ``exists()`` 再实例化）；
  失败关闭由调用方按自身语义决定（M2 写 ``warnings``、M5 桥接转 BLOCKED_INPUT）。
"""

from __future__ import annotations

import json
import os
from typing import Any

#: canonical 信封里**不属于业务体**的键（扁平记录兜底拆分的默认口径）。
#: 与 ``m0_facts._envelope_fields`` / ``m3_local._unpack`` 原口径一致。
ENVELOPE_KEYS = frozenset({
    "identity", "entity_type", "evidence", "idempotency_key", "review_status",
    "reviewed_by", "schema_version", "source", "tenant_id", "canonical_key",
    "filename",
})

#: ``m2_local._canonical_body`` 原口径：扁平记录兜底时连 ``attributes`` 一起剥离
#: （因此扁平记录不参与 attributes 合并）。保持该差异以免改变 M2 既有行为。
M2_ENVELOPE_KEYS = ENVELOPE_KEYS | {"attributes"}

#: 业务字段可能被 ``business_catalog`` 落在 ``payload.attributes`` 下的位置参考。
#: （BOM ``lines`` 在顶层、SOP document ``route_steps`` 在 attributes 下。）


def merge_attributes(payload: dict[str, Any], *, drop_attributes: bool = False) -> dict[str, Any]:
    """``payload["attributes"]`` 的键并入顶层，**同名以顶层优先**。

    - ``drop_attributes=False``（默认）：结果保留 ``attributes`` 原键
      （``m0_facts`` / ``orchestration_bridge`` / ``m1_knowledge`` 口径）；
    - ``drop_attributes=True``：合并后删除 ``attributes`` 键
      （``m3_local._unpack`` 口径）；
    - ``attributes`` 不是 dict 时返回 payload 的浅拷贝（不新增、不删除键）。

    只做形状搬运，**不新增任何事实**：同名冲突一律取顶层值。
    """
    if not isinstance(payload, dict):
        return {}
    attributes = payload.get("attributes")
    if isinstance(attributes, dict):
        if drop_attributes:
            return {**attributes, **{key: value for key, value in payload.items()
                                     if key != "attributes"}}
        return {**attributes, **payload}  # 顶层优先
    return dict(payload)


def normalize_body(entity: dict[str, Any], *,
                   envelope_keys: frozenset[str] = ENVELOPE_KEYS,
                   drop_attributes: bool = False) -> dict[str, Any]:
    """canonical 信封（或平铺记录）→ 业务体。

    1. 拆信封：``entity["payload"]`` 为 dict 时用它；否则把 ``entity`` 里除
       ``envelope_keys`` 之外的键当作业务体（兼容平铺记录）；
    2. 合并 attributes：见 :func:`merge_attributes`。

    传入非 dict 返回 ``{}``（不抛异常、不造数）。
    """
    if not isinstance(entity, dict):
        return {}
    payload = entity.get("payload")
    if not isinstance(payload, dict):
        payload = {key: value for key, value in entity.items() if key not in envelope_keys}
    return merge_attributes(payload, drop_attributes=drop_attributes)


def split_envelope(envelope: dict[str, Any], *,
                   drop_attributes: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(业务体, identity)`` 二元组；identity 缺失/非 dict 时为 ``{}``。

    ``m0_facts._envelope_fields``（drop_attributes=False）与
    ``m3_local._unpack``（drop_attributes=True）的公共实现。
    """
    identity = envelope.get("identity") if isinstance(envelope, dict) else None
    if not isinstance(identity, dict):
        identity = {}
    return normalize_body(envelope, drop_attributes=drop_attributes), identity


def default_db_path() -> str:
    """canonical 库路径（与 ``M0Store`` 缺省口径一致）。"""
    return str(os.getenv("YUNPAI_M0_DB") or "runtime/yunpai-m0.sqlite")


def read_entities(entity_type: str, tenant_id: str = "default",
                  db_path: str | None = None) -> list[dict[str, Any]]:
    """按 ``entity_type`` 读回该租户 active 的 canonical 实体（业务体已归一化）。

    返回形状：``[{"canonical_key": ..., **attributes, **payload}, ...]``
    —— 与 ``orchestration_bridge._m0_local_entities`` 原实现一致（顶层优先，
    payload 自带的 ``canonical_key`` 覆盖读口键）。

    形状归一（R2，含 INT2 第五轮修正）：``merge_attributes`` 对**两层**都生效——
    1. 记录信封层的 ``attributes``（旧 business_catalog 平铺记录）；
    2. 信封内 ``payload``（m0.ingest.v1 业务体）自己的 ``attributes``。
    第 2 层是必需项：``business_catalog.canonical_records_from_batch`` 把 SOP 的
    ``route_steps`` 落在 ``record["payload"]["attributes"]`` 下（BOM 的 ``lines``
    在业务体顶层），只做第 1 层会让 ``orchestration_bridge._route_steps_from_entities``
    读不到任何工序（实测 route_steps=0，M2 工程事实不完整）。同名一律顶层优先、
    只增不改：业务体已有键不被覆盖。

    失败语义（**不静默造数**）：
    - 库文件不存在 → 返回 ``[]``，且**不创建**空库（先判 ``exists()``）；
    - ``M0Store`` 读口异常 → 原样抛出，由调用方决定是否失败关闭；
    - 行 ``payload_json`` 非 dict / JSON 解析失败 → 跳过该行（不伪造空体）。
    """
    from pathlib import Path

    from .m0_backend import M0Store

    path = db_path or default_db_path()
    if not Path(path).exists():
        return []
    rows = M0Store(path).list_entities(entity_type, tenant_id=tenant_id).get("entities") or []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        payload = row.get("payload_json")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                continue
        if not isinstance(payload, dict):
            continue
        item = {"canonical_key": row.get("canonical_key"), **merge_attributes(payload)}
        inner = item.get("payload")
        if isinstance(inner, dict):
            item["payload"] = merge_attributes(inner)
        out.append(item)
    return out
