"""solver 单元测试：取整规则、样例批次与暴力枚举交叉验证。"""

from __future__ import annotations

import itertools
import random

import pytest

from app import solver
from app.solver import COL, GRAND, ROW
from app.schemas import (
    EX_BATCH_1,
    EX_BATCH_2,
    EX_BATCH_COMBINED,
    EX_TABLE,
)


# ---------------------------------------------------------------------------
# 取整基础函数
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value, low, high",
    [
        (0, 0, 0),
        (5, 5, 5),
        (10, 10, 10),
        (6, 5, 10),
        (9, 5, 10),
        (12, 10, 15),
        (14, 10, 15),
        (101, 100, 105),
    ],
)
def test_floor_ceil(value, low, high):
    assert solver.floor5(value) == low
    assert solver.ceil5(value) == high
    # 5 的倍数原样保留
    if value % 5 == 0:
        assert solver.floor5(value) == solver.ceil5(value) == value


# ---------------------------------------------------------------------------
# Swagger 样例：两批各自可行、合在一起冲突
# ---------------------------------------------------------------------------

def _batch_constraints(batch: dict) -> dict:
    constraints: dict = {}
    for i, value in enumerate(batch["row_totals"]):
        if value is not None:
            constraints[(ROW, i)] = value
    for j, value in enumerate(batch["col_totals"]):
        if value is not None:
            constraints[(COL, j)] = value
    if batch.get("grand_total") is not None:
        constraints[(GRAND, 0)] = batch["grand_total"]
    return constraints


def test_sample_batches_each_feasible_but_combined_conflict():
    matrix = EX_TABLE["matrix"]

    b1 = _batch_constraints(EX_BATCH_1)
    b2 = _batch_constraints(EX_BATCH_2)

    rounded_1, reasons_1 = solver.solve(matrix, b1)
    assert reasons_1 == [] and rounded_1 is not None
    _assert_matrix_valid(matrix, rounded_1)
    _assert_constraints_met(rounded_1, b1)

    rounded_2, reasons_2 = solver.solve(matrix, b2)
    assert reasons_2 == [] and rounded_2 is not None
    _assert_matrix_valid(matrix, rounded_2)
    _assert_constraints_met(rounded_2, b2)

    combined, reasons_3 = solver.solve(matrix, {**b1, **b2})
    assert combined is None
    assert reasons_3  # 有结构化冲突原因


def test_combined_example_conflicts_after_batch_one():
    matrix = EX_TABLE["matrix"]
    b1 = _batch_constraints(EX_BATCH_1)
    # Swagger 合并样例：在第一批（行 0 向上）之上再要求列 1 向上
    merged = {**b1, **_batch_constraints(EX_BATCH_COMBINED)}
    rounded, reasons = solver.solve(matrix, merged)
    assert rounded is None
    assert reasons
    assert any("可行流" in r for r in reasons)


# ---------------------------------------------------------------------------
# 无约束时总可行，且矩阵完全合规
# ---------------------------------------------------------------------------

def test_unconstrained_always_feasible():
    matrix = [
        [6, 7, 3],
        [8, 9, 10],
        [0, 14, 5],
    ]
    rounded, reasons = solver.solve(matrix)
    assert reasons == []
    _assert_matrix_valid(matrix, rounded)


def test_all_multiples_of_five_unchanged():
    matrix = [[5, 10, 0], [15, 20, 25]]
    rounded, reasons = solver.solve(matrix)
    assert reasons == []
    assert rounded == matrix  # 全部已是 5 的倍数，原样保留
    assert solver.solve(matrix, {(ROW, 0): 15, (GRAND, 0): 75})[0] == matrix


def test_zero_matrix():
    matrix = [[0, 0], [0, 0]]
    rounded, reasons = solver.solve(matrix)
    assert reasons == [] and rounded == matrix
    rounded, reasons = solver.solve(matrix, {(GRAND, 0): 0})
    assert reasons == [] and rounded == matrix


def test_illegal_constraint_rejected():
    matrix = [[6, 7]]  # 行原合计 13，合法取整区间 [10, 15]
    _, reasons = solver.solve(matrix, {(ROW, 0): 20})
    assert any("合法取整区间" in r for r in reasons)

    _, reasons = solver.solve(matrix, {(ROW, 0): 12})  # 不是 5 的倍数
    assert any("5 的倍数" in r for r in reasons)

    _, reasons = solver.solve(matrix, {("unknown", 0): 10})
    assert any("未知约束种类" in r for r in reasons)


def test_grand_total_constraint():
    matrix = [[6, 5], [5, 7]]
    # 原总计 23，合法取整区间 [20, 25]
    rounded, reasons = solver.solve(matrix, {(GRAND, 0): 20})
    assert reasons == []
    assert sum(sum(row) for row in rounded) == 20

    rounded, reasons = solver.solve(matrix, {(GRAND, 0): 25})
    assert reasons == []
    assert sum(sum(row) for row in rounded) == 25

    _, reasons = solver.solve(matrix, {(GRAND, 0): 30})
    assert any("合法取整区间" in r for r in reasons)


# ---------------------------------------------------------------------------
# 暴力枚举交叉验证
# ---------------------------------------------------------------------------

