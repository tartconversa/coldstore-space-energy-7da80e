-- 计量数据仓仓表定义。
-- 时间一律 TIMESTAMP（输入 +08:00），区间统一左闭右开。
-- 落地区只追加不可变；原始当前区、事实区、集市区可由来源全量重算。

-- ============ 落地区（接收历史，只追加） ============
CREATE TABLE IF NOT EXISTS landing_receipt (
    receipt_id      BIGINT,
    source_table    VARCHAR NOT NULL,
    file_name       VARCHAR NOT NULL,
    file_sha256     VARCHAR NOT NULL,
    received_at     TIMESTAMP NOT NULL,
    row_count       BIGINT NOT NULL,
    PRIMARY KEY (receipt_id)
);

CREATE TABLE IF NOT EXISTS landing_raw_row (
    receipt_id      BIGINT NOT NULL,
    source_table    VARCHAR NOT NULL,
    row_index       BIGINT NOT NULL,
    payload         VARCHAR NOT NULL,
    PRIMARY KEY (receipt_id, source_table, row_index)
);

-- ============ 原始当前区（每次接收按来源整表替换） ============
CREATE TABLE IF NOT EXISTS raw_zones (
    zone_version VARCHAR PRIMARY KEY, zone VARCHAR, start_ts TIMESTAMP, end_ts TIMESTAMP,
    capacity_m3 DOUBLE, weight DOUBLE
);
CREATE TABLE IF NOT EXISTS raw_meters (
    meter_id VARCHAR PRIMARY KEY, modulus DOUBLE
);
CREATE TABLE IF NOT EXISTS raw_topology (
    relation_id VARCHAR PRIMARY KEY, parent VARCHAR, child VARCHAR, zone VARCHAR, tenant VARCHAR,
    start_ts TIMESTAMP, end_ts TIMESTAMP
);
CREATE TABLE IF NOT EXISTS raw_readings (
    reading_id VARCHAR PRIMARY KEY, meter_id VARCHAR, read_at TIMESTAMP, kwh DOUBLE
);
CREATE TABLE IF NOT EXISTS raw_replacements (
    replacement_id VARCHAR PRIMARY KEY, old_meter VARCHAR, new_meter VARCHAR, at_ts TIMESTAMP,
    old_end DOUBLE, new_start DOUBLE
);
CREATE TABLE IF NOT EXISTS raw_occupancy (
    occupancy_id VARCHAR PRIMARY KEY, cargo_id VARCHAR, tenant VARCHAR, zone VARCHAR,
    start_ts TIMESTAMP, end_ts TIMESTAMP, volume_m3 DOUBLE, evidence VARCHAR
);
CREATE TABLE IF NOT EXISTS raw_reservations (
    reservation_id VARCHAR PRIMARY KEY, tenant VARCHAR, zone VARCHAR,
    start_ts TIMESTAMP, end_ts TIMESTAMP, volume_m3 DOUBLE, space_price DOUBLE
);
CREATE TABLE IF NOT EXISTS raw_tariffs (
    tariff_id VARCHAR PRIMARY KEY, start_ts TIMESTAMP, end_ts TIMESTAMP, energy_price DOUBLE
);

-- ============ 维度 ============
CREATE TABLE IF NOT EXISTS dim_meter (
    meter_id VARCHAR PRIMARY KEY, modulus DOUBLE, meter_role VARCHAR
);
CREATE TABLE IF NOT EXISTS dim_zone (
    zone VARCHAR, start_ts TIMESTAMP, end_ts TIMESTAMP,
    capacity_m3 DOUBLE, weight DOUBLE, PRIMARY KEY (zone, start_ts)
);
CREATE TABLE IF NOT EXISTS dim_tenant (
    tenant VARCHAR PRIMARY KEY
);

-- ============ 能量事实 ============
-- 换表事实（物理换表，不附加耗能；记录缝口是否与读数吻合）
CREATE TABLE IF NOT EXISTS fact_meter_replacement (
    replacement_id VARCHAR PRIMARY KEY, old_meter VARCHAR, new_meter VARCHAR, at_ts TIMESTAMP,
    old_end DOUBLE, new_start DOUBLE, seam_status VARCHAR, detail VARCHAR
);

