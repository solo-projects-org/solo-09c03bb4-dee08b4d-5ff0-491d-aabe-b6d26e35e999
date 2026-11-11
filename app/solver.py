"""取整矩阵可行性求解的核心算法。

问题建模
--------
给定 m x n 非负整数计数矩阵 ``A``（乡镇为行、年龄组为列），每份对外
发布的取整表 ``B`` 必须满足：

1. 格值只能向下或向上取到最近的 5 的倍数：
   ``B[i][j] ∈ {floor5(A[i][j]), ceil5(A[i][j])}``；
   原数已是 5 的倍数时二者相等，该格取值唯一。
2. 每一行合计、每一列合计、全表总计同样只能是对应原合计的向下或向上
   最近 5 的倍数（原合计已是 5 的倍数时唯一）。
3. 发布员分批确认的行/列/总计合计是附加的等式约束。

令余数 ``r[i][j] = A[i][j] mod 5``、基数 ``F[i][j] = floor5(A[i][j])``，
引入 0-1 变量 ``x[i][j]``（余数为 0 的格固定为 0）：

    B[i][j] = F[i][j] + 5 * x[i][j]

第 i 行向上取整的格子数记为 ``k_i``，则该行合计为
``base_i + 5*k_i``（``base_i = Σ_j F[i][j]``）。由“行合计必须是原行
合计的最近 5 倍数”可推出 ``k_i`` 的整数上下界；列与全表同理。于是
问题转化为二分图上的**带下界可行流**：

    source -> 行节点   容量区间 [k_min_i, k_max_i]（被确认时缩为定值）
    行节点 -> 列节点   容量区间 [0, 1]（仅余数非 0 的格有边）
    列节点 -> sink     容量区间 [k_min_j, k_max_j]（被确认时缩为定值）
    sink -> source     容量区间 [k_min_G, k_max_G]（总计约束）

带下界边按标准手法转换为超级源/超级汇的最大流问题，用自实现的
Dinic 判定可行性并读取每条行列边的实际流量，恢复出完整取整矩阵。
容量均为整数，Dinic 给出的最大流天然为整数流。
"""

from __future__ import annotations

from dataclasses import dataclass

ROW = "row"
COL = "col"
GRAND = "grand"

# 约束键：(种类, 位置)；总计只有一个，位置固定为 0。
ConstraintKey = tuple[str, int]


class RoundingError(ValueError):
    """输入本身不合法（如合计不是 5 的倍数）时抛出。"""


# ---------------------------------------------------------------------------
# 取整基础函数
# ---------------------------------------------------------------------------

