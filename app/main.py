"""FastAPI 应用：取整表合计登记与查询服务。

业务流程
--------
1. 发布员每月 ``POST /api/v1/tables`` 登记一份数仓导出的非负整数计数
   矩阵（乡镇为行、年龄组为列，最多 12x12），登记后不可修改；
2. 分批 ``POST /api/v1/tables/{month}/batches`` 提交简报拟用的行/列/
   总计合计（5 的倍数）。只有同一张取整表能同时满足本批与已确认合计时
   才写入 SQLite，否则返回 409 且已确认合计保持不变；
3. ``GET /api/v1/tables/{month}`` 返回符合已确认合计的取整矩阵，以及
   从格值加出的行合计、列合计与全表总计。
"""

from __future__ import annotations

from typing import Annotated

from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse

from . import db
from .schemas import (
    BatchAcceptedResponse,
    BatchTotalsRequest,
    ConflictResponse,
    EX_BATCH_1,
    EX_BATCH_2,
    EX_BATCH_COMBINED,
    EX_TABLE,
    HealthResponse,
    MonthlyMatrixResponse,
    MonthSummary,
    RegisteredTableResponse,
    RegisterTableRequest,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="医院取整表合计登记 API",
    description=(
        "病例简报先公布部分乡镇/年龄组合计，月报附表按 5 的倍数取整后仍须"
        "加出这些数。本服务登记每月原始计数矩阵，分批确认行/列/总计合计，"
        "并用带下界可行流（Dinic）判定同一张取整表能否同时满足全部已确认"
        "合计；无解即报冲突，已确认合计不可更改。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# 异常处理
# ---------------------------------------------------------------------------

@app.exception_handler(db.TableExistsError)
def _handle_exists(_request: Request, exc: db.TableExistsError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"status": "conflict",
                                                  "reasons": [str(exc)]})


@app.exception_handler(db.MonthNotFoundError)
def _handle_not_found(_request: Request, exc: db.MonthNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"status": "not_found",
                                                  "reasons": [str(exc)]})


@app.exception_handler(db.RequestShapeError)
def _handle_shape(_request: Request, exc: db.RequestShapeError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"status": "validation_error", "reasons": [str(exc)]},
    )


@app.exception_handler(db.TotalsConflictError)
def _handle_conflict(_request: Request, exc: db.TotalsConflictError) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "status": "conflict",
            "month": _request.path_params.get("month", ""),
            "reasons": exc.reasons,
            "confirmed_totals": exc.confirmed,
        },
    )


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse, tags=["运维"], summary="健康检查")
def health() -> dict:
    return {"status": "ok", "service": "rounding-totals"}


@app.post(
    "/api/v1/tables",
    response_model=RegisteredTableResponse,
    status_code=201,
    tags=["登记表"],
    summary="登记某月原始计数矩阵（每月一份，登记后不可修改）",
)
def register_table(
    payload: Annotated[
        RegisterTableRequest,
        Body(
            openapi_examples={
                "sample_2x2": {
                    "summary": "样例原表：2 个乡镇 x 2 个年龄组",
                    "description": "Swagger 两批合计样例均基于该 2026-10 原表",
                    "value": EX_TABLE,
                }
            }
        ),
    ]
):
    rows = len(payload.matrix)
    cols = len(payload.matrix[0])
    row_names = payload.row_names or [f"row_{i}" for i in range(rows)]
    col_names = payload.col_names or [f"col_{j}" for j in range(cols)]
    return db.register_table(
        month=payload.month,
        matrix=payload.matrix,
        row_names=row_names,
        col_names=col_names,
    )


@app.post(
    "/api/v1/tables/{month}/batches",
    response_model=BatchAcceptedResponse,
    responses={
        404: {"description": "该月份尚未登记矩阵"},
        409: {"model": ConflictResponse, "description": "本批与已确认合计冲突，未写入"},
    },
    tags=["合计批次"],
    summary="分批提交简报拟用的行/列/总计合计",
)
def submit_batch(
    month: str,
    payload: Annotated[
        BatchTotalsRequest,
        Body(
            openapi_examples={
                "batch_1_feasible": {
                    "summary": "第一批：单独可行（低行合计）",
                    "description": (
                        "两行行合计向下取整、两列列合计向上取整；存在取整表"
                        "同时满足，提交后写入 SQLite"
                    ),
                    "value": EX_BATCH_1,
                },
                "batch_2_feasible": {
                    "summary": "第二批：单独可行（高行合计）",
                    "description": (
                        "两行行合计向上取整；在没有第一批的前提下可行，"
                        "可换一个新月份单独验证"
                    ),
                    "value": EX_BATCH_2,
                },
                "combined_conflict": {
                    "summary": "合并冲突：与已确认合计互斥，返回 409",
                    "description": (
                        "先提交第一批后再提交本示例：第 0 行合计 10 与 15 "
                        "互斥，且不存在同一张取整表同时满足两批合计"
                    ),
                    "value": EX_BATCH_COMBINED,
                },
            }
        ),
    ],
):
    # 路径参数与请求体月份以路径为准，不一致直接拒绝，避免误写到别的月份。
    if payload.month != month:
        return JSONResponse(
            status_code=422,
            content={
                "status": "validation_error",
                "reasons": [f"路径月份 {month} 与请求体月份 {payload.month} 不一致"],
            },
        )
    return db.submit_batch(
        month=month,
        row_totals=payload.row_totals,
        col_totals=payload.col_totals,
        grand_total=payload.grand_total,
        batch_label=payload.batch_label,
    )


@app.get(
    "/api/v1/tables",
    response_model=list[MonthSummary],
    tags=["查询"],
    summary="列出已登记的月份及确认进度",
)
def list_tables() -> list[dict]:
    return db.list_months()


@app.get(
    "/api/v1/tables/{month}",
    response_model=MonthlyMatrixResponse,
    responses={404: {"description": "该月份尚未登记矩阵"}},
    tags=["查询"],
    summary="按月查询取整矩阵与格值加出的行列合计、总计",
)
def get_table(month: str) -> dict:
    return db.get_month(month)


@app.get(
    "/api/v1/tables/{month}/confirmed-totals",
    response_model=list[dict],
    responses={404: {"description": "该月份尚未登记矩阵"}},
    tags=["查询"],
    summary="查询某月全部已确认合计",
)
def get_confirmed(month: str) -> list[dict]:
    return db.get_confirmed(month)


@app.get("/method", tags=["运维"], summary="算法说明")
def method() -> dict:
    return {
        "rounding_rule": (
            "格值、行列合计与全表总计都只能取原数向下或向上的最近 5 的"
            "倍数；原数已是 5 的倍数时保持不变"
        ),
        "model": "0-1 变量 x[i][j] 表示该格是否向上取整，B[i][j]=floor5(A[i][j])+5*x[i][j]",
        "feasibility": (
            "行/列/总计的向上取整格数构成带上下界的等式约束，归约为二分图"
            "带下界可行流：source->行（下界/上界）、行->列（容量 0/1，仅余数"
            "非 0 的格有边）、列->sink、sink->source 环流边承载总计约束"
        ),
        "solver": "自实现 Dinic 最大流 + 超级源汇下界转换，容量为整数故流量天然整数",
        "batch_rule": (
            "本批与已确认合计合并后求解；可行才整体提交，无解返回 409 且"
            "已确认合计不变；同一下标的重复相同值按幂等处理"
        ),
        "concurrency": "进程内 RLock 临界区 + SQLite BEGIN IMMEDIATE 写锁串行化",
    }