-- 单表相邻有效读数解释出的区间增量
CREATE TABLE IF NOT EXISTS fact_meter_interval (
    interval_id BIGINT PRIMARY KEY,
    meter_id VARCHAR, start_ts TIMESTAMP, end_ts TIMESTAMP,
    start_reading_id VARCHAR, end_reading_id VARCHAR,
    start_kwh DOUBLE, end_kwh DOUBLE, delta_kwh DOUBLE,
    interval_status VARCHAR, detail VARCHAR
);

-- 逻辑计量链（跨换表缝合后的连续耗用链，按链上的有效读数切片）
CREATE TABLE IF NOT EXISTS fact_meter_chain_segment (
    segment_id BIGINT PRIMARY KEY,
    chain_key VARCHAR, meter_id VARCHAR, start_ts TIMESTAMP, end_ts TIMESTAMP,
    start_reading_id VARCHAR, end_reading_id VARCHAR,
    start_kwh DOUBLE, end_kwh DOUBLE, delta_kwh DOUBLE,
    segment_status VARCHAR, detail VARCHAR
);

-- 拓扑生效区间（物理接线，按表间关系的生效窗口）
CREATE TABLE IF NOT EXISTS fact_topology_edge (
    relation_id VARCHAR PRIMARY KEY, parent VARCHAR, child VARCHAR, zone VARCHAR, tenant VARCHAR,
    start_ts TIMESTAMP, end_ts TIMESTAMP
);

-- 公共池分段：每个总表在每个拓扑切片上的总量、独立子量、公共负荷证据与残差
CREATE TABLE IF NOT EXISTS fact_pool_segment (
    segment_id BIGINT PRIMARY KEY,
    parent_meter VARCHAR, start_ts TIMESTAMP, end_ts TIMESTAMP,
    parent_kwh DOUBLE,
    independent_child_kwh DOUBLE,
    public_evidence_kwh DOUBLE,
    pool_kwh DOUBLE,
    unexplained_kwh DOUBLE,
    coverage_status VARCHAR,
    pool_status VARCHAR,
    detail VARCHAR
);

-- 公共池向租户的分摊（open 占用与容量冲突的份额以 provisional 保留，不交付结算）
CREATE TABLE IF NOT EXISTS fact_pool_allocation (
    allocation_id BIGINT PRIMARY KEY,
    pool_segment_id BIGINT,
    tenant VARCHAR, zone VARCHAR,
    weighted_volume_hours DOUBLE,
    share_kwh DOUBLE,
    evidence_kwh DOUBLE,
    residual_kwh DOUBLE,
    allocation_status VARCHAR,
    detail VARCHAR
);

-- 独立分表租户的已确认耗用（先从公共池剔除，不再参与分摊）
CREATE TABLE IF NOT EXISTS fact_tenant_direct (
    direct_id BIGINT PRIMARY KEY,
    tenant VARCHAR, relation_id VARCHAR, meter_id VARCHAR, zone VARCHAR,
    start_ts TIMESTAMP, end_ts TIMESTAMP, kwh DOUBLE,
    direct_status VARCHAR, detail VARCHAR
);

-- ============ 空间事实 ============
-- 占用区间（按移库/拆区边界切分后的同一货物位置区间，体积不增加）
CREATE TABLE IF NOT EXISTS fact_occupancy_interval (
    occupancy_interval_id BIGINT PRIMARY KEY,
    occupancy_id VARCHAR, cargo_id VARCHAR, tenant VARCHAR, zone VARCHAR,
    start_ts TIMESTAMP, end_ts TIMESTAMP, volume_m3 DOUBLE, evidence VARCHAR
);

-- 自然日粒度占用（体积小时与加权体积小时）
CREATE TABLE IF NOT EXISTS fact_occupancy_day (
    day DATE, tenant VARCHAR, zone VARCHAR, cargo_id VARCHAR,
    volume_m3 DOUBLE, hours DOUBLE, weight DOUBLE,
    volume_hours DOUBLE, weighted_volume_hours DOUBLE, evidence VARCHAR,
    PRIMARY KEY (day, tenant, zone, cargo_id)
);

-- 预留区间与自然日预留（只形成场地费，不形成耗电）
CREATE TABLE IF NOT EXISTS fact_reservation_interval (
    reservation_id VARCHAR PRIMARY KEY, tenant VARCHAR, zone VARCHAR,
    start_ts TIMESTAMP, end_ts TIMESTAMP, volume_m3 DOUBLE, space_price DOUBLE
);
CREATE TABLE IF NOT EXISTS fact_reservation_day (
    day DATE, reservation_id VARCHAR, tenant VARCHAR, zone VARCHAR,
    volume_m3 DOUBLE, hours DOUBLE, space_price DOUBLE,
    space_fee_yuan DECIMAL(18, 6),
    reservation_status VARCHAR,
    PRIMARY KEY (day, reservation_id)
);

