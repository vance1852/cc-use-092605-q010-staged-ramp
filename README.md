# 实现全场功率爬坡预演与分阶段切换基础平台

本项目是一套可离线运行的 Python 服务端平台，用于管理海上风电场、送出通道、机组资源批次、场站申报、功率分配、调度情景、调度预演阶段计划与机组健康准入。业务状态、登录权限、幂等结果和审计事件保存在 SQLite 中，适合生产调度、设备质量、风险与审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/wind_dispatch/`：场站、送出通道、机组可用量、功率申报、日前分配、调度情景和调度预演阶段计划；
- `src/turbine_health/`：机组健康协议、测点导入、异常复核、分析任务租约和健康决定；
- `src/grid_qualification/`：并网机组批次、检测数据、分析、账号登录与质量审批；
- `fixtures/`：离线验收使用的检测协议和结构化测点；
- `tests/`：核心规则、权限、错误边界、事务、API 和命令行验收测试。

## 调度预演阶段计划

多个子项目共同承担调峰指令时，调度人员先固定一份运行快照（机组能力、海况观测、通道设施），再提交目标功率轨迹与降额策略，系统据此把预演展开为可执行的阶段计划：

1. `POST /rehearsal/snapshots` 固定快照，同时戳记 `units`、`sea_state`、`corridors` 三类底层来源版本；`POST /rehearsal/sources/{kind}/updates` 登记来源版本变化；
2. `POST /rehearsal/plans` 按轨迹与快照确定性地生成 准备 → 试送 → 扩容 → 稳定运行 四个阶段，每阶段给出进入条件、目标功率与必须保留的回退余量，并指出 18MW 机组能力、海缆热容量、500kV 无功补偿站、旋转备用四条边界中谁先触限（`binding_constraints` 与各阶段 `blocking_constraints`）；
3. `POST /rehearsal/plans/{id}/confirm` 确认计划并在同一事务内按容量池预留机组能力、海缆、无功与旋转备用四类容量，容量不足则整体失败；
4. `POST /rehearsal/plans/{id}/receipts` 登记现场回执，按回执编号幂等去重，重复到达只计算一次；当前阶段回执生效后才允许 `POST /rehearsal/plans/{id}/advance` 推进；
5. 底层来源版本一旦变化，快照失效，旧计划的确认与推进都会被拒绝，只能重新固定快照另建计划；
6. 发生异常时 `POST /rehearsal/plans/{id}/rollback` 在保留全部已执行证据（回执与阶段事件只标记不删除）的前提下退回仍安全的阶段，可携带实测上限计算安全目标，无安全阶段则全面退出并释放预留；
7. `GET /rehearsal/plans/{id}` 返回当前阶段、各项容量来源与池余量、阻断推进的约束，以及还能回退到哪里。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m wind_dispatch.acceptance --workspace .
PYTHONPATH=src python3 -m turbine_health.acceptance --workspace .
PYTHONPATH=src python3 -m grid_qualification.acceptance
```

三条命令使用临时 SQLite 数据库完成场站与通道登记、功率申报分配、健康测点分析和并网审批，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m wind_dispatch.api --database wind.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m turbine_health.api --database health.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m grid_qualification.api --database grid.sqlite3 --host 127.0.0.1 --port 8082
```

服务均提供 `GET /health`，其余接口使用 JSON。账号登录和角色权限由服务端校验，进程重启后可以继续查询 SQLite 中的业务状态与审计历史。
