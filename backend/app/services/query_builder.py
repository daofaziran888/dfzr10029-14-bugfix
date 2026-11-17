"""OLAP查询业务规则：模板状态流转、查询执行边界与失败诊断都收在这里。

查询执行有三类终态需要严格区分：

* ``数据返回`` / ``空数据``：查询真正执行过，属于成功终态；
* ``参数缺失``：模板要求的参数没有给全，查询没有执行；
* ``查询超时`` / ``中断恢复``：查询执行过但失败，失败原因和已返回结果行都要留档。

失败终态统一写入“查询失败诊断记录”，记录先进入待核对区，核对动作按诊断编号幂等，
重复提交不会产生第二份确认结果。
"""
from __future__ import annotations

import copy
import hashlib
import math
import re
from datetime import datetime, timezone
from typing import Any

from app.store import store

MODULE = "query_builder"
LIST_FIELDS = ["模板编号", "模板名称", "查询语句", "参数定义", "数据集范围", "超时秒数", "缓存策略", "模板状态"]
REQUIRED_FIELDS = ["模板编号", "模板名称", "查询语句"]
STATUS_ORDER = ["草稿", "已保存", "已发布", "已停用"]
ACTION_RULES = {"保存模板": "已保存", "发布模板": "已发布", "停用模板": "已停用"}
NEGATIVE_ACTIONS = ["停用模板"]

STATE_DATA = "数据返回"
STATE_EMPTY = "空数据"
STATE_MISSING_PARAM = "参数缺失"
STATE_TIMEOUT = "查询超时"
STATE_INTERRUPTED = "中断恢复"
SUCCESS_STATES = (STATE_DATA, STATE_EMPTY)
FAILURE_STATES = (STATE_MISSING_PARAM, STATE_TIMEOUT, STATE_INTERRUPTED)

REASON_OK = "OK"
REASON_EMPTY = "EMPTY_DATA"
REASON_MISSING_PARAM = "MISSING_PARAM"
REASON_TIMEOUT = "TIMEOUT"
REASON_INTERRUPTED = "INTERRUPTED"

TIMEOUT_POLICY_PARTIAL = "保留部分结果"
TIMEOUT_POLICY_ROLLBACK = "回滚整份查询"
TIMEOUT_POLICIES = (TIMEOUT_POLICY_PARTIAL, TIMEOUT_POLICY_ROLLBACK)

DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_ROW_COUNT = 6
MAX_ROW_COUNT = 200

DIAGNOSTIC_PENDING = "待核对"
DIAGNOSTIC_REVIEWED = "已核对"
CONFIRM_ACTIONS = {"确认失败", "确认通过"}

