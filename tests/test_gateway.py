#!/usr/bin/env python3
"""
Hyperliquid Gateway 端到端测试脚本
====================================
运行前请设置环境变量:
    export HYPERLIQUID_PRIVATE_KEY="你的私钥"

可选环境变量:
    export HYPERLIQUID_PROXY_HOST="127.0.0.1"
    export HYPERLIQUID_PROXY_PORT=7890
    export HYPERLIQUID_TEST_SYMBOL="BTC"          # 测试用的交易对
    export HYPERLIQUID_ENABLE_LIVE_TEST="0"       # 是否启用真实下单测试(0=否, 1=是)
    export HYPERLIQUID_LIVE_TEST_SIZE="0.01"      # 真实测试单的数量

使用方法:
    cd /root/.openclaw/workspace/cta_developer
    python3 test_hyperliquid_gateway.py
"""

import os
import sys
import time
import traceback
from datetime import datetime, timedelta

# 将项目根目录加入 sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from vnpy.event import EventEngine, Event
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import (
    EVENT_TICK, EVENT_CONTRACT, EVENT_ACCOUNT, EVENT_POSITION,
    EVENT_ORDER, EVENT_TRADE, EVENT_LOG, EVENT_TIMER
)
from vnpy.trader.object import (
    SubscribeRequest, OrderRequest, CancelRequest, HistoryRequest
)
from vnpy.trader.constant import Exchange, Direction, OrderType, Interval

from vnpy_hyperliquid import HyperliquidGateway


class TestResult:
    def __init__(self, name: str, success: bool, detail: str = "", data=None):
        self.name = name
        self.success = success
        self.detail = detail
        self.data = data
        self.timestamp = datetime.now()


