"""OLAP查询接口：维护查询模板，执行模板查询，并核对查询失败诊断记录。

查询终态分三类返回：成功（数据返回 / 空数据）、空参数失败、超时或中断失败。
失败终态统一带 ``reason`` 与 ``diagnosticId``，避免结果页把未执行的查询当成正常空数据。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.query_builder import QueryBuilderService
from app.services.query_builder import query_diagnostics as read_query_diagnostics

router = APIRouter(prefix="/api/query_builder", tags=["OLAP查询"])

service = QueryBuilderService()

LIST_FIELDS = ["模板编号", "模板名称", "查询语句", "参数定义", "数据集范围", "超时秒数", "缓存策略", "模板状态"]
STATUSES = ["草稿", "已保存", "已发布", "已停用"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按模板编号检索"),
    status: str | None = Query(default=None, description="草稿、已保存、已发布、已停用"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、模板编号关键字与状态过滤OLAP查询列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="query_builder.csv"'})


@router.get("/results")
def list_results() -> dict[str, Any]:
    """最近的查询终态列表，按执行先后排列。"""
    return {"items": [_result_payload(item) for item in service.list_results()]}


@router.get("/results/export")
def export_result(
    templateId: int | None = Query(default=None),
    template_id: int | None = Query(default=None),
) -> StreamingResponse:
    """导出查询终态：只包含一份终态，参数摘要随终态一并带出。"""
    return _query_result_csv(templateId if templateId is not None else template_id)


@router.get("/result/export", include_in_schema=False)
def export_result_alias(
    templateId: int | None = Query(default=None),
    template_id: int | None = Query(default=None),
) -> StreamingResponse:
    """``/result/export`` 是 ``/results/export`` 的兼容别名。"""
    return _query_result_csv(templateId if templateId is not None else template_id)


def _csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


def _query_result_csv(template_id: int | None) -> StreamingResponse:
    """导出查询终态：文件里只写一份终态，参数摘要与失败原因随终态一起带出。"""
    outcome = service.export_result(template_id)
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["模板编号", "模板名称", "查询终态", "失败原因码", "失败原因", "参数摘要", "返回行数",
                     "超时秒数", "实际耗时秒数", "诊断编号"])
    if outcome is not None:
        writer.writerow([_csv_cell(value) for value in [
            outcome.get("模板编号", ""),
            outcome.get("模板名称", ""),
            outcome.get("state", ""),
            outcome.get("reasonCode", ""),
            outcome.get("reason", ""),
            outcome.get("paramSummary", ""),
            outcome.get("rowCount", 0),
            outcome.get("timeoutSeconds", ""),
            outcome.get("elapsedSeconds", ""),
            outcome.get("诊断编号") or "",
        ]])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="query_result.csv"'})


def _result_payload(outcome: dict[str, Any]) -> dict[str, Any]:
    """查询终态响应：同时给出英文键与页面直接展示的中文键，避免结果页误读为空数据。"""
    return {
        **outcome,
        "ok": outcome.get("status") == "成功",
        "terminalState": outcome.get("state"),
        "failureReason": outcome.get("reason"),
        "parameterSummary": outcome.get("paramSummary"),
        "diagnosticRecordId": outcome.get("diagnosticId"),
        "查询终态": outcome.get("state"),
        "失败原因": outcome.get("reason"),
        "参数摘要": outcome.get("paramSummary"),
        "结果行": outcome.get("rows", []),
        "是否失败": outcome.get("status") == "失败",
    }


@router.get("/diagnostics")
def list_diagnostics(
    status: str | None = Query(default=None, description="待核对、已核对"),
    templateId: int | None = Query(default=None),
    template_id: int | None = Query(default=None),
    kind: str | None = Query(default=None, description="参数缺失、查询超时、中断恢复"),
) -> dict[str, Any]:
    """查询失败诊断记录：按诊断编号去重，待核对区默认保留异常诊断。"""
    template = templateId if templateId is not None else template_id
    items = read_query_diagnostics(status=status, template_id=template, kind=kind)
    return {"items": items, "total": len(items)}


@router.get("/failures", include_in_schema=False)
def list_failures(
    status: str | None = Query(default=None),
    templateId: int | None = Query(default=None),
    template_id: int | None = Query(default=None),
    kind: str | None = Query(default=None),
) -> dict[str, Any]:
    """``/failures`` 是查询失败诊断记录列表的兼容别名。"""
    return list_diagnostics(
        status=status,
        templateId=templateId if templateId is not None else template_id,
        kind=kind,
    )


@router.get("/diagnostics/{diagnostic_id}")
def get_diagnostic(diagnostic_id: int) -> dict[str, Any]:
    record = service.get_diagnostic(diagnostic_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"查询失败诊断记录 {diagnostic_id} 不存在")
    return record


@router.post("/diagnostics/{diagnostic_id}/actions", response_model=ActionResult)
def run_diagnostic_action(diagnostic_id: int, payload: ActionPayload) -> ActionResult:
    """核对失败诊断；同一诊断编号只保留一份确认结果，重复提交沿用既有结果。"""
    record, message, reused = service.confirm_diagnostic(
        diagnostic_id, payload.action.strip(), payload.remark)
    if record is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=record)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条查询模板明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"查询模板 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/results")
def get_result(entry_id: int) -> dict[str, Any]:
    """查询结果接口：返回终态、失败原因与结果行，并复核对应的失败诊断记录。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"查询模板 {entry_id} 不存在或已归档")
    outcome, diagnostics, reviewed = service.get_result(entry_id)
    if outcome is None:
        return {
            "ok": False,
            "status": "未执行",
            "state": "未执行",
            "reasonCode": "NOT_RUN",
            "reason": "该查询模板还没有执行记录，请先运行查询。",
            "paramSummary": "未执行",
            "items": [],
            "rows": [],
            "diagnostics": [],
            "reviewedDiagnosticIds": reviewed,
            "查询终态": "未执行",
            "失败原因": "该查询模板还没有执行记录，请先运行查询。",
            "参数摘要": "未执行",
            "结果行": [],
            "是否失败": False,
        }
    return {
        **_result_payload(outcome),
        "items": outcome.get("rows", []),
        "diagnostics": diagnostics,
        "reviewedDiagnosticIds": reviewed,
    }


@router.get("/{entry_id}/result", include_in_schema=False)
def get_result_alias(entry_id: int) -> dict[str, Any]:
    """``/result`` 是 ``/results`` 的兼容别名。"""
    return get_result(entry_id)


@router.post("/{entry_id}/run", response_model=dict)
def run_query(entry_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """执行查询模板：先判定空参数，再判定超时，失败时给出可核对的终态与原因。"""
    outcome, message = service.run_query(entry_id, payload or {})
    if outcome is None:
        raise HTTPException(status_code=404, detail=message)
    return {
        **_result_payload(outcome),
        "message": message,
        "items": outcome.get("rows", []),
    }


@router.post("/{entry_id}/execute", response_model=dict, include_in_schema=False)
def run_query_alias(entry_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """``/execute`` 是 ``/run`` 的兼容别名，语义完全一致。"""
    return run_query(entry_id, payload)


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条查询模板，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="查询模板已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条查询模板执行保存模板、发布模板、停用模板；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
