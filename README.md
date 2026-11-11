# 医院取整表合计登记 API

病例简报先公布部分乡镇（行）和年龄组（列）的合计，随后发布的月报附表按
**5 的倍数取整**后，格值仍须能加出简报里已公布的数。本服务管理这一过程：

1. 发布员每月登记一份数仓导出的**非负整数计数矩阵**（最多 12×12），
   登记后不可修改；
2. 分批提交简报拟用的行合计、列合计、全表总计（均为 5 的倍数）。
   **只有同一张取整表能同时满足本批与全部已确认合计时，本批才写入
   SQLite**；否则返回冲突，已确认合计原样保留；
3. 按月查询时返回符合已确认合计的取整矩阵，以及**从格值真实加出的**
   行合计、列合计与全表总计。

## 取整规则

格值、每行合计、每列合计、全表总计都只能取原数**向下或向上最近的 5 的
倍数**；原数本身已是 5 的倍数时保持不变（上下界重合）。例如：

| 原数 | 合法取值 |
| --- | --- |
| 5、10、20 | 仅自身 |
| 6、7、9 | 5 或 10 |
| 14 | 10 或 15 |

## 目录结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI 路由、异常处理、Swagger 示例
│   ├── schemas.py     # Pydantic 模型与内置样例（原表 + 两批合计）
│   ├── solver.py      # 带下界可行流（Dinic）取整可行性求解
│   └── db.py          # SQLite 持久化、批次事务与并发控制
├── tests/
│   ├── test_solver.py # 含 40 组随机矩阵 × 8 组约束的暴力枚举交叉验证
│   └── test_api.py    # 登记/批次/冲突/幂等/并发/查询/Swagger
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── requirements-dev.txt
```

## 启动（Docker Compose）

```bash
docker compose up --build
```

- Swagger UI：<http://localhost:8000/docs>（可直接选用内置示例提交）
- ReDoc：<http://localhost:8000/redoc>
- 健康检查：`GET /health`
- SQLite 数据文件位于命名卷 `rounding-data` 内的 `/data/rounding.db`，
  可通过环境变量 `ROUNDING_DB_PATH` 覆盖。

停止：`docker compose down`（数据卷保留；加 `-v` 同时删除数据）。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/tables` | 登记某月原始矩阵（201）；重复登记返回 409 |
| POST | `/api/v1/tables/{month}/batches` | 提交一批合计；可行写入（200），冲突 409 |
| GET | `/api/v1/tables/{month}` | 取整矩阵 + 格值加出的行/列合计与总计 |
| GET | `/api/v1/tables/{month}/confirmed-totals` | 某月全部已确认合计 |
| GET | `/api/v1/tables` | 已登记月份及确认进度 |
| GET | `/method` | 算法说明 |

### 登记矩阵

```json
POST /api/v1/tables
{
  "month": "2026-10",
  "row_names": ["青山镇", "河口乡"],
  "col_names": ["0-17岁", "18-59岁"],
  "matrix": [[6, 5], [5, 7]]
}
```

响应同时给出每行、每列、总计的合法取整区间，便于发布员核对拟用合计：

```json
{
  "status": "registered",
  "legal_row_ranges": [[10, 15], [10, 15]],
  "legal_col_ranges": [[10, 15], [10, 15]],
  "legal_grand_range": [20, 25]
}
```

### 分批确认合计

请求中数组长度与矩阵尺寸一致，`null` 表示本批不涉及该合计：

```json
POST /api/v1/tables/2026-10/batches
{
  "month": "2026-10",
  "row_totals": [15, null],
  "col_totals": [null, null],
  "grand_total": null,
  "batch_label": "第一批：青山镇行合计向上取整"
}
```

可行时返回批次号、新增确认项、当前全部已确认合计，以及一张同时满足全部
约束的**见证取整矩阵**与其行/列/总计。

冲突响应（HTTP 409，本批不写入，已确认合计不变）：

```json
{
  "status": "conflict",
  "month": "2026-10",
  "reasons": ["不存在同一张取整表能同时满足本批与已确认的全部合计：带下界可行流检验未通过（最大流 1 < 所需流量 2）"],
  "confirmed_totals": [{"kind": "row", "index": 0, "name": "青山镇", "value": 15}]
}
```

