# M6 独立验证方案（TEST）

- 用途：**给测试执行方**。这不是"再跑一遍 pytest"——重复执行本套用例的增量价值接近零（代码与断言同源、本地已跑过）。本方案给出三件**本地跑不出来**的动作。
- 为什么需要：`tests/fixtures/m6/fixture_pack.json` 的 `expected` 与被测代码**出自同一作者**。若公式系统性写错，两边会一起错，测试照样全绿。**"全绿"证明的是自洽，不是正确。**
- 基线：分支 `feat/m6-finance-20260913`，HEAD 以交付包内的 `COMMIT_SHA.txt` 或 `git rev-parse HEAD` 为准。
- 配套：模型与契约见 `SPEC-M6-runtime-io-and-gates-20260915.md`；用例与判据见 `TEST-M6-fixture-pack-20260915.md`。

## 三件事的分工

| # | 动作 | 能抓到什么 | 抓不到什么 |
|---|---|---|---|
| 一 | **独立复算**（换输入手算） | 实现与公式不符（漏乘损耗、报废没扣、时分秒单位错、分母错） | 公式本身与工厂财务口径不符（见 §1.2 末） |
| 二 | **人工三条**（来源标识 / 不伪装 / 数值未漂移） | 假数据冒充真实数据、夹具预期值漂移 | 公式正确性 |
| 三 | **异机冒烟** | 机器相关假设（编码、临时目录、依赖版本、Python 小版本） | 任何业务口径问题 |

---

## 一、独立复算（最高价值，优先做）

### 1.1 三条铁律

1. **先手算填表，再跑代码。** 顺序反了就失去意义（会不自觉地往系统输出上靠）。
2. **跑完之前不要看附录 A。** 附录里是系统实际输出，用来在分歧时定位，**不是答案来源**——公式才是。
3. **手算与系统不一致时**：先自己复核一遍公式和算术，仍不一致才报缺陷，并附上手算过程 + 两次运行输出。

### 1.2 公式（写下来给你的，出处见括号）

| 项 | 公式 |
|---|---|
| 单台材料成本 | `Σ(单价 × 单台用量 × (1 + 损耗率))`（损耗**乘在用量侧**，不是加在金额上） |
| 单台工时 | `Σ(工序 standard_minutes) ÷ 60`（字段名即**分钟**） |
| 单台人工 | `单台工时 × hour_rate` |
| 单台制费 | `单台工时 × overhead_rate` |
| 单台成本 | `材料 + 人工 + 制费` |
| 整单总成本 | `单台成本 × quantity`（缺 quantity 时**必须为 null**，不得为 0） |
| 报价单价 | `单台成本 × (1 + 加价率)` |
| 计件工资 | `Σ((报工数量 − 报废) × 单价)`（报废**要扣**；无单价的行进 missing，不按 0 计） |
| 时薪 | `月薪 ÷ 计薪天数 ÷ 每日工时` |
| 加班费 | `时薪 × 加班倍数 × 加班工时` |
| 缺勤扣款 | `时薪 × 缺勤工时`（**扣减**） |
| 应发月薪 | `月薪 + 加班费 − 缺勤扣款 + 计件工资` |
| 费用分摊 | `费用 × 该产品基准 ÷ 全部产品基准合计`（分母是**基准合计**，不是产品条数） |
| 对账期末 | `期初 + Σ(in) − Σ(out)` |

出处：上表与代码注释、`M6-开发计划-v2口径-20260913.md` §3 一致。

> ⚠️ **本表的边界**：手算能验证"实现是否符合公式"，**不能**验证"公式是否符合工厂财务的实际口径"。后者是业务确认，且已有 5 条明确待确认（代码里叫 `PENDING_FINANCE_CONFIRMATION`）：
> ① 费用分摊基准（默认按数量；导图口径社保按人数）② 加班倍数 1.5 / 计薪天数 21.75 / 每日工时 8 ③ 最低毛利率 0.15 ④ 库存估价单价来源 ⑤ 车间维度口径。
> **这 5 条不是跑测试能替代的**，需要财务确认。若你或财务对某条有异议，请直接标出来——这比任何测试失败都重要。

