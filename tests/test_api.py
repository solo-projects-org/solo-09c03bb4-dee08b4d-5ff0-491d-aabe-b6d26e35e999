"""FastAPI 层测试：登记、批次确认、冲突、幂等、并发与查询。"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.schemas import EX_BATCH_1, EX_BATCH_2, EX_BATCH_COMBINED, EX_TABLE
from app.solver import ROW

MONTH = "2026-10"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_rounding.db"
    monkeypatch.setenv("ROUNDING_DB_PATH", str(db_path))
    db.init_db()
    with TestClient(app) as c:
        yield c


def _register(client, month=MONTH, payload=None):
    body = dict(EX_TABLE) if payload is None else payload
    body["month"] = month
    return client.post("/api/v1/tables", json=body)


def _batch(client, month, rows=None, cols=None, grand=None, label=None):
    return client.post(
        f"/api/v1/tables/{month}/batches",
        json={
            "month": month,
            "row_totals": rows,
            "col_totals": cols,
            "grand_total": grand,
            "batch_label": label,
        },
    )


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------

def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "rounding-totals"}


def test_register_and_legal_ranges(client):
    resp = _register(client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "registered"
    assert body["dimensions"] == {"rows": 2, "cols": 2}
    assert body["row_names"] == ["青山镇", "河口乡"]
    # 行原合计 11、12 -> [10,15]、[10,15]
    assert body["legal_row_ranges"] == [[10, 15], [10, 15]]
    # 列原合计 11、12 -> [10,15]、[10,15]
    assert body["legal_col_ranges"] == [[10, 15], [10, 15]]
    # 总计 23 -> [20,25]
    assert body["legal_grand_range"] == [20, 25]


def test_register_is_immutable(client):
    assert _register(client).status_code == 201
    resp = _register(client)
    assert resp.status_code == 409
    assert resp.json()["status"] == "conflict"


def test_register_validation_errors(client):
    # 月份格式
    resp = client.post("/api/v1/tables", json={**EX_TABLE, "month": "202610"})
    assert resp.status_code == 422

    # 超 12 行
    resp = client.post(
        "/api/v1/tables",
        json={"month": MONTH, "matrix": [[1]] * 13},
    )
    assert resp.status_code == 422

    # 负数 / 布尔 / 浮点 / 行长不一致 / 名称长度不符
    bad_payloads = [
        {"month": MONTH, "matrix": [[-1, 0], [0, 0]]},
        {"month": MONTH, "matrix": [[True, 0], [0, 0]]},
        {"month": MONTH, "matrix": [[1.5, 0], [0, 0]]},
        {"month": MONTH, "matrix": [[1, 2], [3]]},
        {"month": MONTH, "matrix": [[1, 2], [3, 4]], "row_names": ["只有一个"]},
    ]
    for payload in bad_payloads:
        assert client.post("/api/v1/tables", json=payload).status_code == 422


def test_month_not_found(client):
    resp = client.get("/api/v1/tables/2099-01")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 批次：两批各自可行、合在一起冲突
# ---------------------------------------------------------------------------

def test_batch_flow_feasible_then_conflict(client):
    _register(client)

    # 第一批（Swagger 样例）：青山镇行合计向上取整为 15，可行
    resp = _batch(
        client, MONTH,
        rows=EX_BATCH_1["row_totals"],
        cols=EX_BATCH_1["col_totals"],
        label=EX_BATCH_1["batch_label"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["batch_no"] == 1
    assert len(body["newly_confirmed"]) == 1
    witness = body["witness_matrix"]
    assert body["witness_row_totals"][0] == 15
    assert body["witness_grand_total"] in (20, 25)
    _witness_all_multiples_of_five(witness)

    # 第二批在新月份上单独可行：18-59 岁列合计向上取整为 15
    other = "2026-09"
    _register(client, month=other)
    resp = _batch(
        client, other,
        rows=EX_BATCH_2["row_totals"],
        cols=EX_BATCH_2["col_totals"],
    )
    assert resp.status_code == 200
    assert body["witness_row_totals"][0] == 15
    assert resp.json()["witness_col_totals"][1] == 15

    # 合并冲突样例打到已确认第一批的月份 -> 409，且已确认合计不变
    resp = _batch(
        client, MONTH,
        rows=EX_BATCH_COMBINED["row_totals"],
        cols=EX_BATCH_COMBINED["col_totals"],
        grand=EX_BATCH_COMBINED["grand_total"],
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["status"] == "conflict"
    assert body["reasons"]
    confirmed = {(c["kind"], c["index"]): c["value"]
                 for c in body["confirmed_totals"]}
    assert confirmed[(ROW, 0)] == 15

    # 已确认合计未被污染
    resp = client.get(f"/api/v1/tables/{MONTH}/confirmed-totals")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_confirmed_value_cannot_change(client):
    _register(client)
    assert _batch(client, MONTH, rows=[10, None], cols=[None, None]).status_code == 200
    # 同一行改成另一个合法值 -> 409
    resp = _batch(client, MONTH, rows=[15, None], cols=[None, None])
    assert resp.status_code == 409
    assert any("不能改" in r for r in resp.json()["reasons"])


def test_idempotent_resubmission(client):
    _register(client)
    first = _batch(client, MONTH, rows=[15, None], cols=[None, None])
    assert first.status_code == 200
    assert first.json()["batch_no"] == 1
    # 完全相同的值重新提交：接受但没有新增确认项，批次号照常递增
    second = _batch(client, MONTH, rows=[15, None], cols=[None, None])
    assert second.status_code == 200
    assert second.json()["newly_confirmed"] == []
    assert second.json()["batch_no"] == 2
    assert len(second.json()["confirmed_totals"]) == 1


def test_partial_batches_accumulate(client):
    _register(client)
    assert _batch(client, MONTH, rows=[15, None], cols=[None, None]).status_code == 200
    # 第二批只确认列，与已确认行合计可同时满足（列 1 向上不与行 0 抢名额）
    resp = _batch(client, MONTH, rows=[None, None], cols=[None, 10])
    assert resp.status_code == 200
    assert resp.json()["batch_no"] == 2
    assert len(resp.json()["confirmed_totals"]) == 2


def test_batch_validation_errors(client):
    _register(client)
    # 未登记月份
    assert _batch(client, "2099-01", rows=[10], cols=[10]).status_code == 404

    # 合计不是 5 的倍数 / 负数 / 布尔
    for bad in (12, -5, True):
        resp = _batch(client, MONTH, rows=[bad, None], cols=[None, None])
        assert resp.status_code == 422

    # 尺寸与当月矩阵不符
    resp = _batch(client, MONTH, rows=[10, 15, 20], cols=[None, None])
    assert resp.status_code == 422

    # 路径月份与请求体月份不一致
    resp = client.post(
        f"/api/v1/tables/{MONTH}/batches",
        json={"month": "2026-09", "row_totals": [10, None],
              "col_totals": [None, None], "grand_total": None},
    )
    assert resp.status_code == 422


def test_grand_total_conflict(client):
    _register(client)
    # 原总计 23，合法区间 [20,25]，提交 30 超出
    resp = _batch(client, MONTH, rows=[None, None], cols=[None, None], grand=30)
    assert resp.status_code == 409
    assert any("合法取整区间" in r for r in resp.json()["reasons"])

    resp = _batch(client, MONTH, rows=[None, None], cols=[None, None], grand=25)
    assert resp.status_code == 200
    assert resp.json()["witness_grand_total"] == 25


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def test_monthly_query_rounds_and_sums(client):
    _register(client)
    _batch(client, MONTH, rows=[15, None], cols=[None, None])

    resp = client.get(f"/api/v1/tables/{MONTH}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["original_matrix"] == EX_TABLE["matrix"]
    rounded = body["rounded_matrix"]
    _witness_all_multiples_of_five(rounded)
    # 返回的合计必须是从格值真实加出来的
    assert body["row_totals"] == [sum(r) for r in rounded]
    assert body["col_totals"] == [
        sum(rounded[i][j] for i in range(2)) for j in range(2)
    ]
    assert body["grand_total"] == sum(body["row_totals"])
    # 且满足已确认合计
    assert body["row_totals"][0] == 15
    assert body["grand_total"] in (20, 25)
    # 逐格仍在原数的最近 5 倍数区间内
    for i in range(2):
        for j in range(2):
            original = EX_TABLE["matrix"][i][j]
            assert (original // 5) * 5 <= rounded[i][j] <= -(-original // 5) * 5


def test_query_without_any_confirmation(client):
    _register(client)
    resp = client.get(f"/api/v1/tables/{MONTH}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["confirmed_totals"] == []
    _witness_all_multiples_of_five(body["rounded_matrix"])
    assert body["grand_total"] == sum(sum(r) for r in body["rounded_matrix"])


def test_list_tables(client):
    _register(client, "2026-08")
    _register(client, "2026-09")
    _batch(client, "2026-08", rows=[10, None], cols=[None, None])
    resp = client.get("/api/v1/tables")
    assert resp.status_code == 200
    body = {item["month"]: item for item in resp.json()}
    assert set(body) == {"2026-08", "2026-09"}
    assert body["2026-08"]["confirmed_count"] == 1
    assert body["2026-08"]["batches"] == 1
    assert body["2026-09"]["confirmed_count"] == 0


# ---------------------------------------------------------------------------
# 并发：互相冲突的合计不能同时确认
# ---------------------------------------------------------------------------

def test_concurrent_conflicting_batches(client):
    _register(client)
    barrier = threading.Barrier(2)
    outcomes: list[tuple[int, dict]] = []

    def worker(row_value):
        barrier.wait()
        resp = _batch(client, MONTH, rows=[row_value, None], cols=[None, None])
        outcomes.append((resp.status_code, resp.json()))

    # 同一行（行 0）提交两个不同的合法值：15 向上、10 向下，互斥
    t1 = threading.Thread(target=worker, args=(15,))
    t2 = threading.Thread(target=worker, args=(10,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    statuses = sorted(code for code, _ in outcomes)
    # 一个成功、一个冲突；不允许两个都成功
    assert statuses == [200, 409]
    accepted = next(body for code, body in outcomes if code == 200)
    rejected = next(body for code, body in outcomes if code == 409)
    assert any("不能改" in r for r in rejected["reasons"])
    # 最终只保留一个行 0 合计
    confirmed = client.get(
        f"/api/v1/tables/{MONTH}/confirmed-totals"
    ).json()
    row_zero = [c for c in confirmed if c["kind"] == "row" and c["index"] == 0]
    assert len(row_zero) == 1
    assert row_zero[0]["value"] in (10, 15)
    assert row_zero[0]["value"] == next(
        c["value"] for c in accepted["confirmed_totals"]
        if c["kind"] == "row" and c["index"] == 0
    )


def test_concurrent_disjoint_conflicting_batches(client):
    """两批确认的下标不重叠但结构上互斥：只有一个能成功。

    矩阵 [[6,5],[5,7]] 的可向上格只有 (0,0) 与 (1,1)，原总计 23 的合法
    区间为 [20,25]，全表只允许 1 格向上。批 A 要求行 0 向上（必须格
    (0,0) 向上），批 B 要求列 1 向上（必须格 (1,1) 向上），下标不重叠
    但合计需要 2 格向上，互斥。
    """
    payload = {
        "month": MONTH,
        "row_names": ["r0", "r1"],
        "col_names": ["c0", "c1"],
        "matrix": [[6, 5], [5, 7]],
    }
    assert client.post("/api/v1/tables", json=payload).status_code == 201

    barrier = threading.Barrier(2)
    outcomes = []

    def worker(rows, cols):
        barrier.wait()
        resp = _batch(client, MONTH, rows=rows, cols=cols)
        outcomes.append((resp.status_code, resp.json()))

    t1 = threading.Thread(
        target=worker, args=([15, None], [None, None])
    )
    t2 = threading.Thread(
        target=worker, args=([None, None], [None, 15])
    )
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    statuses = sorted(code for code, _ in outcomes)
    assert statuses == [200, 409]
    # 后到的一批未写入任何合计：成功批只确认 1 项
    confirmed = client.get(
        f"/api/v1/tables/{MONTH}/confirmed-totals"
    ).json()
    assert len(confirmed) == 1
    rejected = next(body for code, body in outcomes if code == 409)
    # 结构冲突（不是“已确认不能改”），原因来自可行流检验
    assert any("可行流" in r for r in rejected["reasons"])


# ---------------------------------------------------------------------------
# Swagger
# ---------------------------------------------------------------------------

def test_openapi_contains_samples(client):
    schema = client.get("/openapi.json").json()
    # 登记表样例
    register_examples = (
        schema["paths"]["/api/v1/tables"]["post"]["requestBody"]
        ["content"]["application/json"]["examples"]
    )
    assert "sample_2x2" in register_examples

    batch_examples = (
        schema["paths"]["/api/v1/tables/{month}/batches"]["post"]["requestBody"]
        ["content"]["application/json"]["examples"]
    )
    assert set(batch_examples) == {
        "batch_1_feasible", "batch_2_feasible", "combined_conflict"
    }
    # 两批样例各自可行：在不同月份分别提交（同一月份先提交第一批后，
    # 第二批会按设计与其冲突，409 已在 test_batch_flow 中覆盖）
    separate_months = ("2026-08", "2026-09")
    for key, separate_month in zip(
        ("batch_1_feasible", "batch_2_feasible"), separate_months
    ):
        example = batch_examples[key]["value"]
        _register(client, month=separate_month)
        resp = _batch(
            client, separate_month,
            rows=example["row_totals"],
            cols=example["col_totals"],
            grand=example.get("grand_total"),
        )
        assert resp.status_code == 200
    assert client.get("/docs").status_code == 200


def test_method_endpoint(client):
    resp = client.get("/method")
    assert resp.status_code == 200
    assert "带下界可行流" in resp.json()["feasibility"]


# ---------------------------------------------------------------------------

def _witness_all_multiples_of_five(matrix):
    assert all(cell % 5 == 0 for row in matrix for cell in row)
    assert all(cell >= 0 for row in matrix for cell in row)
