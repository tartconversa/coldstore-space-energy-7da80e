# 共享冷库的空间与能耗计量仓

把冷库的原始读数、库区变化、货物占用和租赁约定建成可以彼此核对的空间、能量与
结算事实，回答租户对电费账单的质疑：这笔电来自独立计量还是公共冷量、依据哪些
读数与占用、哪些金额可以确认、哪些暂不交付结算。

`sourcepack/` 是随附的合成资料通用检查器；`coldstore/` 是本项目建设的计量数据仓。
主体、设备和数值全部合成。

## 口径如何落到仓表

资料字段与业务语义见 `docs/资料口径.md`，关键口径的实现位置：

| 口径 | 实现 |
| --- | --- |
| 读数仅在 `read_at` 代表累计值，区间增量由相邻有效读数解释 | `energy.py` 单表区间 |
| 声明模数回绕按 modulus 解释；无声明倒退不产生负耗电、挂起待核 | `energy.build_meter_intervals` |
| 换表用旧末读/新底数缝合，缝口不符登记质量问题 | `energy.build_replacements/build_chains` |
| 表间包含关系按物理接线生效区间，不按表名猜；拆区即终止旧关系 | `fact_topology_edge` |
| 父量减有效独立子量为公共池；子表缺测不等于零，覆盖不齐整段挂起 | `energy.build_pool_segments` |
| PUBLIC 是公共负荷子表，只作证据留在池内，不与残量重复相加 | 池段 `public_evidence_kwh` |
| 负残差如实保留、绝不剪零后再分摊 | `negative_residual` 质量问题 + 池挂起 |
| 独立分表租户先从公共池剔除，不再参与公共分配 | `allocation.py` |
| 公共池按 m3×小时×温区权重 分担；open 体积是暂估分母，份额保留待分 | `allocation.py` |
| 分母覆盖不全保留待分金额，不在资料齐的租户之间分光 | `settle.py` 挂起台账 |
| 移库/拆托只形成同一 cargo 的位置区间，不增加货量 | `space.build_occupancy` |
| 预留只形成场地费、不形成耗电；与实占不叠加；按小时折算 | `space.build_reservations` |
| 占用、预留、可用容量是三个独立事实，同一时间轴核对 | `fact_*` + `fact_capacity_day` |
| 日行只按可证明范围或明确估算标记呈现，不伪装分钟实测 | `marts.py`（measured/estimated） |
| 舍入到分的尾差归账单分摊基数最大项，并列按业务键升序 | `settle.round_to_fen` |

## 分层

- **落地区** `landing_receipt` / `landing_raw_row`：只追加不可变，保留每次接收的
  文件摘要与原始行；相同摘要幂等不新增事实；同键新文件是整行修订，历史不丢。
- **原始当前区** `raw_*`：按每个来源最新一次接收整表刷新。
- **事实区**：
  - 能量 `fact_meter_interval`（单表区间）、`fact_meter_chain_segment`（跨换表链）、
    `fact_topology_edge`、`fact_pool_segment`（池与残差）、`fact_pool_allocation`
    （池分摊，入仓即算，不绑定关账）、`fact_tenant_direct`（独立计量）；
  - 空间 `fact_occupancy_interval/day`、`fact_reservation_interval/day`、
    `fact_capacity_day`；
  - `quality_issue` 集中登记回绕、倒退、缺测、负残差、超容、缝口异常等。
- **结算区** `settlement_run` / `fact_bill_line` / `fact_hold` / `bill_summary`：
  关账时才金额化，历史运行保留；confirmed 可交付，provisional/held 暂不交付。
- **集市区** `mart_day_energy` / `mart_day_pool` / `mart_day_space`：自然日粒度。

时间在仓内统一为 +08:00 挂钟朴素时间（DuckDB `TIMESTAMP`），区间一律左闭右开。

## 环境与运行

Python 3.11.2，依赖仅 `duckdb==1.4.1`（见 requirements.txt）。用 `-I` 隔离模式运行，
避免资料目录中的同名模块被导入：

    python3 -m pip install -r requirements.txt
    python3 -I run_coldstore.py --db warehouse.duckdb ingest --data data
    python3 -I run_coldstore.py --db warehouse.duckdb issues
    python3 -I run_coldstore.py --db warehouse.duckdb pools
    python3 -I run_coldstore.py --db warehouse.duckdb close --from 2026-10-01 --to 2026-10-31
    python3 -I run_coldstore.py --db warehouse.duckdb bill --run 1
    python3 -I run_coldstore.py --db warehouse.duckdb lineage --run 1 --line 2
    python3 -I run_coldstore.py --db warehouse.duckdb query --view day --day 2026-10-20
    python3 -I run_coldstore.py --db warehouse.duckdb query --view tenant --tenant B
    python3 -I run_coldstore.py --db warehouse.duckdb query --view meter --meter OLD

`ingest` 持续接收新资料并重算事实（落地与已关账不动）；`rebuild` 从原始当前区
全量重算事实与集市；`close` 形成一个账期结算并输出账单、汇总与挂起。

## 对账与溯源

- `pools`：每个总表分段的总量、独立子量、公共负荷证据、公共池与未解释残差。
- `lineage --run --line`：账单行逐层追到池段、分摊依据的占用区间、父表读数区间；
  独立计量行追到拓扑关系、表读数链与换表记录；场地费行追到预留与逐日费用。
- `query --view holds` / `issues`：哪些量挂起待核、原因是什么。
- 缺测、负残差、open 占用、容量冲突、无电价等情况下，对应用量与金额进入
  `fact_hold` 并在账单汇总中与确认金额分列，不会被悄悄分光或压成零。

## 测试

    python3 -I -m unittest discover -s tests -t . -v
    python3 -m compileall -q coldstore tests run_coldstore.py

测试在临时目录用合成资料构造，不触碰 `data/`，覆盖回绕、无声明倒退、换表缝口、
子表缺测与半月覆盖、负残差不剪零、移库不增体积、open 暂估、超容、幂等接收、
整行修订、日集市估算标记、分位舍入与账单溯源。

说明：仓库随附 `data/catalog.json` 的文件摘要与当前 CSV 实际摘要处于预置的不一致
状态（在本仓开发前即如此）。`sourcepack` 检查器会如实报“资料摘要不一致”；
`coldstore` 不依赖 catalog 摘要，按字段契约直接接收 CSV。