-- 温区可用容量日与容量/超容核对
CREATE TABLE IF NOT EXISTS fact_capacity_day (
    day DATE, zone VARCHAR, capacity_m3 DOUBLE, weight DOUBLE,
    max_occupied_m3 DOUBLE, max_reserved_m3 DOUBLE,
    occupancy_over_m3 DOUBLE, reservation_over_m3 DOUBLE,
    PRIMARY KEY (day, zone)
);

-- ============ 结算 ============
CREATE TABLE IF NOT EXISTS settlement_run (
    run_id BIGINT PRIMARY KEY,
    period_start DATE, period_end DATE,
    created_at TIMESTAMP,
    source_receipt_ids VARCHAR,
    note VARCHAR
);

-- 账单行（能量与场地费；confirmed 可交付，provisional/held 暂不交付）
CREATE TABLE IF NOT EXISTS fact_bill_line (
    run_id BIGINT,
    line_no INTEGER,
    tenant VARCHAR,
    line_type VARCHAR,           -- energy_direct / energy_public / space
    ref_id VARCHAR,              -- 分表/关系/池段/预留标识，供溯源
    zone VARCHAR,
    start_ts TIMESTAMP, end_ts TIMESTAMP,
    base_kwh DOUBLE,
    base_volume_hours DOUBLE,
    unit_price DECIMAL(18, 6),
    amount_yuan DECIMAL(18, 6),
    line_status VARCHAR,
    detail VARCHAR,
    PRIMARY KEY (run_id, line_no)
);

-- 挂起/待分金额（覆盖不全、open 占用、负残差、容量冲突、无电价等）
CREATE TABLE IF NOT EXISTS fact_hold (
    hold_id BIGINT PRIMARY KEY,
    run_id BIGINT,
    hold_type VARCHAR,
    tenant VARCHAR, zone VARCHAR,
    start_ts TIMESTAMP, end_ts TIMESTAMP,
    kwh DOUBLE, amount_yuan DECIMAL(18, 6),
    reason VARCHAR
);

-- 账期汇总（确认金额与挂起金额分列）
CREATE TABLE IF NOT EXISTS bill_summary (
    run_id BIGINT, tenant VARCHAR,
    confirmed_kwh DOUBLE, provisional_kwh DOUBLE,
    confirmed_amount_yuan DECIMAL(18, 2), held_amount_yuan DECIMAL(18, 2),
    space_amount_yuan DECIMAL(18, 2),
    PRIMARY KEY (run_id, tenant)
);

-- ============ 自然日集市 ============
CREATE TABLE IF NOT EXISTS mart_day_energy (
    day DATE, tenant VARCHAR, zone VARCHAR,
    direct_kwh DOUBLE, public_kwh DOUBLE,
    public_evidence_kwh DOUBLE, public_unexplained_kwh DOUBLE,
    energy_status VARCHAR,       -- measured / estimated
    detail VARCHAR,
    PRIMARY KEY (day, tenant, zone)
);

CREATE TABLE IF NOT EXISTS mart_day_pool (
    day DATE, parent_meter VARCHAR,
    parent_kwh DOUBLE, independent_child_kwh DOUBLE,
    public_evidence_kwh DOUBLE, pool_kwh DOUBLE, unexplained_kwh DOUBLE,
    pool_status VARCHAR,
    energy_status VARCHAR,
    PRIMARY KEY (day, parent_meter)
);

CREATE TABLE IF NOT EXISTS mart_day_space (
    day DATE, tenant VARCHAR, zone VARCHAR,
    occupied_m3 DOUBLE, reserved_m3 DOUBLE, capacity_m3 DOUBLE,
    weighted_volume_hours DOUBLE,
    occupancy_over_m3 DOUBLE, reservation_over_m3 DOUBLE,
    PRIMARY KEY (day, tenant, zone)
);

-- ============ 数据质量问题（不静默、不剪零） ============
CREATE TABLE IF NOT EXISTS quality_issue (
    issue_id BIGINT PRIMARY KEY,
    issue_type VARCHAR, severity VARCHAR,
    ref_table VARCHAR, ref_key VARCHAR,
    start_ts TIMESTAMP, end_ts TIMESTAMP,
    message VARCHAR
);