### 1.3 工作表（先填"你手算"列）

#### 探针 A｜成本：材料（含损耗）+ 工序（分钟）+ 单位成本

**输入**（走 `get_product_cost`，即"当场算单台成本"）

```json
{
  "product_code": "P-PROBE",
  "bom_lines": [
    {"material_code": "M1", "qty_per": 3, "unit_price": 4.0, "loss_rate": 0.25},
    {"material_code": "M2", "qty_per": 2, "unit_price": 7.5, "loss_rate": 0}
  ],
  "inventory": [
    {"material_code": "M1", "available_qty": 50, "stock_class": "raw"},
    {"material_code": "M2", "available_qty": 50, "stock_class": "raw"}
  ],
  "routing_steps": [
    {"operation_id": "OP10", "standard_minutes": 90},
    {"operation_id": "OP20", "standard_minutes": 30}
  ],
  "hour_rate": 60,
  "overhead_rate": 25
}
```

| 你要算的 | 你的手算 | 系统输出 | 一致？ |
|---|---|---|---|
| 材料成本（两行分别算再相加，写出每一步） | | | |
| 总工时（小时） | | | |
| 人工 | | | |
| 制费 | | | |
| 单台成本 | | | |

> 提示：两行材料并不同值，请分别写。若你把损耗算成"减去"、或忘记乘、或把 90 分钟当成 90 小时，这里的数会明显不同。

#### 探针 B｜报价加价

在探针 A 的单台成本基础上，加价率 `0.15`。用 `generate_quotation`（或直接问"报个价"）。

| 你要算的 | 你的手算 | 系统输出 | 一致？ |
|---|---|---|---|
| 基准成本 | | | |
| 报价单价 | | | |

#### 探针 C｜计件工资

**报工**：`W01` 报工 `120` 件、报废 `20`；`W02` 报工 `50` 件、报废 `0`。
**单价**：`ST-01` + `P1` = `3.2` 元/件，生效日 `2026-08-01`。

| 你要算的 | 你的手算 | 系统输出 | 一致？ |
|---|---|---|---|
| W01 计件工资 | | | |
| W02 计件工资 | | | |

> 提示：报废是否被扣，会让 W01 出现两种明显不同的结果。

#### 探针 D｜月薪工资

**月薪**：`W01` = `8700` 元。
**出勤**：加班 `4` 小时、缺勤 `0` 小时。
**计件**：`W01` = `320` 元（取探针 C 的结果用）。
**口径**：加班倍数 `1.5`、计薪天数 `21.75`、每日工时 `8`（显式给出，不用默认值）。

| 你要算的 | 你的手算 | 系统输出 | 一致？ |
|---|---|---|---|
| 时薪 | | | |
| 加班费 | | | |
| 缺勤扣款 | | | |
| 应发合计 | | | |

> 提示：这组数特意选成时薪为整数，方便你验证分母（计薪天数×每日工时）取对了没有。

#### 探针 E｜费用分摊

**费用**：电费 `4500` 元。
**基准**：`P1` 数量 `30`、`P2` 数量 `60`、`P3` 数量 `10`。
**口径**：按数量分摊（**显式给出**，不要依赖默认值）。

| 你要算的 | 你的手算 | 系统输出 | 一致？ |
|---|---|---|---|
| 基准合计（分母） | | | |
| P1 分摊 | | | |
| P2 分摊 | | | |
| P3 分摊 | | | |

> 提示：如果分母错用成"产品条数"，三个产品会各分到同一个数——一眼能看出来。

#### 探针 F｜对账

**期初** `5000`；流水：收入 `1200`、支出 `800`、收入 `300`。

| 你要算的 | 你的手算 | 系统输出 | 一致？ |
|---|---|---|---|
| 收入合计 | | | |
| 支出合计 | | | |
| 期末余额 | | | |

### 1.4 怎么跑这些探针

把下面整段存成 `probe_m6.py`，**放在仓库根目录**（与 `src/`、`tests/` 同级），然后在**仓库根目录**执行（它只调用内核与工具读口，不落库、不开门、不写任何数据）。

