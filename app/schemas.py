"""请求/响应 Pydantic 模型与 Swagger 示例。

内置样例（Swagger UI 可直接选用）：

* ``EX_TABLE``：2x2 原表，每月登记一次、登记后不可修改；
* ``EX_BATCH_1`` / ``EX_BATCH_2``：两批简报合计，**各自都存在满足它的
  取整表**（单独提交均会被接受）；
* ``EX_BATCH_COMBINED``：把两批合计合并到一起，同一张取整表无法同时
  满足，提交返回 409 冲突。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator

MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# ---------------------------------------------------------------------------
# Swagger 样例数据
# ---------------------------------------------------------------------------

# 原表（数仓导出的非负整数计数矩阵；行=乡镇，列=年龄组）。
# 可向上取整的格只有 (0,0)=6 与 (1,1)=7；(0,1)=5、(1,0)=5 已是 5 的倍数。
# 原总计 23，合法取整区间 [20,25]，即全表最多 1 格向上取整。
EX_TABLE: dict = {
    "month": "2026-10",
    "row_names": ["青山镇", "河口乡"],
    "col_names": ["0-17岁", "18-59岁"],
    "matrix": [
        [6, 5],
        [5, 7],
    ],
}

# 第一批合计：青山镇行合计向上取整为 15（原合计 11，区间 [10,15]），
# 只有格 (0,0) 向上能满足。单独可行：[[10,5],[5,5]]。
EX_BATCH_1: dict = {
    "month": "2026-10",
    "row_totals": [15, None],
    "col_totals": [None, None],
    "grand_total": None,
    "batch_label": "第一批：青山镇行合计向上取整",
}

# 第二批合计：18-59 岁列合计向上取整为 15（原合计 12，区间 [10,15]），
# 只有格 (1,1) 向上能满足。单独可行：[[5,5],[5,10]]。
EX_BATCH_2: dict = {
    "month": "2026-10",
    "row_totals": [None, None],
    "col_totals": [None, 15],
    "grand_total": None,
    "batch_label": "第二批：18-59岁列合计向上取整",
}

# 合并样例：同时要求格 (0,0) 与格 (1,1) 向上，但全表总计只允许 1 格向上
# （总计 23 的合法区间为 [20,25]），任何取整表都无法同时满足，返回 409。
EX_BATCH_COMBINED: dict = {
    "month": "2026-10",
    "row_totals": [15, None],
    "col_totals": [None, 15],
    "grand_total": None,
    "batch_label": "合并样例：两批合计互斥（演示 409）",
}


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class RegisterTableRequest(BaseModel):
    """每月登记一次的数仓导出矩阵（登记后不可修改）。"""

    month: str = Field(..., description="所属月份，格式 YYYY-MM", examples=["2026-10"])
    matrix: list[list[StrictInt]] = Field(
        ..., description="非负整数计数矩阵，乡镇为行、年龄组为列，最多 12x12"
    )
    row_names: list[str] | None = Field(
        default=None, description="乡镇名称（可选），长度须等于行数；缺省为 row_0、row_1……"
    )
    col_names: list[str] | None = Field(
        default=None, description="年龄组名称（可选），长度须等于列数；缺省为 col_0、col_1……"
    )

    @field_validator("month")
    @classmethod
    def _validate_month(cls, value: str) -> str:
        if not MONTH_PATTERN.match(value):
            raise ValueError("month 必须是 YYYY-MM 格式且月份在 01..12 之间")
        return value

    @field_validator("matrix")
    @classmethod
    def _validate_cells(cls, value: list[list[int]]) -> list[list[int]]:
        if not value:
            raise ValueError("matrix 至少要包含一行")
        if len(value) > 12:
            raise ValueError("matrix 行数不能超过 12")
        width = len(value[0])
        if width == 0 or width > 12:
            raise ValueError("matrix 列数须在 1..12 之间")
        for i, row in enumerate(value):
            if len(row) != width:
                raise ValueError(f"第 {i} 行长度与首行不一致（应为 {width}）")
            for j, cell in enumerate(row):
                if isinstance(cell, bool):
                    raise ValueError(f"matrix[{i}][{j}] 不能是布尔值，须为整数")
                if cell < 0:
                    raise ValueError(f"matrix[{i}][{j}] 不能为负数：{cell}")
        return value

    @model_validator(mode="after")
    def _validate_names(self) -> "RegisterTableRequest":
        rows = len(self.matrix)
        cols = len(self.matrix[0])
        if self.row_names is not None:
            if len(self.row_names) != rows:
                raise ValueError(f"row_names 长度须等于行数 {rows}")
            if any(not name.strip() for name in self.row_names):
                raise ValueError("row_names 不能包含空字符串")
        if self.col_names is not None:
            if len(self.col_names) != cols:
                raise ValueError(f"col_names 长度须等于列数 {cols}")
            if any(not name.strip() for name in self.col_names):
                raise ValueError("col_names 不能包含空字符串")
        return self


class BatchTotalsRequest(BaseModel):
    """一批简报拟用的行/列/总计合计；null 表示本批不涉及该合计。"""

    month: str = Field(..., description="目标月份，格式 YYYY-MM")
    row_totals: list[StrictInt | None] = Field(
        ..., description="行合计（5 的倍数），长度须等于矩阵行数；null 表示本批不确认该行"
    )
    col_totals: list[StrictInt | None] = Field(
        ..., description="列合计（5 的倍数），长度须等于矩阵列数；null 表示本批不确认该列"
    )
    grand_total: StrictInt | None = Field(
        default=None, description="全表总计（5 的倍数）；null 表示本批不确认总计"
    )
    batch_label: str | None = Field(
        default=None, description="本批备注（可选），仅用于审计展示"
    )

    @field_validator("month")
    @classmethod
    def _validate_month(cls, value: str) -> str:
        if not MONTH_PATTERN.match(value):
            raise ValueError("month 必须是 YYYY-MM 格式且月份在 01..12 之间")
        return value

    @field_validator("row_totals", "col_totals")
    @classmethod
    def _validate_total_list(cls, value: list[int | None]) -> list[int | None]:
        if not value:
            raise ValueError("合计数组不能为空")
        if len(value) > 12:
            raise ValueError("合计数组长度不能超过 12")
        for i, cell in enumerate(value):
            if cell is None:
                continue
            if isinstance(cell, bool):
                raise ValueError(f"位置 {i} 的合计不能是布尔值")
            if cell < 0:
                raise ValueError(f"位置 {i} 的合计不能为负数：{cell}")
            if cell % 5 != 0:
                raise ValueError(f"位置 {i} 的合计必须是 5 的倍数：{cell}")
        return value

    @field_validator("grand_total")
    @classmethod
    def _validate_grand(cls, value: int | None) -> int | None:
        if value is None:
            return value
        if isinstance(value, bool):
            raise ValueError("grand_total 不能是布尔值")
        if value < 0:
            raise ValueError(f"grand_total 不能为负数：{value}")
        if value % 5 != 0:
            raise ValueError(f"grand_total 必须是 5 的倍数：{value}")
        return value

    model_config = {
        "json_schema_extra": {
            "examples": [EX_BATCH_1, EX_BATCH_2, EX_BATCH_COMBINED]
        }
    }


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------

class Dimensions(BaseModel):
    rows: int
    cols: int


class RegisteredTableResponse(BaseModel):
    status: Literal["registered"]
    month: str
    dimensions: Dimensions
    row_names: list[str]
    col_names: list[str]
    matrix: list[list[int]] = Field(..., description="登记入库的原始矩阵（不可修改）")
    legal_row_ranges: list[list[int]] = Field(
        ..., description="每行合计的合法取整区间 [下界, 上界]"
    )
    legal_col_ranges: list[list[int]] = Field(
        ..., description="每列合计的合法取整区间 [下界, 上界]"
    )
    legal_grand_range: list[int] = Field(
        ..., description="全表总计的合法取整区间 [下界, 上界]"
    )


class ConfirmedItem(BaseModel):
    kind: Literal["row", "col", "grand"]
    index: int = Field(..., description="行/列下标（从 0 开始）；总计固定为 0")
    name: str = Field(..., description="行/列名称；总计为 grand_total")
    value: int


class BatchAcceptedResponse(BaseModel):
    status: Literal["accepted"]
    month: str
    batch_no: int = Field(..., description="本批在该月份下的序号（从 1 开始）")
    newly_confirmed: list[ConfirmedItem] = Field(
        ..., description="本批新写入的合计；与已确认值相同的合计幂等忽略，不在此列"
    )
    confirmed_totals: list[ConfirmedItem] = Field(
        ..., description="写入后该月份全部已确认合计"
    )
    witness_matrix: list[list[int]] = Field(
        ..., description="同时满足全部已确认合计的一张取整矩阵"
    )
    witness_row_totals: list[int] = Field(..., description="见证矩阵的行合计")
    witness_col_totals: list[int] = Field(..., description="见证矩阵的列合计")
    witness_grand_total: int = Field(..., description="见证矩阵的全表总计")


class ConflictResponse(BaseModel):
    status: Literal["conflict"]
    month: str
    reasons: list[str] = Field(
        ..., description="冲突的文字说明；本批不会写入，已确认合计保持不变"
    )
    confirmed_totals: list[ConfirmedItem] = Field(
        ..., description="当前仍然有效的已确认合计"
    )


class MonthlyMatrixResponse(BaseModel):
    status: Literal["ok"]
    month: str
    dimensions: Dimensions
    row_names: list[str]
    col_names: list[str]
    original_matrix: list[list[int]] = Field(..., description="登记的原始整数矩阵")
    rounded_matrix: list[list[int]] = Field(
        ..., description="符合全部已确认合计的一张取整矩阵（每格为最近 5 倍数）"
    )
    row_totals: list[int] = Field(..., description="从取整矩阵格值加出的行合计")
    col_totals: list[int] = Field(..., description="从取整矩阵格值加出的列合计")
    grand_total: int = Field(..., description="从取整矩阵格值加出的全表总计")
    confirmed_totals: list[ConfirmedItem] = Field(
        ..., description="当前已确认合计；无确认时取整矩阵仅满足取整规则"
    )


class MonthSummary(BaseModel):
    month: str
    dimensions: Dimensions
    confirmed_count: int = Field(..., description="已确认合计条目数")
    batches: int = Field(..., description="已接受的批次数")


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