两类冲突都会被拦截：

- **不可更改冲突**：同一行/列/总计已确认的值与本批不同，直接 409；
- **结构冲突**：不存在任何取整表同时满足合并后的全部合计，可行流检验
  返回 409。

同一下标重复提交相同值按**幂等**处理（接受、批次号递增、无新增确认项）。

### 按月查询

```json
GET /api/v1/tables/2026-10
{
  "status": "ok",
  "original_matrix": [[6, 5], [5, 7]],
  "rounded_matrix": [[10, 5], [5, 5]],
  "row_totals": [15, 10],
  "col_totals": [15, 10],
  "grand_total": 25,
  "confirmed_totals": [{"kind": "row", "index": 0, "name": "青山镇", "value": 15}]
}
```

`row_totals`、`col_totals`、`grand_total` 均由 `rounded_matrix` 的格值
现场加出，不做任何独立存储或推算。

状态码约定：

| 场景 | 状态码 |
| --- | --- |
| 登记成功 / 批次接受 / 查询成功 | 201 / 200 / 200 |
| 月份未登记 | 404 |
| 合计冲突（含篡改已确认值、结构无解）、重复登记 | 409 |
| 请求结构/类型错误（超 12×12、负数、非 5 倍数、布尔、尺寸不符、月份格式错） | 422 |

## Swagger 内置样例

Swagger 中备有一份原表和两批合计样例，**两批各自可行、合在一起冲突**：

- 原表 `[[6,5],[5,7]]`：可向上取整的格只有 `(0,0)=6` 与 `(1,1)=7`，
  原总计 23 的合法区间为 `[20,25]`，全表最多 1 格向上；
- 第一批：青山镇行合计 15（要求格 `(0,0)` 向上），单独可行；
- 第二批：18-59 岁列合计 15（要求格 `(1,1)` 向上），单独可行；
- 合并样例：两个要求并存需要 2 格向上，超出总计允许的 1 格，返回 409。

## 算法：带下界可行流

令 `F[i][j] = floor5(A[i][j])`，引入 0-1 变量 `x[i][j]`（余数为 0 的格
固定为 0）：

```
B[i][j] = F[i][j] + 5 · x[i][j]
```

每行合计只能是该行原合计的 floor5/ceil5，因此该行“向上取整的格子数”
`k_i` 至多取两个相邻整数值；列与全表同理。把约束建成二分图网络：

- `source → 行节点`：容量区间 `[k_min_i, k_max_i]`，确认某行合计时
  缩成固定值；
- `行节点 → 列节点`：仅余数非 0 的格有边，容量 `[0,1]`；
- `列节点 → sink`：列的向上取整格数区间，确认时缩成固定值；
- `sink → source` 环流边：承载全表总计的向上取整格数区间。

带下界边按标准手法（超级源/超级汇 + 节点平衡量）转换为普通最大流，用
自实现的 **Dinic** 判定可行性；容量全为整数，最大流天然给出整数流，
读取行列边流量即可恢复完整取整矩阵。输出前另有一轮独立复核，逐格、逐
行、逐列、总计重新校验取整区间与确认值。

## 并发控制

服务以单容器多线程方式运行，并发提交分两层串行化：

1. 进程内 `threading.RLock` 把“读取已确认合计 → 可行性求解 → 写入”
   临界区互斥；
2. SQLite 事务以 `BEGIN IMMEDIATE` 立即获取写锁，并设置
   `busy_timeout=30s`；若未来多进程共享同一数据库文件，写事务同样被
   数据库层串行化。

因此互相冲突的合计无法被两个并发请求同时确认（测试中以双线程屏障
模拟，断言结果必为一成一败）。

## 本地开发（无 Docker）

```bash
python3 -m venv --without-pip .venv        # 若系统无 ensurepip
.venv/bin/python get-pip.py
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
ROUNDING_DB_PATH=/tmp/rounding.db \
  .venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/python -m pytest tests -q
```

测试集包含 40 组随机 1×1 至 3×3 矩阵、每组 8 组随机约束的**暴力枚举
交叉验证**：求解器的可行/无解判定与枚举全部逐格取整组合的结论完全
一致，并覆盖零矩阵、全 5 倍数矩阵、12×12、非法约束与各类 422/409
场景。