```bash
# 情况一：已按 §3.3 装过依赖（pip install -e ".[test]"）——直接跑
python probe_m6.py

# 情况二：没装包——临时把源码目录加进导入路径（Git Bash / macOS / Linux）
PYTHONPATH=src python probe_m6.py
# Windows cmd：set PYTHONPATH=src && python probe_m6.py
# PowerShell：$env:PYTHONPATH="src"; python probe_m6.py
```

> 如果报 `ModuleNotFoundError: No module named 'yunpai_orchestrator'`，就是上面两条都没做——要么先 `pip install -e ".[test]"`，要么加上 `PYTHONPATH=src`。**这不是代码缺陷。**
> 另外必须**在仓库根目录**执行：`PYTHONPATH=src` 是相对路径。

```python
"""M6 独立复算探针（只读，不落库）。用法：python probe_m6.py"""
import asyncio, json, os, tempfile

from yunpai_orchestrator import m6_cost as c
from yunpai_orchestrator.m6_tools import m6_get_product_cost

CTX = {"m6_db_path": os.path.join(tempfile.mkdtemp(), "probe.sqlite"),
       "tenant_id": "probe", "task_id": "probe"}


async def main() -> None:
    print("== 探针 A：成本（材料含损耗 / 分钟工时 / 单位成本）==")
    data = (await m6_get_product_cost({
        "product_code": "P-PROBE",
        "bom_lines": [
            {"material_code": "M1", "qty_per": 3, "unit_price": 4.0, "loss_rate": 0.25},
            {"material_code": "M2", "qty_per": 2, "unit_price": 7.5, "loss_rate": 0},
        ],
        "inventory": [{"material_code": "M1", "available_qty": 50, "stock_class": "raw"},
                      {"material_code": "M2", "available_qty": 50, "stock_class": "raw"}],
        "routing_steps": [{"operation_id": "OP10", "standard_minutes": 90},
                          {"operation_id": "OP20", "standard_minutes": 30}],
        "hour_rate": 60, "overhead_rate": 25,
    }, CTX))["data"]
    print("  材料 =", data.get("unit_material_cost"), " 人工 =", data.get("unit_labor_cost"),
          " 制费 =", data.get("unit_overhead_cost"), " 单位成本 =", data.get("unit_cost"),
          " cost_incomplete =", data.get("cost_incomplete"))

    print("== 探针 B：报价加价 0.15 ==")
    print("  ", c.compute_quotation_price(data.get("unit_material_cost"),
                                          data.get("unit_labor_cost"),
                                          data.get("unit_overhead_cost"), markup_rate=0.15))

    print("== 探针 C：计件工资 ==")
    piece = c.compute_piece_pay(
        [{"worker_id": "W01", "station_code": "ST-01", "product_code": "P1",
          "quantity_report": 120, "scrap": 20, "report_date": "2026-09-15"},
         {"worker_id": "W02", "station_code": "ST-01", "product_code": "P1",
          "quantity_report": 50, "scrap": 0, "report_date": "2026-09-15"}],
        [{"station_code": "ST-01", "product_code": "P1", "unit_rate": 3.2,
          "effective_from": "2026-08-01"}])
    print("  totals =", piece["totals"], " missing =", piece["missing"],
          " cost_incomplete =", piece["cost_incomplete"])

    print("== 探针 D：月薪工资 ==")
    monthly = c.compute_monthly_pay(
        {"W01": 8700}, {"W01": {"overtime_hours": 4, "absence_hours": 0}}, {"W01": 320.0},
        overtime_multiplier=1.5, work_days=21.75, hours_per_day=8)
    print("  rows =", json.dumps(monthly["rows"], ensure_ascii=False),
          " cost_incomplete =", monthly["cost_incomplete"])

    print("== 探针 E：费用分摊（按数量）==")
    expense = c.compute_expense_allocation(
        [{"category": "electricity", "amount": 4500}],
        [{"product_code": "P1", "quantity": 30},
         {"product_code": "P2", "quantity": 60},
         {"product_code": "P3", "quantity": 10}],
        allocation_basis="quantity")
    print("  by_product =", {r["product_code"]: r["allocated_total"] for r in expense["by_product"]},
          " total =", expense["total_allocated"])

    print("== 探针 F：对账 ==")
    print("  ", {k: v for k, v in c.compute_statement(
        5000, [{"direction": "in", "amount": 1200},
               {"direction": "out", "amount": 800},
               {"direction": "in", "amount": 300}]).items()
        if k in ("opening_balance", "inflow", "outflow", "closing_balance")})


asyncio.run(main())
```