class GatewayTester:
    def __init__(
        self,
        private_key: str,
        proxy_host: str = "",
        proxy_port: int = 0,
        test_symbol: str = "BTC",
        enable_live_test: bool = False,
        live_test_size: float = 0.01,
    ):
        self.private_key = private_key
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.test_symbol = test_symbol
        self.enable_live_test = enable_live_test
        self.live_test_size = live_test_size

        # 引擎初始化
        self.event_engine = EventEngine()
        self.main_engine = MainEngine(self.event_engine)
        self.main_engine.add_gateway(HyperliquidGateway)
        self.gateway: HyperliquidGateway = self.main_engine.gateways["HYPERLIQUID"]

        # 事件缓存
        self._ticks: list = []
        self._contracts: list = []
        self._accounts: list = []
        self._positions: list = []
        self._orders: list = []
        self._trades: list = []
        self._logs: list = []

        # 注册监听器
        self.event_engine.register(EVENT_TICK, self._on_tick)
        self.event_engine.register(EVENT_CONTRACT, self._on_contract)
        self.event_engine.register(EVENT_ACCOUNT, self._on_account)
        self.event_engine.register(EVENT_POSITION, self._on_position)
        self.event_engine.register(EVENT_ORDER, self._on_order)
        self.event_engine.register(EVENT_TRADE, self._on_trade)
        self.event_engine.register(EVENT_LOG, self._on_log)

        self.results: list[TestResult] = []

    # ------------------------------------------------------------------
    # 事件回调
    # ------------------------------------------------------------------
    def _on_tick(self, event: Event):
        self._ticks.append(event.data)

    def _on_contract(self, event: Event):
        self._contracts.append(event.data)

    def _on_account(self, event: Event):
        self._accounts.append(event.data)

    def _on_position(self, event: Event):
        self._positions.append(event.data)

    def _on_order(self, event: Event):
        self._orders.append(event.data)

    def _on_trade(self, event: Event):
        self._trades.append(event.data)

    def _on_log(self, event: Event):
        log = event.data
        self._logs.append(log)
        # 打印关键日志到控制台
        if any(k in log.msg for k in ["connected", "failed", "rejected", "error", "complete", "Fill", "Order"]):
            print(f"    [LOG] {log.msg}")

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------
    def _wait_for_log(self, keyword: str, timeout: float = 30.0) -> bool:
        """等待包含关键字的日志出现"""
        deadline = time.time() + timeout
        checked = 0
        while time.time() < deadline:
            while checked < len(self._logs):
                if keyword in self._logs[checked].msg:
                    return True
                checked += 1
            time.sleep(0.2)
        return False

    def _wait_for_contracts(self, min_count: int = 1, timeout: float = 30.0) -> bool:
        """等待合约查询完成"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self._contracts) >= min_count:
                return True
            time.sleep(0.2)
        return False

    def _wait_for_ticks(self, min_count: int = 1, timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self._ticks) >= min_count:
                return True
            time.sleep(0.2)
        return False

    def _wait_for_orders(self, min_count: int = 1, timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self._orders) >= min_count:
                return True
            time.sleep(0.2)
        return False

    def _wait_for_order_status(self, orderid: str, status_predicate, timeout: float = 30.0) -> bool:
        """等待指定订单满足状态条件"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for o in self._orders:
                if o.orderid == orderid and status_predicate(o.status):
                    return True
            time.sleep(0.2)
        return False

    def _clear_events(self):
        self._ticks.clear()
        self._accounts.clear()
        self._positions.clear()
        self._orders.clear()
        self._trades.clear()
        self._logs.clear()

    def _add_result(self, name: str, success: bool, detail: str = "", data=None):
        r = TestResult(name, success, detail, data)
        self.results.append(r)
        status = "✅ PASS" if success else "❌ FAIL"
        print(f"  [{status}] {name}")
        if detail:
            print(f"       → {detail}")
        return r

    def _get_contract(self, symbol: str = None):
        """获取指定symbol的合约，默认取第一个可用的永续合约"""
        if symbol:
            for c in self._contracts:
                if symbol in c.symbol or c.symbol.startswith(symbol):
                    return c
            return None
        for c in self._contracts:
            if c.product.name == "SWAP":
                return c
        return self._contracts[0] if self._contracts else None

    # ------------------------------------------------------------------
    # 测试用例
    # ------------------------------------------------------------------
    def test_connect(self) -> bool:
        print("\n[Test 1/10] 连接网关...")
        self._clear_events()
        try:
            setting = {
                "Private Key": self.private_key,
                "Proxy Host": self.proxy_host,
                "Proxy Port": self.proxy_port,
            }
            self.main_engine.connect(setting, "HYPERLIQUID")

            # 等待合约查询完成
            ok = self._wait_for_log("Contract query complete", timeout=30.0)
            if not ok:
                self._add_result("连接网关", False, "超时：30秒内未收到合约查询完成")
                return False

            # 验证合约数量
            contract_count = len(self._contracts)
            if contract_count < 100:
                self._add_result("连接网关", False, f"合约数量异常：仅 {contract_count} 个")
                return False

            self._add_result("连接网关", True, f"成功加载 {contract_count} 个合约")
            return True
        except Exception as e:
            self._add_result("连接网关", False, f"异常: {e}")
            traceback.print_exc()
            return False

    def test_contracts(self) -> bool:
        print("\n[Test 2/10] 合约数据检查...")
        try:
            contract = self._get_contract()
            if not contract:
                self._add_result("合约数据", False, "未找到任何合约")
                return False

            checks = []
            if contract.symbol.endswith("_SWAP_HL"):
                checks.append("symbol命名正确")
            else:
                checks.append(f"symbol命名异常: {contract.symbol}")

            if contract.pricetick > 0:
                checks.append(f"pricetick={contract.pricetick}")
            else:
                checks.append(f"pricetick异常: {contract.pricetick}")

            if contract.min_volume > 0:
                checks.append(f"min_volume={contract.min_volume}")
            else:
                checks.append(f"min_volume异常: {contract.min_volume}")

            if contract.size > 0:
                checks.append(f"size={contract.size}")

            ok = contract.pricetick > 0 and contract.min_volume > 0
            self._add_result("合约数据", ok, " | ".join(checks), data=contract)
            return ok
        except Exception as e:
            self._add_result("合约数据", False, f"异常: {e}")
            return False

    def test_subscribe(self) -> bool:
        print("\n[Test 3/10] 行情订阅...")
        self._clear_events()
        try:
            contract = self._get_contract()
            if not contract:
                self._add_result("行情订阅", False, "无可用合约")
                return False

            req = SubscribeRequest(
                symbol=contract.symbol,
                exchange=contract.exchange,
            )
            self.main_engine.subscribe(req, "HYPERLIQUID")

            ok = self._wait_for_ticks(min_count=3, timeout=30.0)
            if not ok:
                self._add_result("行情订阅", False, "超时：30秒内未收到Tick推送")
                return False

            tick = self._ticks[-1]
            details = []
            if tick.bid_price_1 > 0:
                details.append(f"bid1={tick.bid_price_1}")
            if tick.ask_price_1 > 0:
                details.append(f"ask1={tick.ask_price_1}")
            if tick.last_price > 0:
                details.append(f"last={tick.last_price}")

            has_depth = tick.bid_price_1 > 0 and tick.ask_price_1 > 0
            self._add_result("行情订阅", has_depth, " | ".join(details), data=tick)
            return has_depth
        except Exception as e:
            self._add_result("行情订阅", False, f"异常: {e}")
            return False

    def test_account(self) -> bool:
        print("\n[Test 4/10] 账户查询...")
        self._clear_events()
        try:
            ok = self._wait_for_log("marginSummary", timeout=15.0)
            # 如果没有这个关键词，就等 account 事件
            if not ok:
                deadline = time.time() + 15.0
                while time.time() < deadline and not self._accounts:
                    time.sleep(0.2)

            if not self._accounts:
                self._add_result("账户查询", False, "超时：未收到账户数据")
                return False

            acc = self._accounts[-1]
            detail = f"balance={acc.balance:.4f} available={acc.available:.4f} frozen={acc.frozen:.4f}"
            self._add_result("账户查询", True, detail, data=acc)
            return True
        except Exception as e:
            self._add_result("账户查询", False, f"异常: {e}")
            return False

    def test_position(self) -> bool:
        print("\n[Test 5/10] 持仓查询...")
        self._clear_events()
        try:
            # 持仓可能为空，这是正常的
            deadline = time.time() + 15.0
            while time.time() < deadline and not self._positions:
                time.sleep(0.2)

            if self._positions:
                pos = self._positions[-1]
                detail = f"symbol={pos.symbol} dir={pos.direction.value} vol={pos.volume} pnl={pos.pnl:.4f}"
                self._add_result("持仓查询", True, detail, data=pos)
            else:
                self._add_result("持仓查询", True, "当前无持仓（正常）")
            return True
        except Exception as e:
            self._add_result("持仓查询", False, f"异常: {e}")
            return False

    def test_history(self) -> bool:
        print("\n[Test 6/10] 历史K线查询...")
        try:
            contract = self._get_contract()
            if not contract:
                self._add_result("历史K线", False, "无可用合约")
                return False

            end = datetime.now()
            start = end - timedelta(hours=24)
            req = HistoryRequest(
                symbol=contract.symbol,
                exchange=contract.exchange,
                start=start,
                end=end,
                interval=Interval.HOUR,
            )
            bars = self.main_engine.query_history(req, "HYPERLIQUID")

            if not bars:
                self._add_result("历史K线", False, "返回空列表")
                return False

            bar = bars[0]
            detail = f"count={len(bars)} first={bar.datetime} O={bar.open_price} H={bar.high_price} L={bar.low_price} C={bar.close_price} V={bar.volume}"
            self._add_result("历史K线", True, detail, data=bar)
            return True
        except Exception as e:
            self._add_result("历史K线", False, f"异常: {e}")
            traceback.print_exc()
            return False

    def test_send_order(self) -> tuple[bool, str | None]:
        print("\n[Test 7/10] 下单测试（安全限价单，不成交）...")
        self._clear_events()
        try:
            contract = self._get_contract()
            if not contract:
                self._add_result("下单测试", False, "无可用合约")
                return False, None

            # 获取当前价格
            deadline = time.time() + 10.0
            while time.time() < deadline and not self._ticks:
                time.sleep(0.2)
            if not self._ticks:
                self._add_result("下单测试", False, "无法获取当前价格")
                return False, None

            last_px = self._ticks[-1].last_price or self._ticks[-1].bid_price_1
            if last_px <= 0:
                self._add_result("下单测试", False, f"无效价格: {last_px}")
                return False, None

            # 下一个极低价买单（永远不会成交）
            safe_price = round(last_px * 0.5, 2)
            volume = 0.001  # 极小数量

            req = OrderRequest(
                symbol=contract.symbol,
                exchange=contract.exchange,
                direction=Direction.LONG,
                type=OrderType.LIMIT,
                volume=volume,
                price=safe_price,
            )
            orderid = self.main_engine.send_order(req, "HYPERLIQUID")

            if not orderid:
                self._add_result("下单测试", False, "send_order 返回空")
                return False, None

            # 等待订单状态推送
            ok = self._wait_for_order_status(
                orderid,
                lambda s: s.name in ["NOTTRADED", "SUBMITTING"],
                timeout=15.0
            )
            if not ok:
                self._add_result("下单测试", False, f"订单未进入 NOTTRADED 状态，orderid={orderid}")
                return False, orderid

            order = [o for o in self._orders if o.orderid == orderid][-1]
            detail = f"orderid={orderid} status={order.status.name} price={order.price} vol={order.volume}"
            self._add_result("下单测试", True, detail, data=order)
            return True, orderid
        except Exception as e:
            self._add_result("下单测试", False, f"异常: {e}")
            traceback.print_exc()
            return False, None

    def test_cancel_order(self, orderid: str) -> bool:
        print("\n[Test 8/10] 撤单测试...")
        self._clear_events()
        try:
            contract = self._get_contract()
            req = CancelRequest(
                orderid=orderid,
                symbol=contract.symbol,
                exchange=contract.exchange,
            )
            self.main_engine.cancel_order(req, "HYPERLIQUID")

            ok = self._wait_for_order_status(
                orderid,
                lambda s: s.name in ["CANCELLED", "PARTTRADED"],
                timeout=15.0
            )
            if not ok:
                self._add_result("撤单测试", False, f"订单状态未变为 CANCELLED，orderid={orderid}")
                return False

            order = [o for o in self._orders if o.orderid == orderid][-1]
            self._add_result("撤单测试", True, f"orderid={orderid} status={order.status.name}", data=order)
            return True
        except Exception as e:
            self._add_result("撤单测试", False, f"异常: {e}")
            return False

    def test_query_order(self) -> bool:
        print("\n[Test 9/10] 查询活跃订单...")
        self._clear_events()
        try:
            self.gateway.query_order()
            deadline = time.time() + 10.0
            while time.time() < deadline:
                if self._wait_for_log("Active order data received", timeout=2.0):
                    break
                time.sleep(0.2)
            else:
                self._add_result("查询活跃订单", False, "超时未收到响应")
                return False

            count = len([o for o in self._orders if o.is_active()])
            self._add_result("查询活跃订单", True, f"当前活跃订单数: {count}")
            return True
        except Exception as e:
            self._add_result("查询活跃订单", False, f"异常: {e}")
            return False

    def test_live_trade(self) -> bool:
        print("\n[Test 10/10] 真实成交测试（IOC市价单，可能产生手续费）...")
        if not self.enable_live_test:
            self._add_result("真实成交测试", True, "跳过（未启用 HYPERLIQUID_ENABLE_LIVE_TEST）")
            return True

        self._clear_events()
        try:
            contract = self._get_contract()
            if not contract:
                self._add_result("真实成交测试", False, "无可用合约")
                return False

            req = OrderRequest(
                symbol=contract.symbol,
                exchange=contract.exchange,
                direction=Direction.LONG,
                type=OrderType.MARKET,  # IOC市价单
                volume=self.live_test_size,
                price=0,
            )
            orderid = self.main_engine.send_order(req, "HYPERLIQUID")
            if not orderid:
                self._add_result("真实成交测试", False, "send_order 返回空")
                return False

            # 等待成交回报
            ok = self._wait_for_order_status(
                orderid,
                lambda s: s.name in ["ALLTRADED", "PARTTRADED"],
                timeout=30.0
            )
            if not ok:
                self._add_result("真实成交测试", False, f"未收到成交确认，orderid={orderid}")
                return False

            trades = [t for t in self._trades if t.orderid == orderid]
            detail = f"orderid={orderid} trades={len(trades)}"
            if trades:
                detail += f" total_filled={sum(t.volume for t in trades):.6f} avg_price={sum(t.volume*t.price for t in trades)/sum(t.volume for t in trades):.2f}"
            self._add_result("真实成交测试", True, detail)
            return True
        except Exception as e:
            self._add_result("真实成交测试", False, f"异常: {e}")
            traceback.print_exc()
            return False

    # ------------------------------------------------------------------
    # 运行全部测试
    # ------------------------------------------------------------------
    def run(self):
        print("=" * 70)
        print(" Hyperliquid Gateway 端到端测试")
        print("=" * 70)
        print(f" 测试标的: {self.test_symbol}")
        print(f" 代理设置: {self.proxy_host}:{self.proxy_port}" if self.proxy_host else " 代理设置: 无")
        print(f" 真实成交测试: {'启用' if self.enable_live_test else '禁用'}")
        print("=" * 70)

        try:
            # 1. 连接
            if not self.test_connect():
                print("\n❌ 连接失败，后续测试终止")
                self.print_report()
                return

            # 2. 合约数据
            self.test_contracts()

            # 3. 行情订阅
            self.test_subscribe()

            # 4. 账户
            self.test_account()

            # 5. 持仓
            self.test_position()

            # 6. 历史K线
            self.test_history()

            # 7. 下单（安全限价单）
            ok, orderid = self.test_send_order()

            # 8. 撤单
            if ok and orderid:
                self.test_cancel_order(orderid)
            else:
                self._add_result("撤单测试", False, "跳过（下单失败）")

            # 9. 查询活跃订单
            self.test_query_order()

            # 10. 真实成交测试
            self.test_live_trade()

        finally:
            print("\n[Test Cleanup] 断开连接...")
            self.gateway.close()
            time.sleep(2)
            self.print_report()

    def print_report(self):
        print("\n" + "=" * 70)
        print(" 测试报告")
        print("=" * 70)
        passed = sum(1 for r in self.results if r.success)
        total = len(self.results)
        print(f" 总计: {passed}/{total} 通过")
        print("-" * 70)
        for r in self.results:
            status = "✅" if r.success else "❌"
            print(f" {status} {r.name:<25s} {r.detail}")
        print("=" * 70)

        # 关键检查项
        critical_tests = ["连接网关", "合约数据", "行情订阅", "下单测试", "撤单测试"]
        critical_pass = all(
            any(r.name == ct and r.success for r in self.results)
            for ct in critical_tests
        )
        if critical_pass:
            print("\n🎉 核心测试全部通过，网关可用！")
        else:
            print("\n⚠️  核心测试存在失败项，请检查日志后再实盘。")


if __name__ == "__main__":
    private_key = os.environ.get("HYPERLIQUID_PRIVATE_KEY", "").strip()
    if not private_key:
        print("错误: 请设置环境变量 HYPERLIQUID_PRIVATE_KEY")
        print("示例: export HYPERLIQUID_PRIVATE_KEY='你的私钥'")
        sys.exit(1)

    proxy_host = os.environ.get("HYPERLIQUID_PROXY_HOST", "").strip()
    proxy_port = int(os.environ.get("HYPERLIQUID_PROXY_PORT", "0"))
    test_symbol = os.environ.get("HYPERLIQUID_TEST_SYMBOL", "BTC").strip()
    enable_live_test = os.environ.get("HYPERLIQUID_ENABLE_LIVE_TEST", "0") == "1"
    live_test_size = float(os.environ.get("HYPERLIQUID_LIVE_TEST_SIZE", "0.01"))

    tester = GatewayTester(
        private_key=private_key,
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        test_symbol=test_symbol,
        enable_live_test=enable_live_test,
        live_test_size=live_test_size,
    )
    tester.run()
