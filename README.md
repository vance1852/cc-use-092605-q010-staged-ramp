# 实现全场功率爬坡预演与分阶段切换基础平台

本项目是一套可离线运行的 Python 服务端平台，用于管理海上风电场、送出通道、机组资源批次、场站申报、功率分配、调度情景与机组健康准入。业务状态、登录权限、幂等结果和审计事件保存在 SQLite 中，适合生产调度、设备质量、风险与审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/wind_dispatch/`：场站、送出通道、机组可用量、功率申报、日前分配、调度情景和调峰阶段计划；
- `src/turbine_health/`：机组健康协议、测点导入、异常复核、分析任务租约和健康决定；
- `src/grid_qualification/`：并网机组批次、检测数据、分析、账号登录与质量审批；
- `fixtures/`：离线验收使用的检测协议和结构化测点；
- `tests/`：核心规则、权限、错误边界、事务、API 和命令行验收测试。

## 调峰阶段计划

针对多个百万千瓦级子项目共同承担的调峰指令，平台把调度预演固化为可执行的阶段计划：

1. 调度人员通过 `POST /ramp/snapshots` 固定机组能力、海况观测和通道设施快照，系统同时记录底层数据版本指纹；
2. `POST /ramp/plans` 提交目标功率轨迹和降额策略，系统切成准备、试送、扩容、稳定运行四个阶段，逐步列出进入条件、必须保留的回退余量，并指出机组能力、海缆热容量、无功补偿、旋转备用中会先碰到的边界；
3. `POST /ramp/plans/{id}/confirm` 确认计划时在同一事务内按来源预留容量，预留不足或底层版本已变化都会被拒绝；
4. 现场回执 `POST /ramp/plans/{id}/receipts` 按幂等键去重，重复到达只计算一次；`POST /ramp/plans/{id}/advance` 校验版本、回执和下一阶段进入条件后推进；
5. 发生异常时 `POST /ramp/plans/{id}/abort` 在保留已执行回执证据的前提下退回仍有证据支撑的安全阶段，`POST /ramp/plans/{id}/retire` 关闭计划并释放预留；
6. `GET /ramp/plans/{id}` 说明当前阶段、各项容量来源与预留、阻断推进的约束以及还能回退到哪里。

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