### 1.5 判据

- **全部一致** → 第一节通过。
- **任一不一致** → 先复核公式与算术；仍不一致才报缺陷，附手算过程 + 探针输出原文。
- 顺带记录：如果某个探针出现 `cost_incomplete=true` 而你没给全输入，那是**正确行为**（缺数不编造），不算缺陷。

---

## 二、人工三条（来源与数值的合规核查）

### 2.1 T-01 来源标识在位

```bash
grep -c "source_ref" tests/fixtures/m6/fixture_pack.json      # 应与事实条数相当，不是 0/1
grep -o '"source_ref": "fixture:[^"]*"' tests/fixtures/m6/fixture_pack.json | head -20
```

**判据**：每条夹具、以及每条**行级事实**（BOM 行、工序、库存行、报工行）都带 `source_ref: fixture:<ID>...`；抽查 5 条，缺一即失败。

**为什么必须人工看**：机器只能断言"字段存在"，判断不了"这个标识是否真的指向测试数据而非伪造的真实来源"。这正是"不冒充、不编造"两条红线的落地。

### 2.2 T-02 不伪装成真实数据

```bash
# 1) 所有 source_ref 是否都以 fixture: 开头
grep -o '"source_ref": "[^"]*"' tests/fixtures/m6/fixture_pack.json | grep -v '"fixture:' || echo "OK：全部为 fixture: 前缀"

# 2) 是否混入真实业务名称（示例关键词，按你的实际客户/供应商名补充）
grep -iE "客户|供应商|有限公司|经理|工号" tests/fixtures/m6/fixture_pack.json || echo "OK：未发现"
```

**判据**：第一条无输出（说明没有非 `fixture:` 前缀的来源）；第二条无真实企业/人名。**任一命中即失败**，因为测试数据伪装成真实事实会污染后续判断。

### 2.3 T-03 夹具预期值未漂移

把 `tests/fixtures/m6/fixture_pack.json` 里每组的 `expected` 与 `TEST-M6-fixture-pack-20260915.md` §二 的实算值逐条对照：

| 夹具 | fixture_pack 的 expected | TEST 文档 §二 | 一致？ |
|---|---|---|---|
| FX-COST-001 | | | |
| FX-COST-002 | | | |
| FX-PAY-001 | | | |
| FX-PAY-002 | | | |
| FX-EXP-001 | | | |
| FX-ASSET-001 | | | |
| FX-STMT-001 | | | |
| FX-INV-001 | | | |

> ⚠️ **说清这条验的是什么**：它验证"夹具里的预期值没有相对独立锚发生漂移"（即装配/落库路径与内核算的是同一组数）。它**不验证公式正确性**——那个由第一节的手算负责。两节不要互相替代，也不要把本节结论写成"数值已验证正确"。

---

## 三、异机冒烟（**这是冒烟，不是验收**）

目的：验证代码在**不是它被开发出来的那台机器**上能否正常跑。本机跑多少次都测不出机器相关假设。

### 3.1 前置

```bash
git --version                       # 需 git
python --version                    # 需 3.11 以上，项目基线 3.12
```

### 3.2 取得代码并确认版本

```bash
git clone m6-p017.bundle m6-test && cd m6-test
git rev-parse HEAD                  # 与交付包文件名/COMMIT_SHA.txt 对照
```

### 3.3 安装与跑

```bash
python -m venv .venv
source .venv/Scripts/activate       # Windows Git Bash；cmd 用 .venv\Scripts\activate.bat
pip install -e ".[test]"

python -m pytest tests/test_api_runs_m6.py -q     # 版本自检：应 6 passed
python -m pytest tests/ -q -k m6                  # 基线 179 passed, 1 skipped
python -m pytest -q                               # 基线 841 passed, 5 skipped
python scripts/check_contracts.py                 # 应 exit 0（**不要加 --strict**）
```