_PARAM_SPLIT = re.compile(r"[,，、;；\n]+")
_CONTROL_KEYS = {
    "values", "params", "parameters", "查询参数", "返回行数", "行数", "limit", "rowCount", "row_count",
    "超时秒数", "超时设置", "timeout", "timeoutSeconds", "timeout_seconds",
    "预计耗时秒数", "耗时秒数", "耗时", "elapsed", "elapsedSeconds", "elapsed_seconds",
    "durationSeconds", "duration_seconds",
    "超时策略", "timeoutPolicy", "timeout_policy", "中断恢复", "恢复", "resume",
    "已超时", "超时", "timedOut", "timed_out",
    "已返回行", "已取得行", "已返回结果行", "returnedRows", "partialRows", "receivedRows", "rows",
    "动作", "action", "remark", "说明",
}
# 诊断记录的身份字段：重复写入同一条失败时保留，其余字段整体重建，避免详情残留旧值。
_DIAGNOSTIC_IDENTITY_FIELDS = {
    "id", "诊断编号", "createdAt", "occurrences", "status", "pending", "abnormal", "reopened",
    "confirmation", "resultReviewed", "resultReviewedAt",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_positive_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    parsed = _as_int(value, -1)
    return parsed if parsed > 0 else None


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().casefold() in {"1", "true", "yes", "y", "是", "中断恢复", "恢复"}


def _first_value(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return None


_PLACEHOLDER = re.compile(r"(?<!:):([A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff]*)")


def parse_parameter_names(definition: Any) -> list[str]:
    """从“参数定义”里解析参数名，兼容 ``区域=华东,日期=2026-11``、``:区域`` 与纯名称列表。"""
    if definition is None:
        return []
    names: list[str] = []
    for token in _PARAM_SPLIT.split(str(definition).strip()):
        token = token.strip()
        if not token:
            continue
        name = re.split(r"[=:]", token, maxsplit=1)[0].strip().strip("{}[]`'\"")
        if not name or name.casefold() in {"无", "无参数", "none", "null"}:
            continue
        if name not in names:
            names.append(name)
    return names


def declared_parameter_names(entry: dict[str, Any]) -> list[str]:
    """参数定义优先；为空时退回到查询语句里的 ``:占位符``，避免漏判空参数。"""
    names = parse_parameter_names(entry.get("参数定义"))
    if names:
        return names
    names = []
    for match in _PLACEHOLDER.finditer(str(entry.get("查询语句") or "")):
        name = match.group(1).strip()
        if name and name.casefold() not in {"none", "null"} and name not in names:
            names.append(name)
    return names


def _normalise_params(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    params: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key).strip()
        if not name:
            continue
        params[name] = "" if value is None else str(value).strip()
    return params


def _param_summary(declared: list[str], params: dict[str, str]) -> str:
    names = list(dict.fromkeys([*declared, *params.keys()]))
    if not names:
        return "无参数"
    return "；".join(f"{name}={params.get(name) or '（空）'}" for name in names)


def _read_run_input(payload: dict[str, Any]) -> dict[str, Any]:
    """把请求里的查询参数、行数、耗时、超时策略拆开，空值保留以便判定“空参数”。

    兼容三种入参写法：``查询参数``/``params`` 独立字段、``values`` 表单载荷、顶层直传字段。
    """
    payload = payload or {}
    values = payload.get("values") if isinstance(payload.get("values"), dict) else {}

    raw_params: Any = None
    for key in ("查询参数", "params", "parameters"):
        if isinstance(payload.get(key), dict):
            raw_params = payload[key]
            break
    if raw_params is None and isinstance(values.get("查询参数"), dict):
        raw_params = values["查询参数"]
    if raw_params is None and isinstance(values.get("params"), dict):
        raw_params = values["params"]
    if raw_params is None and values:
        raw_params = {key: value for key, value in values.items() if key not in _CONTROL_KEYS}
    if raw_params is None:
        raw_params = {key: value for key, value in payload.items() if key not in _CONTROL_KEYS}

    def pick(*keys: str) -> Any:
        for source in (payload, values):
            found = _first_value(source, *keys)
            if found is not None:
                return found
        return None

    row_count_raw = pick("返回行数", "行数", "limit", "rowCount", "row_count")
    elapsed_raw = pick("预计耗时秒数", "耗时秒数", "耗时", "elapsed", "elapsedSeconds", "elapsed_seconds",
                       "durationSeconds", "duration_seconds")
    timeout_raw = pick("超时秒数", "超时设置", "timeout", "timeoutSeconds", "timeout_seconds")
    policy_raw = pick("超时策略", "timeoutPolicy", "timeout_policy")
    resume_raw = pick("中断恢复", "恢复", "resume")
    timed_out_raw = pick("已超时", "超时", "timedOut", "timed_out")
    returned_rows_raw = pick(
        "已返回行", "已取得行", "已返回结果行", "returnedRows", "partialRows", "receivedRows", "rows")

    return {
        "params": _normalise_params(raw_params),
        "row_count": _as_int(row_count_raw, DEFAULT_ROW_COUNT) if row_count_raw is not None else DEFAULT_ROW_COUNT,
        "elapsed_seconds": _as_float(elapsed_raw, 0.0) if elapsed_raw is not None else 0.0,
        "timeout_override": _as_positive_int(timeout_raw),
        "timeout_override_present": timeout_raw is not None,
        "timeout_policy": str(policy_raw).strip() if policy_raw is not None else TIMEOUT_POLICY_PARTIAL,
        "resume": _is_truthy(resume_raw),
        "timed_out": _is_truthy(timed_out_raw),
        "returned_rows": _normalise_rows(returned_rows_raw),
    }


def _normalise_rows(raw: Any) -> list[dict[str, Any]]:
    """归一化上游已经返回的结果行，超时时作为诊断保留或随回滚丢弃。"""
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if isinstance(item, dict):
            row = {str(key): value for key, value in item.items()}
        else:
            row = {"值": item}
        row.setdefault("行号", index + 1)
        rows.append(row)
    return rows


def _build_rows(entry: dict[str, Any], params: dict[str, str], count: int) -> list[dict[str, Any]]:
    """按模板与参数生成稳定的模拟结果行，方便核对同一次查询的重试结果。"""
    seed = f"{entry.get('id')}|{entry.get('模板编号')}|" + "|".join(
        f"{key}={value}" for key, value in sorted(params.items()))
    dimension = next((value for value in params.values() if value), "全量")
    rows: list[dict[str, Any]] = []
    for index in range(max(0, count)):
        digest = hashlib.sha256(f"{seed}|{index}".encode("utf-8")).hexdigest()
        metric = round((int(digest[:4], 16) % 9000 + 1000) / 1000, 3)
        baseline = round(metric - (int(digest[4:8], 16) % 600 - 300) / 1000, 3)
        rows.append({
            "行号": index + 1,
            "分组": f"分组{index % 3 + 1}",
            "维度": dimension,
            "指标值": metric,
            "对比基准": baseline,
            "差异": round(metric - baseline, 3),
        })
    return rows


class QueryBuilderService:
    """查询模板服务；诊断记录挂在类上，供查询结果接口与审计模块共同读取。"""

    _diagnostics: list[dict[str, Any]] = []
    _diagnostic_index: dict[tuple[Any, ...], int] = {}
    _diagnostic_seq: int = 0
    _run_seq: int = 0
    _latest_results: dict[int, dict[str, Any]] = {}

    # ------------------------------------------------------------------ 模板 CRUD
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("模板编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"查询模板 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于OLAP查询可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"查询模板已{action}"

    # ------------------------------------------------------------------ 查询执行
    def run_query(self, entry_id: int, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """解析既有输入，先判空参数，再判超时，最终返回带失败原因的查询终态。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"查询模板 {entry_id} 不存在或已归档"

        raw_input = copy.deepcopy(payload or {})
        run_input = _read_run_input(raw_input)
        params: dict[str, str] = run_input["params"]
        declared = declared_parameter_names(entry)
        missing = [name for name in declared if not params.get(name)]
        summary = _param_summary(declared, params)
        notes: list[str] = []

        row_count = run_input["row_count"]
        if row_count < 0:
            notes.append("返回行数不能为负数，已按 0 行处理。")
            row_count = 0
        if row_count > MAX_ROW_COUNT:
            notes.append(f"返回行数超过上限，已按 {MAX_ROW_COUNT} 行处理。")
            row_count = MAX_ROW_COUNT

        elapsed = run_input["elapsed_seconds"]
        if elapsed < 0:
            notes.append("预计耗时不能为负数，已按 0 秒处理。")
            elapsed = 0.0

        timeout_seconds, timeout_source = self._resolve_timeout(entry, run_input, notes)

        policy = run_input["timeout_policy"]
        if policy not in TIMEOUT_POLICIES:
            notes.append(f"超时策略「{policy}」无效，已按「{TIMEOUT_POLICY_PARTIAL}」处理。")
            policy = TIMEOUT_POLICY_PARTIAL

        self.__class__._run_seq += 1
        outcome: dict[str, Any] = {
            "runSeq": self.__class__._run_seq,
            "templateId": int(entry.get("id", entry_id)),
            "模板编号": entry.get("模板编号"),
            "模板名称": entry.get("模板名称"),
            "params": params,
            "paramSummary": summary,
            "missingParams": [],
            "timeoutSeconds": timeout_seconds,
            "timeoutSource": timeout_source,
            "timeoutPolicy": policy,
            "elapsedSeconds": round(elapsed, 2),
            "rows": [],
            "rowCount": 0,
            "partial": False,
            "rolledBack": False,
            "retryable": False,
            "notes": notes,
            "input": raw_input,
            "处理输入": raw_input,
            "diagnosticId": None,
            "诊断编号": None,
            "recoveredFromDiagnosticId": None,
            "ranAt": _now(),
        }

        # 1) 空参数边界：参数没给全时查询没有执行，必须给出失败终态而不是成功空页。
        if missing:
            outcome.update({
                "status": "失败",
                "state": STATE_MISSING_PARAM,
                "reasonCode": REASON_MISSING_PARAM,
                "missingParams": missing,
                "reason": (
                    "查询未执行：缺少查询参数"
                    + "".join(f"「{name}」" for name in missing)
                    + "。请补齐参数后重试。"
                ),
                "retryable": True,
            })
            self._record_diagnostic(outcome, STATE_MISSING_PARAM, "")
            self._remember(outcome)
            return outcome, outcome["reason"]

        # 2) 中断恢复：沿用既有诊断里已取得的结果行，单独说明恢复来源。
        if run_input["resume"]:
            source = self._find_recoverable_diagnostic(outcome["templateId"])
            if source is None:
                outcome.update({
                    "status": "失败",
                    "state": STATE_INTERRUPTED,
                    "reasonCode": REASON_INTERRUPTED,
                    "reason": "查询中断后恢复失败：未找到可恢复的查询结果行，请整份重新执行。",
                    "retryable": True,
                })
            else:
                recovered = copy.deepcopy(source.get("resultRows") or [])
                outcome.update({
                    "status": "失败",
                    "state": STATE_INTERRUPTED,
                    "reasonCode": REASON_INTERRUPTED,
                    "rows": recovered,
                    "rowCount": len(recovered),
                    "partial": True,
                    "retryable": True,
                    "recoveredFromDiagnosticId": source["id"],
                    "reason": (
                        f"查询中断后恢复：已按既有次序恢复上次取得的 {len(recovered)} 行"
                        f"（诊断 {source.get('诊断编号')}）；剩余结果请重试整份查询。"
                    ),
                })
            self._record_diagnostic(
                outcome, STATE_INTERRUPTED, f"source={outcome.get('recoveredFromDiagnosticId')}")
            self._remember(outcome)
            return outcome, outcome["reason"]

        all_rows = _build_rows(entry, params, row_count)
        # 上游已经返回过的结果行优先作为“已取得行”，超时时按策略保留或随回滚丢弃。
        received_rows = run_input["returned_rows"]
        if received_rows:
            all_rows = copy.deepcopy(received_rows)

        # 3) 超时边界：实际耗时严格大于超时上限才算超时，恰好等于上限仍在边界内；
        #    上游明确标记“已超时”时同样进入超时终态，避免把超时当成正常空数据。
        flagged_timeout = bool(run_input["timed_out"])
        timed_out = flagged_timeout or elapsed > timeout_seconds
        if timed_out:
            if elapsed > timeout_seconds:
                cause = f"实际耗时 {elapsed:g} 秒超过超时设置 {timeout_seconds} 秒"
            else:
                cause = f"上游已标记超时（超时设置 {timeout_seconds} 秒）"
            if policy == TIMEOUT_POLICY_ROLLBACK:
                outcome.update({
                    "status": "失败",
                    "state": STATE_TIMEOUT,
                    "reasonCode": REASON_TIMEOUT,
                    "rows": [],
                    "rowCount": 0,
                    "rolledBack": True,
                    "rolledBackRowCount": len(all_rows),
                    "reason": (
                        f"查询超时：{cause}；已回滚整份查询，未返回结果行"
                        + (f"（丢弃已取得的 {len(all_rows)} 行）" if all_rows else "")
                        + "，请调整参数或超时设置后整份重试。"
                    ),
                    "retryable": True,
                })
            else:
                if received_rows:
                    # 已经拿到手的行全部作为诊断保留，不再按耗时二次折算。
                    kept_rows = all_rows
                elif elapsed > 0:
                    kept = min(len(all_rows), int(math.floor(len(all_rows) * timeout_seconds / elapsed)))
                    kept_rows = all_rows[:max(0, kept)]
                else:
                    # 耗时缺失时按已取回 0 行处理。
                    kept_rows = []
                outcome.update({
                    "status": "失败",
                    "state": STATE_TIMEOUT,
                    "reasonCode": REASON_TIMEOUT,
                    "rows": kept_rows,
                    "rowCount": len(kept_rows),
                    "partial": bool(kept_rows),
                    "reason": (
                        f"查询超时：{cause}；已保留超时前取得的 {len(kept_rows)} 行作为诊断，"
                        "可核对后基于部分结果重试剩余部分。"
                    ),
                    "retryable": True,
                })
            self._record_diagnostic(outcome, STATE_TIMEOUT, policy)
            self._remember(outcome)
            return outcome, outcome["reason"]

        if elapsed == timeout_seconds and timeout_seconds > 0:
            notes.append("实际耗时恰好等于超时上限，按边界内处理，未判定为超时。")

        if not all_rows:
            outcome.update({
                "status": "成功",
                "state": STATE_EMPTY,
                "reasonCode": REASON_EMPTY,
                "reason": "查询已执行：参数完整，结果集为空（0 行），这是空数据而不是查询失败。",
            })
        else:
            outcome.update({
                "status": "成功",
                "state": STATE_DATA,
                "reasonCode": REASON_OK,
                "rows": all_rows,
                "rowCount": len(all_rows),
                "reason": f"查询执行成功：返回 {len(all_rows)} 行。",
            })
        self._remember(outcome)
        return outcome, outcome["reason"]

    def _resolve_timeout(
        self, entry: dict[str, Any], run_input: dict[str, Any], notes: list[str],
    ) -> tuple[int, str]:
        override = run_input["timeout_override"]
        if override is not None:
            return int(override), "请求覆盖"
        if run_input["timeout_override_present"]:
            notes.append("请求里的超时秒数无效（必须为正整数），已忽略并沿用模板设置。")

        raw = entry.get("超时秒数")
        parsed = _as_positive_int(raw)
        if parsed is not None:
            return int(parsed), "模板设置"
        notes.append(
            f"模板超时秒数「{raw if raw not in (None, '') else '未设置'}」无效，"
            f"已按默认 {DEFAULT_TIMEOUT_SECONDS} 秒执行。")
        return DEFAULT_TIMEOUT_SECONDS, "默认值"

    def _remember(self, outcome: dict[str, Any]) -> None:
        self.__class__._latest_results[int(outcome["templateId"])] = copy.deepcopy(outcome)

    # ------------------------------------------------------------------ 结果读取
    def list_results(self) -> list[dict[str, Any]]:
        results = [copy.deepcopy(item) for item in self._latest_results.values()]
        return sorted(results, key=lambda item: int(item.get("runSeq", 0)))

    def get_result(self, entry_id: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[int]]:
        """查询结果接口读取并复核对应的失败诊断记录，返回结果与诊断的对应关系。"""
        outcome = copy.deepcopy(self._latest_results.get(entry_id))
        if outcome is None:
            return None, [], []
        reviewed: list[int] = []
        diagnostics: list[dict[str, Any]] = []
        stamp = _now()
        for record in self._diagnostics:
            if int(record.get("templateId", 0)) != entry_id:
                continue
            if record.get("paramSummary") != outcome.get("paramSummary"):
                continue
            record["resultReviewed"] = True
            record["resultReviewedAt"] = stamp
            reviewed.append(int(record["id"]))
            diagnostics.append(self._public_diagnostic(record))
        outcome["diagnostics"] = diagnostics
        outcome["reviewedDiagnosticIds"] = reviewed
        return outcome, diagnostics, reviewed

    def export_result(self, template_id: int | None = None) -> dict[str, Any] | None:
        """导出用的查询终态：同一时刻只取一份，参数摘要随终态一起带出。"""
        if template_id is not None:
            outcome = self._latest_results.get(int(template_id))
        elif self._latest_results:
            outcome = max(
                self._latest_results.values(), key=lambda item: int(item.get("runSeq", 0)))
        else:
            outcome = None
        return copy.deepcopy(outcome)

    # ------------------------------------------------------------------ 失败诊断
    def list_diagnostics(
        self,
        *,
        status: str | None = None,
        template_id: int | None = None,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """列表按诊断编号去重，重复执行同一个失败不会多出一条。"""
        seen: set[int] = set()
        items: list[dict[str, Any]] = []
        for record in self._diagnostics:
            diagnostic_id = int(record.get("id", 0))
            if diagnostic_id in seen:
                continue
            if status and record.get("status") != status:
                continue
            if template_id is not None and int(record.get("templateId", 0)) != int(template_id):
                continue
            if kind and record.get("kind") != kind:
                continue
            seen.add(diagnostic_id)
            items.append(self._public_diagnostic(record, include_rows=False))
        return items

    def get_diagnostic(self, diagnostic_id: int) -> dict[str, Any] | None:
        record = self._find_diagnostic(diagnostic_id)
        return self._public_diagnostic(record) if record is not None else None

    def confirm_diagnostic(
        self, diagnostic_id: int, action: str, remark: str | None = None,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """提交核对；同一诊断编号只保留一份确认结果，重复提交沿用既有结果。"""
        record = self._find_diagnostic(diagnostic_id)
        if record is None:
            return None, f"查询失败诊断记录 {diagnostic_id} 不存在", False
        if record.get("confirmation"):
            # 重复提交只做“再次确认”：沿用既有确认结果，不生成第二份确认记录。
            if record.get("pending"):
                record["status"] = DIAGNOSTIC_REVIEWED
                record["pending"] = False
                record["abnormal"] = False
                record["reopened"] = False
                record["updatedAt"] = _now()
            return (
                self._public_diagnostic(record),
                "该诊断记录已核对，沿用既有确认结果，未新增第二份确认结果",
                True,
            )
        if action not in CONFIRM_ACTIONS:
            return None, f"动作「{action}」不属于查询失败诊断记录可执行范围", False
        confirmation = {
            "确认编号": f"QDGC-{diagnostic_id:04d}",
            "确认结论": action,
            "确认说明": (remark or "").strip()
            or ("已确认失败原因，等待参数补齐或重试" if action == "确认失败" else "已确认失败原因可关闭"),
            "确认时间": _now(),
        }
        record["confirmation"] = confirmation
        record["status"] = DIAGNOSTIC_REVIEWED
        record["pending"] = False
        record["abnormal"] = False
        record["updatedAt"] = _now()
        return self._public_diagnostic(record), "查询失败诊断记录已核对", False

    def _find_diagnostic(self, diagnostic_id: int) -> dict[str, Any] | None:
        for record in self._diagnostics:
            if int(record.get("id", 0)) == int(diagnostic_id):
                return record
        return None

    def _find_recoverable_diagnostic(self, template_id: int) -> dict[str, Any] | None:
        candidates = [
            record for record in self._diagnostics
            if int(record.get("templateId", 0)) == template_id
            and record.get("kind") in (STATE_TIMEOUT, STATE_INTERRUPTED)
            and int(record.get("returnedRowCount", 0)) > 0
            and record.get("resultRows")
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: str(item.get("updatedAt", "")))

    def _record_diagnostic(
        self, outcome: dict[str, Any], kind: str, policy_key: str,
    ) -> dict[str, Any]:
        key = (outcome["templateId"], kind, outcome["paramSummary"], policy_key,
               bool(outcome.get("recoveredFromDiagnosticId")))
        index = self._diagnostic_index.get(key)
        record = self._diagnostics[index] if index is not None else None
        if record is None:
            self.__class__._diagnostic_seq += 1
            record = {
                "id": self.__class__._diagnostic_seq,
                "诊断编号": f"QDIAG-{self.__class__._diagnostic_seq:04d}",
                "createdAt": _now(),
                "occurrences": 0,
                "status": DIAGNOSTIC_PENDING,
                "pending": True,
                "abnormal": True,
                "confirmation": None,
                "reopened": False,
                "resultReviewed": False,
                "resultReviewedAt": None,
            }
            self._diagnostics.append(record)
            self._diagnostic_index[key] = len(self._diagnostics) - 1
        record["occurrences"] = int(record.get("occurrences", 0)) + 1
        # 已核对的记录再次出现同一失败时回到待核对区，但沿用既有确认结果，不生成第二份。
        if record.get("confirmation"):
            record["reopened"] = True
            record["status"] = DIAGNOSTIC_PENDING
            record["pending"] = True
            record["abnormal"] = True
        # 重复写入时整体重建业务字段，避免详情残留上一次失败的类型、原因或结果行。
        for field in [field for field in record if field not in _DIAGNOSTIC_IDENTITY_FIELDS]:
            del record[field]
        record.update({
            "templateId": outcome["templateId"],
            "模板编号": outcome.get("模板编号"),
            "模板名称": outcome.get("模板名称"),
            "kind": kind,
            "reasonCode": outcome["reasonCode"],
            "reason": outcome["reason"],
            "paramSummary": outcome["paramSummary"],
            "params": copy.deepcopy(outcome.get("params", {})),
            "missingParams": copy.deepcopy(outcome.get("missingParams", [])),
            "timeoutSeconds": outcome.get("timeoutSeconds"),
            "timeoutPolicy": outcome.get("timeoutPolicy"),
            "elapsedSeconds": outcome.get("elapsedSeconds"),
            "returnedRowCount": outcome.get("rowCount", 0),
            "partial": outcome.get("partial", False),
            "rolledBack": outcome.get("rolledBack", False),
            "rolledBackRowCount": outcome.get("rolledBackRowCount", 0),
            "retryable": outcome.get("retryable", False),
            "resultRows": copy.deepcopy(outcome.get("rows", [])),
            "input": copy.deepcopy(outcome.get("input", {})),
            "处理输入": copy.deepcopy(outcome.get("input", {})),
            "recoveredFromDiagnosticId": outcome.get("recoveredFromDiagnosticId"),
            "updatedAt": _now(),
        })
        outcome["diagnosticId"] = record["id"]
        outcome["诊断编号"] = record["诊断编号"]
        return record

    @staticmethod
    def _public_diagnostic(record: dict[str, Any], *, include_rows: bool = True) -> dict[str, Any]:
        data = copy.deepcopy(record)
        rows = data.pop("resultRows", [])
        if include_rows:
            data["rows"] = rows
        return data


def query_diagnostics(
    *, status: str | None = None, template_id: int | None = None, kind: str | None = None,
) -> list[dict[str, Any]]:
    """跨业务模块读取查询失败诊断记录时使用的只读入口。"""
    return QueryBuilderService().list_diagnostics(status=status, template_id=template_id, kind=kind)


def query_diagnostic(diagnostic_id: int) -> dict[str, Any] | None:
    return QueryBuilderService().get_diagnostic(diagnostic_id)