def floor5(value: int) -> int:
    """向下取到最近的 5 的倍数。"""
    return (value // 5) * 5


def ceil5(value: int) -> int:
    """向上取到最近的 5 的倍数；已是 5 的倍数时原值保留。"""
    quotient, remainder = divmod(value, 5)
    return value if remainder == 0 else (quotient + 1) * 5


def rounding_options(value: int) -> tuple[int, int]:
    """返回单格/单合计的合法取整区间 (下界, 上界)，均为 5 的倍数。"""
    return floor5(value), ceil5(value)


# ---------------------------------------------------------------------------
# Dinic 最大流
# ---------------------------------------------------------------------------

class _Edge:
    __slots__ = ("to", "rev", "cap")

    def __init__(self, to: int, rev: int, cap: int):
        self.to = to
        self.rev = rev
        self.cap = cap


class _Dinic:
    def __init__(self, n: int):
        self.g: list[list[_Edge]] = [[] for _ in range(n)]
        self.level = [-1] * n
        self.it = [0] * n

    def add_edge(self, fr: int, to: int, cap: int) -> _Edge:
        forward = _Edge(to, len(self.g[to]), cap)
        backward = _Edge(fr, len(self.g[fr]), 0)
        self.g[fr].append(forward)
        self.g[to].append(backward)
        return forward
    def _bfs(self, s: int, t: int) -> bool:
        self.level = [-1] * len(self.g)
        self.level[s] = 0
        queue = [s]
        head = 0
        while head < len(queue):
            v = queue[head]
            head += 1
            for e in self.g[v]:
                if e.cap > 0 and self.level[e.to] < 0:
                    self.level[e.to] = self.level[v] + 1
                    queue.append(e.to)
        return self.level[t] >= 0

    def _dfs(self, v: int, t: int, f: int) -> int:
        if v == t:
            return f
        while self.it[v] < len(self.g[v]):
            e = self.g[v][self.it[v]]
            if e.cap > 0 and self.level[e.to] == self.level[v] + 1:
                d = self._dfs(e.to, t, min(f, e.cap))
                if d > 0:
                    e.cap -= d
                    self.g[e.to][e.rev].cap += d
                    return d
            self.it[v] += 1
        return 0

    def max_flow(self, s: int, t: int, limit: int) -> int:
        flow = 0
        while flow < limit and self._bfs(s, t):
            self.it = [0] * len(self.g)
            while flow < limit:
                f = self._dfs(s, t, limit - flow)
                if f == 0:
                    break
                flow += f
        return flow


# ---------------------------------------------------------------------------
# 输入校验与区间计算
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _LineSpec:
    """一行/一列/总计的向上取整计数区间。"""

    base: int       # 各格 floor5 之和（5 的倍数）
    k_min: int      # 满足合计下界所需的最少向上取整格数
    k_max: int      # 满足合计上界允许的最多向上取整格数
    low_total: int  # 合法合计下界 floor5(原合计)
    high_total: int  # 合法合计上界 ceil5(原合计)


def _line_spec(base: int, residue_sum: int, non_zero_cells: int,
               original_sum: int) -> _LineSpec:
    """计算一行/一列/总计的向上取整格数区间。

    合计只能是原合计的 floor5 或 ceil5：

    * ``base + 5*k_min = floor5(原合计)``，故 ``k_min = 余数和 // 5``；
    * 余数和不是 5 的倍数时允许再向上一格，``k_max = k_min + 1``，对应
      ``ceil5(原合计)``；余数和恰为 5 的倍数时上下界重合（合计唯一）；
    * ``non_zero_cells`` 仅用于防御性断言：``k_max`` 不可能超过可用的
      余数非 0 格数。
    """
    low_total, high_total = rounding_options(original_sum)
    k_min = residue_sum // 5
    k_max = k_min + (1 if residue_sum % 5 else 0)
    if k_max > non_zero_cells:
        raise RoundingError(
            f"向上取整格数上界 {k_max} 超过余数非 0 格数 {non_zero_cells}"
        )
    return _LineSpec(base, k_min, k_max, low_total, high_total)


def validate_matrix(matrix: list[list[int]]) -> tuple[int, int]:
    """独立调用 solver 时的防御性尺寸与类型校验。"""
    if not matrix or not matrix[0]:
        raise RoundingError("matrix 不能为空矩阵")
    rows = len(matrix)
    cols = len(matrix[0])
    if rows > 12 or cols > 12:
        raise RoundingError("matrix 尺寸不能超过 12x12")
    if any(len(row) != cols for row in matrix):
        raise RoundingError("matrix 各行长度不一致")
    for i, row in enumerate(matrix):
        for j, value in enumerate(row):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RoundingError(f"matrix[{i}][{j}] 必须是非负整数")
    return rows, cols


def _check_constraint_value(key: ConstraintKey, value: object, spec: _LineSpec,
                            label: str) -> list[str]:
    """校验单个确认合计，返回冲突原因列表（空列表表示通过）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return [f"{label}合计必须是整数，收到 {value!r}"]
    if value < 0:
        return [f"{label}合计不能为负数：{value}"]
    if value % 5 != 0:
        return [f"{label}合计必须是 5 的倍数：{value}"]
    if not spec.low_total <= value <= spec.high_total:
        return [
            f"{label}合计 {value} 超出该合计的合法取整区间 "
            f"[{spec.low_total}, {spec.high_total}]（原合计只能向下或向上 "
            "取到最近的 5 的倍数）"
        ]
    return []


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def solve(
    matrix: list[list[int]],
    constraints: dict[ConstraintKey, int] | None = None,
) -> tuple[list[list[int]] | None, list[str]]:
    """判定约束是否可行，可行时返回一张满足全部要求的取整矩阵。

    :param matrix: 原始非负整数计数矩阵（最多 12x12）
    :param constraints: 确认合计映射，键为 ``("row", i)`` / ``("col", j)``
        / ``("grand", 0)``，值为 5 的倍数合计
    :returns: ``(取整矩阵, [])`` 可行；``(None, 冲突原因列表)`` 无解
    """
    constraints = constraints or {}
    rows, cols = validate_matrix(matrix)

    floors = [[floor5(v) for v in row] for row in matrix]
    residues = [[v % 5 for v in row] for row in matrix]

    row_base = [sum(floors[i]) for i in range(rows)]
    col_base = [sum(floors[i][j] for i in range(rows)) for j in range(cols)]
    row_res = [sum(residues[i]) for i in range(rows)]
    col_res = [sum(residues[i][j] for i in range(rows)) for j in range(cols)]
    original_rows = [sum(matrix[i]) for i in range(rows)]
    original_cols = [sum(matrix[i][j] for i in range(rows)) for j in range(cols)]
    grand_base = sum(row_base)
    grand_res = sum(row_res)
    original_grand = sum(original_rows)

    row_nonzero = [
        sum(1 for j in range(cols) if residues[i][j] > 0) for i in range(rows)
    ]
    col_nonzero = [
        sum(1 for i in range(rows) if residues[i][j] > 0) for j in range(cols)
    ]
    grand_nonzero = sum(row_nonzero)

    row_specs = [
        _line_spec(row_base[i], row_res[i], row_nonzero[i], original_rows[i])
        for i in range(rows)
    ]
    col_specs = [
        _line_spec(col_base[j], col_res[j], col_nonzero[j], original_cols[j])
        for j in range(cols)
    ]
    grand_spec = _line_spec(
        grand_base, grand_res, grand_nonzero, original_grand
    )
    # 约束 -> 固定的向上取整格数；同时收集非法约束原因。
    fixed_row: dict[int, int] = {}
    fixed_col: dict[int, int] = {}
    fixed_grand: int | None = None
    reasons: list[str] = []

    for (kind, pos), value in constraints.items():
        if kind == ROW:
            if not 0 <= pos < rows:
                reasons.append(f"行合计下标 {pos} 超出矩阵范围（共 {rows} 行）")
                continue
            label = f"第 {pos} 行"
            spec = row_specs[pos]
            errs = _check_constraint_value((kind, pos), value, spec, label)
            if errs:
                reasons.extend(errs)
                continue
            fixed_row[pos] = (value - spec.base) // 5
        elif kind == COL:
            if not 0 <= pos < cols:
                reasons.append(f"列合计下标 {pos} 超出矩阵范围（共 {cols} 列）")
                continue
            label = f"第 {pos} 列"
            spec = col_specs[pos]
            errs = _check_constraint_value((kind, pos), value, spec, label)
            if errs:
                reasons.extend(errs)
                continue
            fixed_col[pos] = (value - spec.base) // 5
        elif kind == GRAND:
            if pos != 0:
                reasons.append(f"总计约束位置只能是 0，收到 {pos}")
                continue
            errs = _check_constraint_value((kind, pos), value, grand_spec, "全表")
            if errs:
                reasons.extend(errs)
                continue
            fixed_grand = (value - grand_spec.base) // 5
        else:
            reasons.append(f"未知约束种类：{kind!r}")

    if reasons:
        return None, reasons

    # ------------------------------------------------------------------
    # 构造带下界可行流网络
    # ------------------------------------------------------------------
    source = 0
    row_node = lambda i: 1 + i
    col_node = lambda j: 1 + rows + j
    sink = 1 + rows + cols
    super_source = sink + 1
    super_sink = sink + 2
    dinic = _Dinic(super_sink + 1)
    # balance[v] = 入边下界之和 - 出边下界之和
    balance = [0] * (super_sink + 1)
    # 记录每条 行->列 边的前向边对象与转换后容量，供读取实际流量。
    cell_edges: dict[tuple[int, int], tuple[_Edge, int]] = {}

    def add_lb_edge(fr: int, to: int, lower: int, upper: int) -> _Edge:
        if upper < lower:
            # 理论上不会发生，兜底下界合法性。
            raise RoundingError(
                f"边 {fr}->{to} 的下界 {lower} 大于上界 {upper}"
            )
        edge = dinic.add_edge(fr, to, upper - lower)
        balance[fr] -= lower
        balance[to] += lower
        return edge

    # source -> 行
    for i in range(rows):
        if i in fixed_row:
            k = fixed_row[i]
            add_lb_edge(source, row_node(i), k, k)
        else:
            add_lb_edge(
                source, row_node(i), row_specs[i].k_min, row_specs[i].k_max
            )

    # 行 -> 列（余数为 0 的格无法向上取整，不建边）
    for i in range(rows):
        for j in range(cols):
            if residues[i][j] > 0:
                edge = add_lb_edge(row_node(i), col_node(j), 0, 1)
                cell_edges[(i, j)] = (edge, 1)

    # 列 -> sink
    for j in range(cols):
        if j in fixed_col:
            k = fixed_col[j]
            add_lb_edge(col_node(j), sink, k, k)
        else:
            add_lb_edge(
                col_node(j), sink, col_specs[j].k_min, col_specs[j].k_max
            )

    # sink -> source：总计约束（环流边）
    if fixed_grand is not None:
        add_lb_edge(sink, source, fixed_grand, fixed_grand)
    else:
        add_lb_edge(sink, source, grand_spec.k_min, grand_spec.k_max)

    required = 0
    for v, bal in enumerate(balance):
        if bal > 0:
            dinic.add_edge(super_source, v, bal)
            required += bal
        elif bal < 0:
            dinic.add_edge(v, super_sink, -bal)

    if required > 0:
        flow = dinic.max_flow(super_source, super_sink, required)
        if flow < required:
            return None, [
                "不存在同一张取整表能同时满足本批与已确认的全部合计："
                f"带下界可行流检验未通过（最大流 {flow} < 所需流量 {required}）"
            ]

    # ------------------------------------------------------------------
    # 读取流量并恢复取整矩阵
    # ------------------------------------------------------------------
    rounded = [row[:] for row in floors]
    for (i, j), (edge, original_cap) in cell_edges.items():
        ups = original_cap - edge.cap  # 该边实际流量（下界为 0）
        # 防御性检查：流量只能是 0/1。
        if ups not in (0, 1):
            return None, [f"内部错误：格 ({i},{j}) 取得非二元流量 {ups}"]
        rounded[i][j] = floors[i][j] + 5 * ups

    problems = _verify(matrix, rounded, constraints, row_specs, col_specs,
                       grand_spec, fixed_row, fixed_col, fixed_grand)
    if problems:
        return None, problems
    return rounded, []


def _verify(
    matrix: list[list[int]],
    rounded: list[list[int]],
    constraints: dict[ConstraintKey, int],
    row_specs: list[_LineSpec],
    col_specs: list[_LineSpec],
    grand_spec: _LineSpec,
    fixed_row: dict[int, int],
    fixed_col: dict[int, int],
    fixed_grand: int | None,
) -> list[str]:
    """输出前复核：格值合法、行列总计均为原合计的最近 5 倍数、约束满足。"""
    rows = len(matrix)
    cols = len(matrix[0])
    problems: list[str] = []

    for i in range(rows):
        for j in range(cols):
            low, high = rounding_options(matrix[i][j])
            if not low <= rounded[i][j] <= high or rounded[i][j] % 5 != 0:
                problems.append(
                    f"内部校验失败：格 ({i},{j}) 取整值 {rounded[i][j]} "
                    f"不在合法区间 [{low}, {high}]"
                )

    for i in range(rows):
        total = sum(rounded[i])
        spec = row_specs[i]
        if not spec.low_total <= total <= spec.high_total:
            problems.append(
                f"内部校验失败：第 {i} 行合计 {total} 超出合法区间 "
                f"[{spec.low_total}, {spec.high_total}]"
            )
        if i in fixed_row and (ROW, i) in constraints and total != constraints[(ROW, i)]:
            problems.append(
                f"内部校验失败：第 {i} 行合计 {total} 不等于确认值 "
                f"{constraints[(ROW, i)]}"
            )

    for j in range(cols):
        total = sum(rounded[i][j] for i in range(rows))
        spec = col_specs[j]
        if not spec.low_total <= total <= spec.high_total:
            problems.append(
                f"内部校验失败：第 {j} 列合计 {total} 超出合法区间 "
                f"[{spec.low_total}, {spec.high_total}]"
            )
        if j in fixed_col and (COL, j) in constraints and total != constraints[(COL, j)]:
            problems.append(
                f"内部校验失败：第 {j} 列合计 {total} 不等于确认值 "
                f"{constraints[(COL, j)]}"
            )

    grand = sum(sum(row) for row in rounded)
    if not grand_spec.low_total <= grand <= grand_spec.high_total:
        problems.append(
            f"内部校验失败：全表总计 {grand} 超出合法区间 "
            f"[{grand_spec.low_total}, {grand_spec.high_total}]"
        )
    if fixed_grand is not None and (GRAND, 0) in constraints:
        if grand != constraints[(GRAND, 0)]:
            problems.append(
                f"内部校验失败：全表总计 {grand} 不等于确认值 "
                f"{constraints[(GRAND, 0)]}"
            )
    return problems


def legal_totals(matrix: list[list[int]]) -> dict:
    """给出每张表各行、列、总计的合法取整区间（供查询与排错展示）。"""
    rows, cols = validate_matrix(matrix)
    row_ranges = [rounding_options(sum(matrix[i])) for i in range(rows)]
    col_ranges = [
        rounding_options(sum(matrix[i][j] for i in range(rows))) for j in range(cols)
    ]
    grand_range = rounding_options(sum(sum(row) for row in matrix))
    return {
        "row_ranges": row_ranges,
        "col_ranges": col_ranges,
        "grand_range": grand_range,
    }