def _all_legal_rounded_matrices(matrix):
    """枚举全部逐格取整组合，仅保留行/列/总计也合法的矩阵。"""
    rows, cols = len(matrix), len(matrix[0])
    options = [
        [solver.rounding_options(v) for v in row]
        for row in matrix
    ]
    row_legal = [solver.rounding_options(sum(matrix[i])) for i in range(rows)]
    col_legal = [
        solver.rounding_options(sum(matrix[i][j] for i in range(rows)))
        for j in range(cols)
    ]
    grand_legal = solver.rounding_options(sum(sum(r) for r in matrix))

    choices = [list(opt) for row in options for opt in row]
    legal = []
    for picked in itertools.product(*choices):
        candidate = [
            list(picked[i * cols:(i + 1) * cols]) for i in range(rows)
        ]
        row_sums = [sum(candidate[i]) for i in range(rows)]
        col_sums = [sum(candidate[i][j] for i in range(rows)) for j in range(cols)]
        grand = sum(row_sums)
        if any(not (lo <= s <= hi) for s, (lo, hi) in zip(row_sums, row_legal)):
            continue
        if any(not (lo <= s <= hi) for s, (lo, hi) in zip(col_sums, col_legal)):
            continue
        if not grand_legal[0] <= grand <= grand_legal[1]:
            continue
        legal.append(candidate)
    return legal


def _brute_force_feasible(legal_matrices, constraints):
    for candidate in legal_matrices:
        rows, cols = len(candidate), len(candidate[0])
        row_sums = [sum(candidate[i]) for i in range(rows)]
        col_sums = [sum(candidate[i][j] for i in range(rows)) for j in range(cols)]
        grand = sum(row_sums)
        ok = True
        for (kind, pos), value in constraints.items():
            actual = (
                row_sums[pos] if kind == ROW
                else col_sums[pos] if kind == COL
                else grand
            )
            if actual != value:
                ok = False
                break
        if ok:
            return True
    return False


def _random_constraint_sets(matrix, legal_matrices, rng, count):
    rows, cols = len(matrix), len(matrix[0])
    # 收集每个键在合法矩阵中实际出现过的合计值，保证候选值“看起来可能”。
    achievable = {}
    for candidate in legal_matrices:
        row_sums = [sum(candidate[i]) for i in range(rows)]
        col_sums = [sum(candidate[i][j] for i in range(rows)) for j in range(cols)]
        for i, value in enumerate(row_sums):
            achievable.setdefault((ROW, i), set()).add(value)
        for j, value in enumerate(col_sums):
            achievable.setdefault((COL, j), set()).add(value)
        achievable.setdefault((GRAND, 0), set()).add(sum(row_sums))

    keys = list(achievable)
    for _ in range(count):
        constraints = {}
        for key in keys:
            if rng.random() < 0.5:
                constraints[key] = rng.choice(sorted(achievable[key]))
        yield constraints


def test_brute_force_cross_validation():
    rng = random.Random(20261111)
    checked = 0
    for trial in range(40):
        rows = rng.randint(1, 3)
        cols = rng.randint(1, 3)
        matrix = [[rng.randint(0, 9) for _ in range(cols)] for _ in range(rows)]
        legal = _all_legal_rounded_matrices(matrix)
        assert legal, "取整问题无约束时必然存在合法矩阵"

        # 无约束必须可行
        rounded, reasons = solver.solve(matrix)
        assert reasons == [] and rounded in legal

        for constraints in _random_constraint_sets(matrix, legal, rng, 8):
            expected = _brute_force_feasible(legal, constraints)
            rounded, reasons = solver.solve(matrix, constraints)
            assert (rounded is not None) == expected, (
                f"trial={trial} matrix={matrix} constraints={constraints} "
                f"expected={expected} reasons={reasons}"
            )
            if expected:
                _assert_matrix_valid(matrix, rounded)
                _assert_constraints_met(rounded, constraints)
            checked += 1
    assert checked >= 100


def test_twelve_by_twelve_performance():
    rng = random.Random(42)
    matrix = [[rng.randint(0, 999) for _ in range(12)] for _ in range(12)]
    # 固定前 4 行向上取整，其余行列与总计由网络自行平衡
    constraints = {
        (ROW, i): solver.ceil5(sum(matrix[i]))
        for i in range(4)
        if solver.floor5(sum(matrix[i])) != solver.ceil5(sum(matrix[i]))
    }
    rounded, reasons = solver.solve(matrix, constraints)
    assert reasons == []
    _assert_matrix_valid(matrix, rounded)
    _assert_constraints_met(rounded, constraints)


# ---------------------------------------------------------------------------
# 辅助断言
# ---------------------------------------------------------------------------

def _assert_matrix_valid(original, rounded):
    rows, cols = len(original), len(original[0])
    assert len(rounded) == rows and all(len(r) == cols for r in rounded)
    for i in range(rows):
        for j in range(cols):
            low, high = solver.rounding_options(original[i][j])
            assert low <= rounded[i][j] <= high
            assert rounded[i][j] % 5 == 0
    for i in range(rows):
        lo, hi = solver.rounding_options(sum(original[i]))
        assert lo <= sum(rounded[i]) <= hi
    for j in range(cols):
        lo, hi = solver.rounding_options(
            sum(original[i][j] for i in range(rows))
        )
        assert lo <= sum(rounded[i][j] for i in range(rows)) <= hi
    lo, hi = solver.rounding_options(sum(sum(r) for r in original))
    assert lo <= sum(sum(r) for r in rounded) <= hi


def _assert_constraints_met(rounded, constraints):
    rows, cols = len(rounded), len(rounded[0])
    row_sums = [sum(rounded[i]) for i in range(rows)]
    col_sums = [sum(rounded[i][j] for i in range(rows)) for j in range(cols)]
    grand = sum(row_sums)
    for (kind, pos), value in constraints.items():
        actual = (
            row_sums[pos] if kind == ROW
            else col_sums[pos] if kind == COL
            else grand
        )
        assert actual == value
