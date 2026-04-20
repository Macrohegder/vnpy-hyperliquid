# Hyperliquid-vnpy 交易接口可行性评估报告

**日期**: 2026-04-20
**参考网关**: vnpy_okx (OKX 官方接口)
**目标交易所**: Hyperliquid (https://hyperliquid.xyz)
**评估方法**: 对比参考网关 + 实时 API 探针 + 官方 SDK 分析

---

## 1. 背景说明

本报告基于之前已固化为 skill 的 gateway 评估方法 (`vnpy-gateway-evaluation`)，结合 2026-04-20 最新 Hyperliquid 官方 API 文档、实时 API 探针和官方 Python SDK (`hyperliquid-python-sdk`)进行重新验证。

**API 基础信息**:
- REST Host: `https://api.hyperliquid.xyz`
- WebSocket Host: `wss://api.hyperliquid.xyz/ws`
- 查询 Endpoint: `POST /info` (body 中通过 `type` 字段区分)
- 交易 Endpoint: `POST /exchange`
- 官方 SDK: `hyperliquid-python-sdk` (依赖 `eth-account`, `msgpack`, `eth-utils`)

---

## 2. 参考网关分析 (vnpy_okx)

OKX 网关采用 **1 Gateway + 1 RestApi + 3 WebSocket** 架构:

| 组件 | 职责 |
|------|------|
| `OkxGateway` | 统一入口，管理连接、订单、订阅 |
| `RestApi` | HMAC 签名、合约查询、历史 K 线、账户查询 |
| `PublicApi` (WS) | 市场数据 (ticker, depth, trades) |
| `PrivateApi` (WS) | 订单发送、撤单、账户/持仓推送 |
| `BusinessApi` (WS) | 价差合约 (spread trading) |

**OKX 认证**: HMAC-SHA256，基于 API Key + Secret + Passphrase，WebSocket 需要登录握手。

---

## 3. Hyperliquid API 现状 (2026-04 最新)

### 3.1 合约元数据
- `POST /info {"type": "meta"}` 返回 229 个永续合约 (perps)
- 每个合约包含: `name`, `szDecimals`, `maxLeverage`, `marginTableId`
- **关键发现**: 没有 `pricetick` (价格最小变动位)。价格精度由规则推导:
  - Perp: 最多 5 位有效数字，小数位不超过 `6 - szDecimals`
  - Spot: 最多 5 位有效数字，小数位不超过 `8 - szDecimals`
  - 整数价格始终允许（不受有效数字限制）
- 例如 BTC (szDecimals=5): 价格约 74,000 已占 5 位有效数字，只能下整数价格（tick 实际为 1.0）
- 例如某低价格币种 (价格 ~0.5, szDecimals=2): 可以有小数位 `6-2=4` 位，有效数字 5 位允许 `0.1234`

### 3.2 市场数据
- `allMids`: 539 个价格键（perp + spot 联合）
- `l2Book`: 二级深度，格式 `{"coin", "time", "levels": [[bids...], [asks...]]}`
- `candleSnapshot`: K 线历史数据，需要嵌套 `req` 对象
- `metaAndAssetCtxs`: 同步返回 meta + 实时指标 (`funding`, `oraclePx`, `markPx`, `midPx`, `openInterest`)

### 3.3 账户与持仓
- `clearinghouseState`: 查询账户状态，包含:
  - `marginSummary`: `accountValue`, `totalMarginUsed`, `totalNtlPos`, `totalRawUsd`
  - `assetPositions`: 每个币种的持仓 (`szi`, `entryPx`, `leverage`, `liquidationPx`, `unrealizedPnl`)
  - `withdrawable`: 可提现金额

### 3.4 签名机制 (核心差异)
Hyperliquid 使用 **EVM 钱包签名**，与 OKX 的 HMAC 完全不同:

1. 用户需要提供私钥 (private key)，生成 `eth_account.Account`
2. 交易时，构建 action JSON，计算 `nonce` (毫秒时间戳)
3. 用 `msgpack` 序列化 action + nonce + vaultAddress，然后 `keccak` 哈希
4. 使用 `eth_account` 的 `unsafe_sign_hash` 或 `sign_typed_data` 生成 EIP-712 签名 (`r, s, v`)
5. 将 `action + nonce + signature` POST 到 `/exchange`

官方 SDK 中的核心方法:
```python
signature = sign_l1_action(wallet, order_action, vault_address, timestamp, expires_after, is_mainnet)
self._post_action(order_action, signature, timestamp)
```

### 3.5 WebSocket
- 单一 endpoint `wss://api.hyperliquid.xyz/ws`
- **无需登录握手**（不像 OKX 需要 WS 登录）
- 订阅格式: `{"method": "subscribe", "subscription": {"type": "l2Book", "coin": "BTC"}}`
- 心跳: `{"method": "ping"}` 每 50 秒
- 支持的频道:
  - 公开: `allMids`, `l2Book`, `trades`, `candle`, `bbo`, `activeAssetCtx`
  - 用户: `userEvents`, `orderUpdates`, `userFills`, `userFundings`, `webData2`
- 用户频道直接通过地址订阅，无需预先认证握手

### 3.6 订单与交易
- 订单通过 **REST** `/exchange` 发送，而不是 WebSocket
- 支持批量下单 (`bulkOrders`)
- 支持修改订单 (`modify`)
- 支持按 `oid` 或 `cloid` 撤单
- 订单状态通过 WebSocket `orderUpdates` 推送
- 成交通过 WebSocket `userFills` 推送

---

## 4. 关键差异对比 (OKX vs Hyperliquid)

| 维度 | OKX | Hyperliquid | 影响程度 |
|------|-----|-------------|--------|
| **认证** | HMAC-SHA256 (API Key/Secret) | EVM 私钥签名 (EIP-712/keccak) | 高：需要全新签名层 |
| **订单通道** | WebSocket (PrivateApi) | REST POST `/exchange` | 中：需要在 RestApi 或单独类中实现交易 |
| **WS 架构** | Public + Private + Business 三套 | 单一 WS，无登录握手 | 低：简化架构，不需要复制 OKX 三分类 |
| **合约精度** | 直接提供 `tickSz` | 无 `pricetick`，需规则计算 | 中：需要动态精度处理 |
| **账户模式** | 多币种账户体系 | 单一 USDC 计价账户 | 中：账户映射简化，但需处理 cross margin |
| **币种空间** | `instId` 字符串 | `coin` 字符串 + 内部 `asset` 整数 ID | 低：直接映射 name |
| **依赖库** | 标准库 | `eth-account`, `msgpack`, `eth-utils` | 中：需要安装额外依赖 |
| **测试网** | Demo 服务器 | 无正式测试网，可用小额资金测试 | 中：测试风险较高 |

---

## 5. 数据模型映射

### 5.1 ContractData
| vnpy 字段 | Hyperliquid 来源 | 说明 |
|-----------|-------------------|------|
| `symbol` | `name` (e.g. "BTC") | 直接使用 HL 名称 |
| `name` | `name` | 同 symbol |
| `pricetick` | 规则计算 | `10 ** -(6 - szDecimals)` (仅作为参考，实际订单需按有效数字规则舍入) |
| `min_volume` | `10 ** -szDecimals` | 最小交易数量 |
| `size` | `maxLeverage` | 最大杠杆倍数 |
| `product` | 固定 `Product.SWAP` | 当前仅考虑 perp |

### 5.2 AccountData
| vnpy 字段 | Hyperliquid 来源 |
|-----------|-------------------|
| `accountid` | 固定 "USDC" 或地址 |
| `balance` | `marginSummary.accountValue` |
| `available` | `withdrawable` |
| `frozen` | `accountValue - withdrawable` |

### 5.3 PositionData
| vnpy 字段 | Hyperliquid 来源 |
|-----------|-------------------|
| `symbol` | `position.coin` |
| `direction` | `Direction.NET` (HL 是 one-way 模式) |
| `volume` | `abs(float(position.szi))` |
| `price` | `float(position.entryPx)` |
| `pnl` | `float(position.unrealizedPnl)` |

### 5.4 OrderData / TradeData
| vnpy 字段 | Hyperliquid 来源 |
|-----------|-------------------|
| `orderid` | `cloid` (用户自定义) 或 `oid` (系统分配) |
| `direction` | `LONG` if `is_buy=True` else `SHORT` |
| `type` | LIMIT / MARKET / FOK / FAK (映射 `tif`: Gtc/Ioc/Alo) |
| `status` | 基于 `status`/`filledTotalSz` 判断 |

---

## 6. 网关架构设计

基于以上差异，推荐架构:

```
HyperliquidGateway (BaseGateway)
    |
    +-- RestApi (RestClient 拓展或独立封装)
    |       职责: 合约查询、历史 K 线、订单发送/撤单/修改、账户查询
    |       签名: EIP-712 / keccak + eth-account
    |
    +-- WsApi (WebsocketClient 拓展)
            职责: 市场数据 (allMids/l2Book/trades) + 用户推送 (orderUpdates/userFills/userEvents)
            无需登录握手，连接后直接订阅
```

**不推荐复制 OKX 的三分类架构**，因为 HL 的 WS 不需要登录握手，且只有一个 endpoint。

---

## 7. 可行性结论

### 7.1 是否可以通过 vnpy 进行交易？
**结论: 完全可行**。

Hyperliquid 提供了完整的 REST + WebSocket API，官方 Python SDK 成熟且维护积极。虽然其认证模型 (EVM 签名) 与传统中心化交易所差异较大，但这些均可以在 vnpy 网关中封装。

### 7.2 主要工作量估算
- 签名层封装: 1-2 天 (可借鉴官方 SDK 的 `sign_l1_action`)
- 合约查询与精度处理: 1 天
- 市场数据 (Tick/Bar): 1 天
- 账户/持仓推送: 1 天
- 订单发送/撤单/状态推送: 2 天
- 历史数据与整合测试: 2 天

**预计总工期: 8-10 天**

### 7.3 技术风险
1. **签名错误**: EVM 签名错误导致的错误信息不直观。建议先用 SDK 测试通过后再移植到 vnpy。
2. **价格精度**: 动态有效数字限制可能导致某些价位的订单被拒绝。需要在 `send_order` 中实现严格的价格校验。
3. **测试网缺失**: HL 没有正式测试网，测试需要用极小金额在 mainnet 进行。
4. **滑点与建造者费用**: 市价订单需要计算滑点价格，官方 SDK 提供了参考实现。
5. **额外依赖**: `eth-account`, `msgpack`, `eth-utils` 等库需要加入依赖。

---

## 8. 分阶段开发计划

### Phase 1: 骨架与连接 (Day 1-2)
- [ ] 创建 `HyperliquidGateway` 类，实现 `connect()`, `close()`
- [ ] 实现 `RestApi`: 基础 HTTP 封装，连接 `/info` 和 `/exchange`
- [ ] 实现 `WsApi`: WebSocket 连接、心跳、订阅管理
- [ ] 合约查询 (`meta` + `spotMeta`)并映射为 `ContractData`

### Phase 2: 市场数据 (Day 3-4)
- [ ] `allMids` + `l2Book` 订阅与 `TickData` 映射
- [ ] 合并 `allMids` (最新价) 与 `l2Book` (深度) 生成完整 Tick
- [ ] `candleSnapshot` 历史数据查询实现 `query_history()`

### Phase 3: 账户与持仓 (Day 5)
- [ ] 实现 `clearinghouseState` 查询，映射 `AccountData` 和 `PositionData`
- [ ] WebSocket `userEvents` 订阅与推送
- [ ] 实现 `query_account()` 和 `query_position()`

### Phase 4: 交易核心 (Day 6-8)
- [ ] 实现 EVM 签名层 (`sign_l1_action` 等效实现)
- [ ] 订单发送 `send_order()`: 构建 action → 签名 → POST `/exchange`
- [ ] 撤单 `cancel_order()`: 按 `oid` 或 `cloid` 撤单
- [ ] WebSocket `orderUpdates` 推送与 `OrderData` 更新
- [ ] WebSocket `userFills` 推送与 `TradeData` 生成
- [ ] 价格精度校验与舍入函数

### Phase 5: 整合测试 (Day 9-10)
- [ ] 端到端连接测试
- [ ] 小额订单下单测试 (mainnet 小金额)
- [ ] 心跳、重连、异常处理测试
- [ ] 与 vnpy CtaStrategy 模块整合测试
- [ ] 编写 `README.md` 和 `setup.py`

---

## 9. 建议与决策

**建议: 立即启动正式开发**。

Hyperliquid 是目前流动性最好的去中心化交易手续费最低的期货交易所之一，其 API 稳定性和完备性已经过充分验证。开发一个完整的 vnpy 网关可以让现有的 CTA 策略无缝迁移到 Hyperliquid，尤其对于需要低手续费和深度流动性的策略具有重要价值。

唯一需要注意的是**签名依赖** (`eth-account` 等)和**测试方案**（无测试网，需小额 mainnet 测试）。
