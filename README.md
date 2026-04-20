# vnpy-hyperliquid

Hyperliquid 交易所的 VeighNa (vn.py) 网关插件，支持永续合约 (Perp/SWAP) 的行情订阅、下单、撤单、持仓查询和历史数据获取。

## 特性

- **完全对标 vnpy_okx**: 功能和 API 设计与官方 OKX 网关保持一致，降低学习成本
- **私钥认证**: 通过 Hyperliquid Python SDK 进行签名认证，无需 API Key/Secret
- **安全的撤单机制**: 使用 `cancelByCloid` 避免 oid 映射竞态条件
- **重复成交去重**: 通过 `filled_tids` 集合处理 `userEvents` 和 `userFills` 双通道重复推送
- **符号命名避冲**: `{name}_SWAP_HL` 格式避免与其他网关 `vt_symbol` 冲突

## 安装

```bash
# 从源码安装
pip install -e .

# 或安装依赖
pip install -r requirements.txt
```

## 快速开始

```python
from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy_hyperliquid import HyperliquidGateway

# 初始化引擎
event_engine = EventEngine()
main_engine = MainEngine(event_engine)
main_engine.add_gateway(HyperliquidGateway)

# 连接设置
setting = {
    "Private Key": "你的私钥",
    "Proxy Host": "",
    "Proxy Port": 0,
}
main_engine.connect(setting, "HYPERLIQUID")
```

## 测试

```bash
export HYPERLIQUID_PRIVATE_KEY="你的私钥"
python tests/test_gateway.py
```

测试脚本包含 10 个测试用例：连接、合约、行情、账户、持仓、历史K线、下单、撤单、查询活跃订单、可选真实成交。

## 项目结构

```
vnpy-hyperliquid/
├── vnpy_hyperliquid/          # 网关核心代码
│   ├── __init__.py
│   ├── hyperliquid_gateway.py    # 主网关实现 (~1200 行)
│   └── py.typed
├── tests/
│   └── test_gateway.py           # 端到端测试脚本
├── scripts/
│   ├── patch_gateway.py          # Phase 4 核心补丁
│   └── patch_okx_parity.py       # OKX 功能对标补丁
├── docs/
│   └── assessment.md             # 开发评估报告
├── setup.py
├── pyproject.toml
├── requirements.txt
└── README.md
```

## 开发记录

| 阶段 | 状态 | 说明 |
|------|------|------|
| Phase 1: 基础框架 | ✅ | REST + WebSocket 连接、合约查询、行情订阅 |
| Phase 2: 账户与持仓 | ✅ | 账户余额、持仓查询 |
| Phase 3: 历史数据 | ✅ | K线历史数据获取 |
| Phase 4: 交易核心 | ✅ | 下单、撤单、订单状态推送、成交回报 |
| Phase 5: OKX 功能对标 | ✅ | on_error、query_order、重连状态同步 |
| 盘后验证 | ⏳ | 待实盘测试验证 |

## 贡献

本项目基于 [xldistance/vnpy_hyperliquid](https://github.com/xldistance/vnpy_hyperliquid) 社区版本进行重构和完善。

## License

MIT