### 3.4 记录什么

操作系统与版本、`python --version`、`pip install` 的尾部输出、四条命令的完整输出（含 passed/skipped 计数）、以及**任何与环境相关的失败原文**。

### 3.5 已知环境级偶发失败

若出现：

```
PermissionError: [WinError 5] 拒绝访问。: ...\pytest-of-...\pytest-current
```

那是 Windows 下 pytest 清理临时目录的环境问题，**与代码无关**（实测同目录复跑即恢复）。处置：**先原样复跑一次**；仍失败则换干净目录：

```bash
python -m pytest tests/ -q -k m6 --basetemp=%TEMP%\m6-run1     # cmd
python -m pytest tests/ -q -k m6 --basetemp=/tmp/m6-run1       # Git Bash / macOS / Linux
```

只有**在干净 basetemp 下稳定复现**的失败才计为缺陷，报告里附两次输出。

---

## 四、回执模板（按这个回报）

### 4.1 逐项结果

| 项 | 结果 | 证据（命令 + 关键输出） | 备注 |
|---|---|---|---|
| 一 独立复算（探针 A~F） | 通过 / 不一致 | `probe_m6.py` 输出原文 + 手算表 | 不一致的写出差异数 |
| 二 T-01 来源标识 | 通过 / 失败 | grep 输出 | |
| 二 T-02 不伪装 | 通过 / 失败 | grep 输出 | |
| 二 T-03 数值未漂移 | 通过 / 失败 | 对照表 | |
| 三 异机冒烟 | 通过 / 失败 | 四条命令输出 | 标注 OS 与 Python 版本 |

### 4.2 结论与口径问题

- 三条各自结论：______
- 本轮是否建议进入"接真实数据库"阶段：建议 / 不建议，理由：______
- **口径异议（重要）**：§1.2 末的 5 条待确认口径中，你认为哪几条与工厂财务实际做法不符？______

### 4.3 诚实声明（照抄进报告，不要删）

- 本轮验证的范围：**假数据下的确定性与公式实现**。
- 本轮**未**验证：真实 canonical 数据齐备性、M1→M5→M6 全链路、生产环境验收、以及 §1.2 的 5 条业务口径（需财务确认）。
- 本轮的"异机跑通"属**环境冒烟**，不得记为"独立测试通过"或"验收通过"。

---

## 附录 A：探针参照值（**先填完 §1.3 的"你手算"列再看**）

以下是修复后代码的**实际输出**（2026-09-15 实测）。用途：当你手算与系统输出不一致时，用它快速判断差在哪一步。

**它不是"正确答案"的来源——规则来源是 §1.2 的公式。**

| 探针 | 项目 | 系统实际输出 |
|---|---|---|
| A | 材料成本 | `30.0` |
| A | 工时（小时） | `2.0` |
| A | 人工 | `120.0` |
| A | 制费 | `50.0` |
| A | 单台成本 | `200.0`（`cost_incomplete=false`） |
| B | 基准成本 / 报价单价 | `200.0` / `230.0` |
| C | W01 / W02 计件 | `320.0` / `160.0`（`cost_incomplete=false`） |
| D | 时薪 | `50.0` |
| D | 加班费 | `300.0` |
| D | 缺勤扣款 | `0.0` |
| D | 应发合计 | `9320.0` |
| E | 基准合计 | `100` |
| E | P1 / P2 / P3 分摊 | `1350.0` / `2700.0` / `450.0`（合计 `4500.0`） |
| F | 收入 / 支出 / 期末 | `1500.0` / `800.0` / `5700.0` |

> 若你的手算与上表不一致，**先检查是不是这几处最常错的地方**：损耗率的方向（乘 `1+loss` 而不是加金额或减）；分钟→小时（除 60）；报废是否扣减；时薪分母（计薪天数 × 每日工时）；分摊分母（基准合计而非产品条数）；对账的 in/out 符号。
