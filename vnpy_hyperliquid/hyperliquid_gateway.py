import json
import time
from copy import copy
from datetime import datetime
from decimal import Decimal
from typing import Callable, cast

from eth_account import Account
from eth_account.signers.local import LocalAccount

from hyperliquid.utils.signing import (
    get_timestamp_ms,
    order_request_to_order_wire,
    order_wires_to_order_action,
    sign_l1_action,
    OrderRequest as HlOrderRequest,
    OrderType as HlOrderType,
    OrderWire,
)
from hyperliquid.utils.types import Cloid

from vnpy.event import EventEngine, Event, EVENT_TIMER
from vnpy.trader.constant import (
    Direction,
    Exchange,
    Interval,
    Offset,
    OrderType,
    Product,
    Status,
)
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.utility import round_to, ZoneInfo
from vnpy.trader.object import (
    AccountData,
    BarData,
    CancelRequest,
    ContractData,
    HistoryRequest,
    OrderData,
    OrderRequest,
    PositionData,
    SubscribeRequest,
    TickData,
    TradeData,
)
from vnpy_rest import Request, Response, RestClient
from vnpy_websocket import WebsocketClient


CHINA_TZ: ZoneInfo = ZoneInfo("Asia/Shanghai")

# Hosts
REST_HOST: str = "https://api.hyperliquid.xyz"
WS_HOST: str = "wss://api.hyperliquid.xyz/ws"

# Status map
STATUS_HL2VT: dict[str, Status] = {
    "open": Status.NOTTRADED,
    "filled": Status.ALLTRADED,
    "canceled": Status.CANCELLED,
    "rejected": Status.REJECTED,
    "marginCanceled": Status.CANCELLED,
}

# Order type map
ORDERTYPE_VT2HL: dict[OrderType, HlOrderType] = {
    OrderType.LIMIT: {"limit": {"tif": "Gtc"}},
    OrderType.MARKET: {"limit": {"tif": "Ioc"}},
    OrderType.FAK: {"limit": {"tif": "Ioc"}},
    OrderType.FOK: {"limit": {"tif": "Alo"}},
}
ORDERTYPE_HL2VT: dict[str, OrderType] = {
    "Gtc": OrderType.LIMIT,
    "Ioc": OrderType.FAK,
    "Alo": OrderType.FOK,
}

# Direction map
DIRECTION_VT2HL: dict[Direction, bool] = {
    Direction.LONG: True,
    Direction.SHORT: False,
}
DIRECTION_HL2VT: dict[bool, Direction] = {
    True: Direction.LONG,
    False: Direction.SHORT,
}

# Interval map
INTERVAL_VT2HL: dict[Interval, str] = {
    Interval.MINUTE: "1m",
    Interval.HOUR: "1h",
    Interval.DAILY: "1d",
}

# HL uses "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"


def parse_timestamp(ts: int) -> datetime:
    """Parse millisecond timestamp to datetime."""
    return datetime.fromtimestamp(ts / 1000, CHINA_TZ)


def get_float_value(data: dict, key: str) -> float:
    """Safely get float value from dict."""
    value = data.get(key, "")
    if value == "" or value is None:
        return 0.0
    return float(value)


def round_hyperliquid_price(price: float, sz_decimals: int, is_spot: bool = False) -> float:
    """
    Round price according to Hyperliquid rules.
    - Up to 5 significant figures
    - Decimal places <= MAX_DECIMALS - szDecimals
    - Integer prices always allowed
    """
    max_decimals = 8 if is_spot else 6
    max_frac = max_decimals - sz_decimals
    if max_frac < 0:
        max_frac = 0

    # First round to max allowed decimals
    price = round(price, max_frac)

    # Then enforce 5 significant figures
    # If price >= 10000, it already has >=5 sig figs as integer, so keep integer
    if price >= 100000:
        return round(price)
    if price >= 10000:
        # 5 sig figs means no decimal if >= 10000
        return round(price)
    # Use 5 significant figures
    return round(float(f"{price:.5g}"), max_frac)


class HyperliquidGateway(BaseGateway):
    """
    Hyperliquid trading gateway for VeighNa.
    Supports perpetual contracts (perps) only for now.
    """

    default_name: str = "HYPERLIQUID"

    default_setting: dict = {
        "Private Key": "",
        "Proxy Host": "",
        "Proxy Port": 0,
        # Account publishing:
        # - full: publish every perp dex account (USDC, USDC_XYZ, ...) + any enabled aggregates
        # - minimal: only publish 3 accounts: XYZ + PERPS + SPOT (recommended for UI clarity)
        "Account Publish Mode": "full",   # "full" or "minimal"
        "Minimal XYZ AccountId": "XYZ",
        "Minimal PERPS AccountId": "PERPS",
        "Minimal SPOT AccountId": "SPOT",
        # Market data subscriptions
        # Note: l2Book is the heaviest channel. If you don't need bid/ask depth, disable it to reduce CPU.
        "Subscribe L2Book": True,
        "Subscribe Trades": True,
        "Subscribe ActiveAssetCtx": True,
        # Tick event throttling (align with okx-style consolidated tick push)
        # 0 means no throttling (NOT recommended).
        "Tick Push Interval (ms)": 200,
        # Account aggregation
        # If enabled, query clearinghouseState across ALL perp dexs (including filtered-out ones)
        # and publish an extra AccountData with summed balance/available.
        # Warning: if you have many builder dexs, this will increase REST requests.
        "Aggregate All Perp Dex Accounts": False,
        "Aggregate AccountId": "USDC_TOTAL",
        # Total equity aggregation (align with HL UI "total account value", including unrealized PnL)
        # This aggregates:
        # 1) Perp: sum of clearinghouseState.marginSummary.accountValue across all perp dexs
        # 2) Spot: value of spotClearinghouseState balances marked by spotMetaAndAssetCtxs markPx (USDC=1)
        # Note: This is still "trading account" equity. Vaults/custody outside clearinghouse/spot are not included.
        "Aggregate Total Equity": False,
        "Aggregate Equity AccountId": "EQUITY_TOTAL",
        # DEX/account filtering (builder-deployed perp dexs)
        # - "default" represents the standard Hyperliquid perp dex (dex="")
        # - empty string means "no filter" (load all dexs)
        "Perp Dex Include": "",   # e.g. "default,xyz"
        "Perp Dex Exclude": "",   # e.g. "abc,test"
        "Perp Dex Regex": "",     # e.g. "^(default|prod_.*)$"
    }

    exchanges: list[Exchange] = [Exchange.GLOBAL]

    def __init__(self, event_engine: EventEngine, gateway_name: str) -> None:
        super().__init__(event_engine, gateway_name)

        self.private_key: str = ""
        self.proxy_host: str = ""
        self.proxy_port: int = 0

        self.wallet: LocalAccount | None = None

        self.orders: dict[str, OrderData] = {}
        self.local_orderids: set[str] = set()

        self.symbol_contract_map: dict[str, ContractData] = {}
        self.name_contract_map: dict[str, ContractData] = {}

        # Order tracking: local_orderid <-> exchange oid/cloid
        self.orderid_cloid_map: dict[str, str] = {}
        self.cloid_orderid_map: dict[str, str] = {}
        self.oid_orderid_map: dict[int, str] = {}
        self._cloid_counter: int = 0

        # Fill dedup: avoid double counting from userEvents + userFills
        self.filled_tids: set[int] = set()

        # Meta data
        self.meta: dict = {}
        self.asset_to_sz_decimals: dict[int, int] = {}
        self.name_to_asset: dict[str, int] = {}
        self.name_to_coin: dict[str, str] = {}
        self.asset_to_name: dict[int, str] = {}

        # Perp dex support
        self.perp_dexs: list[str] = []
        self.all_perp_dexs: list[str] = []
        self.perp_dex_to_offset: dict[str, int] = {}
        self.name_to_dex: dict[str, str] = {}

        self.rest_api: RestApi = RestApi(self)
        self.ws_api: WsApi = WsApi(self)

        self.ping_count: int = 0
        self.ping_interval: int = 20

        self.subscribed_symbols: set[str] = set()

        # Market data behavior
        self.subscribe_l2book: bool = True
        self.subscribe_trades: bool = True
        self.subscribe_active_asset_ctx: bool = True
        self.tick_push_interval_ms: int = 200

        # Account publishing behavior
        self.account_publish_mode: str = "full"
        self.minimal_xyz_accountid: str = "XYZ"
        self.minimal_perps_accountid: str = "PERPS"
        self.minimal_spot_accountid: str = "SPOT"

        # Account aggregation behavior
        self.aggregate_all_perp_dex_accounts: bool = False
        self.aggregate_accountid: str = "USDC_TOTAL"
        self.aggregate_total_equity: bool = False
        self.aggregate_equity_accountid: str = "EQUITY_TOTAL"

        # DEX filtering
        self.perp_dex_include: set[str] = set()
        self.perp_dex_exclude: set[str] = set()
        self.perp_dex_regex: str = ""
        self._perp_dex_regex_compiled = None

        # WS keepalive/restart
        self.ws_heartbeat_timeout: int = 60          # seconds without any packet => restart
        self.ws_reconnect_min_delay: int = 3         # seconds
        self.ws_reconnect_max_delay: int = 60        # seconds
        self._ws_next_restart_ts: float = 0.0
        self._ws_restart_delay: int = self.ws_reconnect_min_delay

    @staticmethod
    def _normalize_dex_label(dex_name: str) -> str:
        """Normalize dex name for filtering. default dex uses empty string in API."""
        return (dex_name or "default").strip().lower()

    def _load_dex_filter_setting(self, setting: dict) -> None:
        """Parse dex include/exclude/regex from gateway setting."""
        include_raw: str = (setting.get("Perp Dex Include") or "").strip()
        exclude_raw: str = (setting.get("Perp Dex Exclude") or "").strip()
        regex_raw: str = (setting.get("Perp Dex Regex") or "").strip()

        def split_list(text: str) -> set[str]:
            if not text:
                return set()
            parts = []
            for p in text.replace(";", ",").split(","):
                p = p.strip()
                if p:
                    parts.append(p)
            return {self._normalize_dex_label(p if p.lower() != "default" else "") for p in parts}

        self.perp_dex_include = split_list(include_raw)
        self.perp_dex_exclude = split_list(exclude_raw)
        self.perp_dex_regex = regex_raw
        self._perp_dex_regex_compiled = None

        if regex_raw:
            try:
                import re
                self._perp_dex_regex_compiled = re.compile(regex_raw, re.IGNORECASE)
            except Exception as e:
                self.write_log(f"Invalid Perp Dex Regex='{regex_raw}', ignored: {e}")
                self.perp_dex_regex = ""

        if self.perp_dex_include or self.perp_dex_exclude or self.perp_dex_regex:
            self.write_log(
                "Perp dex filter enabled: "
                f"include={sorted(self.perp_dex_include) if self.perp_dex_include else 'ALL'}, "
                f"exclude={sorted(self.perp_dex_exclude) if self.perp_dex_exclude else 'NONE'}, "
                f"regex={self.perp_dex_regex or 'NONE'}"
            )

    def is_dex_enabled(self, dex_name: str) -> bool:
        """Return whether a given perp dex (builder dex) should be loaded."""
        label: str = self._normalize_dex_label(dex_name)

        if self.perp_dex_include and label not in self.perp_dex_include:
            return False
        if self.perp_dex_exclude and label in self.perp_dex_exclude:
            return False
        if self._perp_dex_regex_compiled and not self._perp_dex_regex_compiled.search(label):
            return False
        return True

    def connect(self, setting: dict) -> None:
        self.private_key = setting["Private Key"]
        self.proxy_host = setting["Proxy Host"]
        self.proxy_port = setting["Proxy Port"]

        # Account publishing settings
        self.account_publish_mode = str(setting.get("Account Publish Mode", "full") or "full").strip().lower()
        if self.account_publish_mode not in ("full", "minimal"):
            self.account_publish_mode = "full"
        self.minimal_xyz_accountid = (setting.get("Minimal XYZ AccountId") or "XYZ").strip() or "XYZ"
        self.minimal_perps_accountid = (setting.get("Minimal PERPS AccountId") or "PERPS").strip() or "PERPS"
        self.minimal_spot_accountid = (setting.get("Minimal SPOT AccountId") or "SPOT").strip() or "SPOT"

        # Market data settings
        self.subscribe_l2book = bool(setting.get("Subscribe L2Book", True))
        self.subscribe_trades = bool(setting.get("Subscribe Trades", True))
        self.subscribe_active_asset_ctx = bool(setting.get("Subscribe ActiveAssetCtx", True))
        try:
            self.tick_push_interval_ms = int(setting.get("Tick Push Interval (ms)", 200) or 0)
            if self.tick_push_interval_ms < 0:
                self.tick_push_interval_ms = 0
        except Exception:
            self.tick_push_interval_ms = 200

        # Account aggregation settings
        self.aggregate_all_perp_dex_accounts = bool(setting.get("Aggregate All Perp Dex Accounts", False))
        self.aggregate_accountid = (setting.get("Aggregate AccountId") or "USDC_TOTAL").strip() or "USDC_TOTAL"
        self.aggregate_total_equity = bool(setting.get("Aggregate Total Equity", False))
        self.aggregate_equity_accountid = (setting.get("Aggregate Equity AccountId") or "EQUITY_TOTAL").strip() or "EQUITY_TOTAL"

        self._load_dex_filter_setting(setting)

        # Init wallet
        try:
            key = self.private_key
            if key.startswith("0x"):
                key = key[2:]
            self.wallet = Account.from_key(key)
        except Exception as e:
            self.write_log(f"Failed to load private key: {e}")
            return

        self.rest_api.connect(
            self.wallet,
            self.proxy_host,
            self.proxy_port,
        )

    def connect_ws_api(self) -> None:
        self.ws_api.connect(
            self.proxy_host,
            self.proxy_port,
        )
        self.event_engine.register(EVENT_TIMER, self.process_timer_event)

    def subscribe(self, req: SubscribeRequest) -> None:
        contract: ContractData | None = self.symbol_contract_map.get(req.symbol, None)
        if not contract:
            self.write_log(f"Failed to subscribe, symbol not found: {req.symbol}")
            return

        if req.vt_symbol in self.subscribed_symbols:
            return
        self.subscribed_symbols.add(req.vt_symbol)

        self.ws_api.subscribe(req)

    def send_order(self, req: OrderRequest) -> str:
        contract: ContractData | None = self.symbol_contract_map.get(req.symbol, None)
        if not contract:
            self.write_log(f"Failed to send order, symbol not found: {req.symbol}")
            return ""

        return self.rest_api.send_order(req, contract)

    def cancel_order(self, req: CancelRequest) -> None:
        contract: ContractData | None = self.symbol_contract_map.get(req.symbol, None)
        if not contract:
            self.write_log(f"Failed to cancel order, symbol not found: {req.symbol}")
            return

        self.rest_api.cancel_order(req, contract)

    def query_account(self) -> None:
        self.rest_api.query_account()

    def query_position(self) -> None:
        self.rest_api.query_position()

    def query_history(self, req: HistoryRequest) -> list[BarData]:
        contract: ContractData | None = self.symbol_contract_map.get(req.symbol, None)
        if not contract:
            self.write_log(f"Failed to query history, symbol not found: {req.symbol}")
            return []

        return self.rest_api.query_history(req, contract)

    def close(self) -> None:
        self.rest_api.stop()
        self.ws_api.stop()

    def on_order(self, order: OrderData) -> None:
        self.orders[order.orderid] = order
        super().on_order(order)

    def get_order(self, orderid: str) -> OrderData | None:
        return self.orders.get(orderid, None)

    def on_contract(self, contract: ContractData) -> None:
        self.symbol_contract_map[contract.symbol] = contract
        self.name_contract_map[contract.name] = contract
        super().on_contract(contract)

    def get_contract_by_symbol(self, symbol: str) -> ContractData | None:
        return self.symbol_contract_map.get(symbol, None)

    def get_contract_by_name(self, name: str) -> ContractData | None:
        return self.name_contract_map.get(name, None)

    def parse_order_data(self, data: dict, gateway_name: str) -> OrderData:
        """Parse HL order dict to OrderData."""
        name: str = data.get("coin", "")
        contract: ContractData | None = self.get_contract_by_name(name)
        if not contract:
            raise ValueError(f"Contract not found for coin: {name}")

        # Determine orderid: prefer cloid, fallback to oid
        cloid_str: str | None = data.get("cloid")
        oid: int | None = data.get("oid")

        orderid: str
        if cloid_str and cloid_str != "0x00000000000000000000000000000000":
            orderid = self.cloid_orderid_map.get(cloid_str, cloid_str)
            self.local_orderids.add(orderid)
        elif oid is not None:
            orderid = self.oid_orderid_map.get(oid, str(oid))
        else:
            orderid = ""

        side: str = data.get("side", "")
        direction: Direction = Direction.LONG if side == "B" else Direction.SHORT

        tif: str = data.get("tif", "Gtc")
        order_type: OrderType = ORDERTYPE_HL2VT.get(tif, OrderType.LIMIT)

        status_str: str = data.get("status", "")
        if status_str:
            status: Status = STATUS_HL2VT.get(status_str, Status.NOTTRADED)
        else:
            status = Status.NOTTRADED

        order: OrderData = OrderData(
            symbol=contract.symbol,
            exchange=Exchange.GLOBAL,
            type=order_type,
            orderid=orderid,
            direction=direction,
            offset=Offset.NONE,
            traded=float(data.get("filledTotalSz", "0") or "0"),
            price=float(data.get("limitPx", "0") or "0"),
            volume=float(data.get("sz", "0") or "0"),
            datetime=parse_timestamp(data.get("timestamp", 0)),
            status=status,
            gateway_name=gateway_name,
        )
        return order

    def generate_cloid(self) -> str:
        self._cloid_counter += 1
        unique_int: int = (int(time.time() * 1000) << 16) | (self._cloid_counter & 0xFFFF)
        return Cloid.from_int(unique_int).to_raw()

    def process_timer_event(self, event: Event) -> None:
        # Heartbeat watchdog: if WS is stalled (no packets) or disconnected, restart with backoff
        now: float = time.time()
        ws_last_recv: float = getattr(self.ws_api, "last_recv_ts", 0.0) or 0.0
        ws_disconnected: bool = bool(getattr(self.ws_api, "disconnected", False))

        need_restart: bool = False
        reason: str = ""
        if ws_disconnected:
            need_restart = True
            reason = "disconnected"
        elif ws_last_recv and (now - ws_last_recv) > self.ws_heartbeat_timeout:
            need_restart = True
            reason = f"no packets for {int(now - ws_last_recv)}s"

        if need_restart and now >= self._ws_next_restart_ts:
            ok = self.ws_api.restart()
            if ok:
                self.write_log(f"WebSocket restarted ({reason})")
                self._ws_restart_delay = self.ws_reconnect_min_delay
                self._ws_next_restart_ts = 0.0
            else:
                # Exponential backoff
                self._ws_next_restart_ts = now + self._ws_restart_delay
                self._ws_restart_delay = min(self._ws_restart_delay * 2, self.ws_reconnect_max_delay)
                self.write_log(
                    f"WebSocket restart deferred ({reason}), next try in {self._ws_restart_delay}s"
                )

        self.ping_count += 1
        if self.ping_count < self.ping_interval:
            return
        self.ping_count = 0
        self.ws_api.send_ping()


class RestApi(RestClient):
    """The REST API of HyperliquidGateway"""

    def __init__(self, gateway: HyperliquidGateway) -> None:
        super().__init__()

        self.gateway: HyperliquidGateway = gateway
        self.gateway_name: str = gateway.gateway_name

        self.wallet: LocalAccount | None = None

        self.reqid: int = 0
        self.connect_time: int = 0
        self.reqid_order_map: dict[int, OrderData] = {}

        self._pending_dex_count: int = 0
        self._dex_contracts_ready: int = 0

        # Account query aggregation across all dexs
        self._pending_account_dex_count: int = 0
        self._account_queries_ready: int = 0
        self._account_by_dex: dict[str, AccountData] = {}

        # Total equity aggregation (perp + spot)
        self._spot_prices_ready: bool = False
        self._spot_balances_ready: bool = False
        self._spot_token_to_pair: dict[str, str] = {}   # token symbol -> spot pair coin string
        self._spot_pair_mark_px: dict[str, float] = {}  # spot pair coin string -> markPx
        self._spot_balances: dict[str, float] = {}      # coin symbol -> total
        self._minimal_accounts_published: bool = False

    def connect(
        self,
        wallet: LocalAccount,
        proxy_host: str,
        proxy_port: int,
    ) -> None:
        self.wallet = wallet

        self.connect_time = int(datetime.now().strftime("%y%m%d%H%M%S"))

        self.init(REST_HOST, proxy_host, proxy_port)
        self.start()
        self.gateway.write_log("REST API started")

        self.query_contract()

    def on_error(
        self,
        exception_type: type,
        exception_value: Exception,
        tb,
        request: Request
    ) -> None:
        """General error callback for REST API."""
        detail: str = self.exception_detail(exception_type, exception_value, tb, request)
        detail = detail.replace("{", "{{").replace("}", "}}")
        msg: str = f"REST API exception: {detail}"
        self.gateway.write_log(msg)

    def on_failed(self, status_code: int, request: Request) -> None:
        """Callback for failed REST API requests."""
        msg: str = f"REST API request failed: {status_code} {request.method} {request.path}, data={request.data}"
        self.gateway.write_log(msg)

    def sign_action(self, action: dict, nonce: int) -> dict:
        """Sign action with wallet."""
        if not self.wallet:
            raise RuntimeError("Wallet not initialized")

        is_mainnet = REST_HOST == "https://api.hyperliquid.xyz"
        signature = sign_l1_action(
            self.wallet,
            action,
            None,  # vault_address
            nonce,
            None,  # expires_after
            is_mainnet,
        )

        return {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": None,
        }

    def new_orderid(self) -> str:
        """Generate a new local order ID."""
        self.reqid += 1
        prefix: str = datetime.now().strftime("%H%M%S")
        suffix: str = str(self.reqid).rjust(6, "0")
        orderid: str = f"{prefix}_{suffix}"
        return orderid

    def query_contract(self) -> None:
        self.add_request(
            method="POST",
            path="/info",
            data=json.dumps({"type": "perpDexs"}),
            headers={"Content-Type": "application/json"},
            callback=self.on_query_perp_dexs,
        )

    def query_account(self) -> None:
        if not self.wallet:
            return
        # Query account for each dex
        # - In minimal publish mode, we must query ALL perp dexs so PERPS account is complete.
        # - If any aggregation is enabled, query ALL perp dexs.
        target_dexs: list[str]
        need_all_dexs: bool = (
            self.gateway.account_publish_mode == "minimal"
            or self.gateway.aggregate_all_perp_dex_accounts
            or self.gateway.aggregate_total_equity
        )

        if need_all_dexs and self.gateway.all_perp_dexs:
            target_dexs = self.gateway.all_perp_dexs
        else:
            target_dexs = self.gateway.perp_dexs

        self._pending_account_dex_count = len(target_dexs)
        self._account_queries_ready = 0
        self._account_by_dex.clear()
        self._minimal_accounts_published = False

        for dex_name in target_dexs:
            self.add_request(
                method="POST",
                path="/info",
                data=json.dumps({"type": "clearinghouseState", "user": self.wallet.address, "dex": dex_name}),
                headers={"Content-Type": "application/json"},
                callback=self.on_query_account,
                extra=dex_name,
            )

        # In minimal publish mode OR total-equity mode, we need spot balances + mark prices
        if self.gateway.account_publish_mode == "minimal" or self.gateway.aggregate_total_equity:
            self._spot_prices_ready = False
            self._spot_balances_ready = False
            self._spot_token_to_pair.clear()
            self._spot_pair_mark_px.clear()
            self._spot_balances.clear()

            # 1) Spot balances (token totals)
            self.add_request(
                method="POST",
                path="/info",
                data=json.dumps({"type": "spotClearinghouseState", "user": self.wallet.address}),
                headers={"Content-Type": "application/json"},
                callback=self.on_query_spot_balances,
            )

            # 2) Spot mark prices (pair markPx)
            self.add_request(
                method="POST",
                path="/info",
                data=json.dumps({"type": "spotMetaAndAssetCtxs"}),
                headers={"Content-Type": "application/json"},
                callback=self.on_query_spot_meta_and_asset_ctxs,
            )

    def query_position(self) -> None:
        if not self.wallet:
            return
        # Query position for each dex
        for dex_name in self.gateway.perp_dexs:
            self.add_request(
                method="POST",
                path="/info",
                data=json.dumps({"type": "clearinghouseState", "user": self.wallet.address, "dex": dex_name}),
                headers={"Content-Type": "application/json"},
                callback=self.on_query_position,
                extra=dex_name,
            )

    def query_order(self) -> None:
        if not self.wallet:
            return
        # Query open orders for each dex
        for dex_name in self.gateway.perp_dexs:
            self.add_request(
                method="POST",
                path="/info",
                data=json.dumps({"type": "openOrders", "user": self.wallet.address, "dex": dex_name}),
                headers={"Content-Type": "application/json"},
                callback=self.on_query_order,
                extra=dex_name,
            )

    def send_order(self, req: OrderRequest, contract: ContractData) -> str:
        if not self.wallet:
            self.gateway.write_log("Wallet not initialized, cannot send order")
            return ""

        orderid: str = self.new_orderid()

        # Get asset id
        asset: int | None = self.gateway.name_to_asset.get(contract.name)
        if asset is None:
            self.gateway.write_log(f"Asset id not found for {contract.name}")
            return ""

        sz_decimals: int = self.gateway.asset_to_sz_decimals.get(asset, 0)

        # Round price
        # Spot assets are in range [10000, 110000), perp dex assets start at 110000
        is_spot = 10000 <= asset < 110000
        limit_px = round_hyperliquid_price(req.price, sz_decimals, is_spot)

        # Generate cloid for order tracking
        cloid_str: str = self.gateway.generate_cloid()
        self.gateway.orderid_cloid_map[orderid] = cloid_str
        self.gateway.cloid_orderid_map[cloid_str] = orderid

        # Build order request
        hl_order: HlOrderRequest = {
            "coin": contract.name,
            "is_buy": DIRECTION_VT2HL[req.direction],
            "sz": req.volume,
            "limit_px": limit_px,
            "order_type": ORDERTYPE_VT2HL.get(req.type, {"limit": {"tif": "Gtc"}}),
            "reduce_only": False,
            "cloid": Cloid.from_str(cloid_str),
        }

        order_wire: OrderWire = order_request_to_order_wire(hl_order, asset)
        order_action = order_wires_to_order_action([order_wire], None, "na")
        nonce = get_timestamp_ms()
        payload = self.sign_action(order_action, nonce)

        # Track order
        order: OrderData = OrderData(
            symbol=contract.symbol,
            exchange=Exchange.GLOBAL,
            type=req.type,
            orderid=orderid,
            direction=req.direction,
            offset=Offset.NONE,
            price=limit_px,
            volume=req.volume,
            datetime=datetime.now(CHINA_TZ),
            status=Status.SUBMITTING,
            gateway_name=self.gateway_name,
        )
        self.reqid_order_map[self.reqid] = order
        self.gateway.local_orderids.add(orderid)
        self.gateway.on_order(order)

        self.add_request(
            method="POST",
            path="/exchange",
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            callback=self.on_send_order,
            extra=orderid,
        )

        return order.vt_orderid

    def cancel_order(self, req: CancelRequest, contract: ContractData) -> None:
        if not self.wallet:
            return

        order: OrderData | None = self.gateway.get_order(req.orderid)
        if not order:
            self.gateway.write_log(f"Cancel failed, order not found: {req.orderid}")
            return

        cloid_str: str | None = self.gateway.orderid_cloid_map.get(req.orderid)
        if not cloid_str:
            self.gateway.write_log(f"Cancel failed, cloid not found for: {req.orderid}")
            return

        asset: int | None = self.gateway.name_to_asset.get(contract.name)
        if asset is None:
            self.gateway.write_log(f"Cancel failed, asset id not found for {contract.name}")
            return

        cancel_action = {
            "type": "cancelByCloid",
            "cancels": [
                {
                    "asset": asset,
                    "cloid": cloid_str,
                }
            ],
        }
        nonce = get_timestamp_ms()
        payload = self.sign_action(cancel_action, nonce)

        self.add_request(
            method="POST",
            path="/exchange",
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            callback=self.on_cancel_order,
            extra=req.orderid,
        )

        self.gateway.write_log(f"Cancel request sent: {req.orderid}, cloid: {cloid_str}")

    def query_history(self, req: HistoryRequest, contract: ContractData) -> list[BarData]:
        """Query historical kline data via candleSnapshot."""
        interval: str = INTERVAL_VT2HL.get(req.interval, "1h")

        if not req.end:
            req.end = datetime.now()

        start_time: int = int(req.start.timestamp() * 1000)
        end_time: int = int(req.end.timestamp() * 1000)

        buf: dict[datetime, BarData] = {}
        chunk_ms: int = 3 * 24 * 3600 * 1000  # ~3 days per chunk
        cur_end: int = end_time

        while cur_end > start_time:
            cur_start = max(cur_end - chunk_ms, start_time)

            resp = self.request(
                method="POST",
                path="/info",
                data=json.dumps({
                    "type": "candleSnapshot",
                    "req": {
                        "coin": contract.name,
                        "interval": interval,
                        "startTime": cur_start,
                        "endTime": cur_end,
                    }
                }),
                headers={"Content-Type": "application/json"},
            )

            if resp.status_code // 100 != 2:
                self.gateway.write_log(f"Query history failed: {resp.status_code} {resp.text}")
                break

            data = resp.json()
            if not isinstance(data, list) or not data:
                break

            for d in data:
                bar = BarData(
                    symbol=contract.symbol,
                    exchange=Exchange.GLOBAL,
                    datetime=parse_timestamp(d["t"]),
                    interval=req.interval,
                    volume=float(d.get("v", 0)),
                    open_price=float(d.get("o", 0)),
                    high_price=float(d.get("h", 0)),
                    low_price=float(d.get("l", 0)),
                    close_price=float(d.get("c", 0)),
                    gateway_name=self.gateway_name,
                )
                buf[bar.datetime] = bar

            # Move window backward
            cur_end = cur_start

        index: list[datetime] = list(buf.keys())
        index.sort()
        return [buf[i] for i in index]

    def on_query_perp_dexs(self, packet: dict, request: Request) -> None:
        """Callback of perpDexs query, then query metaAndAssetCtxs for each dex."""
        if not isinstance(packet, list):
            self.gateway.write_log("Invalid perpDexs response")
            return

        # Keep a copy of all dexs returned by API ("" stands for default dex)
        all_dexs: list[str] = []
        for dex_info in packet:
            if dex_info is None:
                all_dexs.append("")
            else:
                all_dexs.append(dex_info.get("name", "") or "")
        self.gateway.all_perp_dexs = all_dexs

        self.gateway.perp_dexs = []
        self.gateway.perp_dex_to_offset = {}

        # packet[0] is None for standard dex, then builder dexs
        for i, dex_info in enumerate(packet):
            if dex_info is None:
                dex_name = ""
            else:
                dex_name = dex_info.get("name", "")

            # Apply filtering (keep original index i for correct offset)
            if not self.gateway.is_dex_enabled(dex_name):
                continue

            if dex_name:
                # builder-deployed perp dexs start at 110000, offset by 10000 each
                self.gateway.perp_dex_to_offset[dex_name] = 110000 + (i - 1) * 10000
            else:
                self.gateway.perp_dex_to_offset[""] = 0

            self.gateway.perp_dexs.append(dex_name)

        # Safety: keep at least default dex if user filtered out everything
        if not self.gateway.perp_dexs:
            self.gateway.write_log("Perp dex filter excluded all dexs, fallback to default dex")
            self.gateway.perp_dexs = [""]
            self.gateway.perp_dex_to_offset = {"": 0}

        self.gateway.write_log(f"Found perp dexs: {self.gateway.perp_dexs}")

        # Query metaAndAssetCtxs for each dex
        for dex_name in self.gateway.perp_dexs:
            self.add_request(
                method="POST",
                path="/info",
                data=json.dumps({"type": "metaAndAssetCtxs", "dex": dex_name}),
                headers={"Content-Type": "application/json"},
                callback=self.on_query_contract,
                extra=dex_name,
            )

        # Track how many dexs we're waiting for
        self._pending_dex_count = len(self.gateway.perp_dexs)
        self._dex_contracts_ready = 0

    def on_query_contract(self, packet: dict, request: Request) -> None:
        """Callback of contract query for a specific dex."""
        if not isinstance(packet, list) or len(packet) < 2:
            self.gateway.write_log("Invalid contract response")
            self._dex_contracts_ready += 1
            self._check_all_contracts_ready()
            return

        dex_name: str = request.extra or ""
        meta = packet[0]
        asset_ctxs = packet[1]
        offset = self.gateway.perp_dex_to_offset.get(dex_name, 0)

        universe = meta.get("universe", [])
        self.gateway.write_log(f"Contract query for dex='{dex_name}', count: {len(universe)}, offset: {offset}")

        # Build mappings and create contract data
        for idx, asset_info in enumerate(universe):
            name = asset_info["name"]
            sz_decimals = asset_info["szDecimals"]
            max_leverage = asset_info.get("maxLeverage", 1)
            global_asset = idx + offset

            self.gateway.asset_to_sz_decimals[global_asset] = sz_decimals
            self.gateway.name_to_asset[name] = global_asset
            self.gateway.name_to_coin[name] = name
            self.gateway.asset_to_name[global_asset] = name
            self.gateway.name_to_dex[name] = dex_name

            pricetick = 10 ** -(6 - sz_decimals)
            min_volume = 10 ** -sz_decimals

            # Generate symbol: replace ':' with '_' and uppercase
            # Standard dex coins use {COIN}USDC_SWAP_HL to align with data-platform naming
            symbol_name = name.replace(":", "_").upper()
            if ":" not in name:
                symbol_name += "USDC"
            symbol: str = f"{symbol_name}_SWAP_HL"

            contract: ContractData = ContractData(
                symbol=symbol,
                exchange=Exchange.GLOBAL,
                name=name,
                pricetick=pricetick,
                size=1,
                min_volume=min_volume,
                product=Product.SWAP,
                net_position=True,
                history_data=True,
                gateway_name=self.gateway_name,
            )
            contract.extra = asset_info
            contract.extra["dex"] = dex_name
            contract.extra["asset"] = global_asset

            self.gateway.on_contract(contract)

        self._dex_contracts_ready += 1
        self._check_all_contracts_ready()

    def _check_all_contracts_ready(self) -> None:
        """Check if all dex contract queries are complete."""
        if self._dex_contracts_ready >= self._pending_dex_count:
            total_contracts = len(self.gateway.symbol_contract_map)
            self.gateway.write_log(f"All contract queries complete, total contracts: {total_contracts}")
            self.gateway.connect_ws_api()
            self.query_account()
            self.query_order()

    def on_query_account(self, packet: dict, request: Request) -> None:
        """Callback of account query."""
        if not isinstance(packet, dict):
            return

        dex_name: str = request.extra or ""
        margin_summary = packet.get("marginSummary") or {}
        withdrawable = packet.get("withdrawable", "0") or "0"

        accountid: str
        if self.gateway.account_publish_mode == "minimal":
            # Only keep XYZ as a standalone account in UI
            if (dex_name or "").strip().lower() == "xyz":
                accountid = self.gateway.minimal_xyz_accountid
            else:
                accountid = ""   # not published
        else:
            accountid = "USDC" if not dex_name else f"USDC_{dex_name.upper()}"

        account: AccountData = AccountData(
            accountid=accountid or "HIDDEN",
            balance=get_float_value(margin_summary, "accountValue"),
            gateway_name=self.gateway_name,
        )
        account.available = float(withdrawable)
        account.frozen = account.balance - account.available

        self.gateway.write_log(f"Account query [{dex_name or 'default'}]: balance={account.balance}, available={account.available}")
        if accountid:
            self.gateway.on_account(account)

        # Track and publish aggregated account if enabled
        if self.gateway.aggregate_all_perp_dex_accounts:
            self._account_by_dex[dex_name] = account
            self._account_queries_ready += 1

            if self._pending_account_dex_count > 0 and self._account_queries_ready >= self._pending_account_dex_count:
                total_balance: float = 0.0
                total_available: float = 0.0
                total_frozen: float = 0.0

                for a in self._account_by_dex.values():
                    total_balance += float(a.balance or 0)
                    total_available += float(getattr(a, "available", 0) or 0)
                    total_frozen += float(getattr(a, "frozen", 0) or 0)

                total_accountid: str = self.gateway.aggregate_accountid or "USDC_TOTAL"
                total: AccountData = AccountData(
                    accountid=total_accountid,
                    balance=total_balance,
                    gateway_name=self.gateway_name,
                )
                total.available = total_available
                # Prefer summing each dex frozen, fallback to balance-available
                total.frozen = total_frozen if total_frozen > 0 else (total_balance - total_available)

                self.gateway.write_log(
                    f"Account aggregate [{total_accountid}]: balance={total.balance}, available={total.available}"
                )
                self.gateway.on_account(total)

        # Always track progress for minimal mode / total equity publishing
        if self.gateway.account_publish_mode == "minimal" and dex_name not in self._account_by_dex:
            self._account_by_dex[dex_name] = account
            self._account_queries_ready += 1
        elif not self.gateway.aggregate_all_perp_dex_accounts:
            # In non-aggregation mode we still need to advance readiness for minimal/total equity checks
            self._account_by_dex[dex_name] = account
            self._account_queries_ready += 1

        # Try publishing minimal accounts / total equity when all required parts are ready
        if self.gateway.account_publish_mode == "minimal":
            self._try_publish_minimal_accounts()
        elif self.gateway.aggregate_total_equity:
            self._try_publish_total_equity()

    def on_query_spot_balances(self, packet: dict, request: Request) -> None:
        """Callback of spotClearinghouseState (spot token balances)."""
        if not isinstance(packet, dict):
            return

        balances = packet.get("balances", []) or []
        if not isinstance(balances, list):
            balances = []

        for b in balances:
            if not isinstance(b, dict):
                continue
            coin = b.get("coin", "") or ""
            total_str = b.get("total", "0") or "0"
            try:
                total = float(total_str)
            except Exception:
                total = 0.0
            if coin:
                self._spot_balances[coin] = total

        self._spot_balances_ready = True
        if self.gateway.account_publish_mode == "minimal":
            self._try_publish_minimal_accounts()
        else:
            self._try_publish_total_equity()

    def on_query_spot_meta_and_asset_ctxs(self, packet: dict, request: Request) -> None:
        """Callback of spotMetaAndAssetCtxs (spot metadata + mark prices)."""
        # Expected schema: [spotMeta, assetCtxs]
        if not isinstance(packet, list) or len(packet) < 2:
            self.gateway.write_log("Invalid spotMetaAndAssetCtxs response")
            self._spot_prices_ready = True
            self._try_publish_total_equity()
            return

        spot_meta = packet[0] or {}
        asset_ctxs = packet[1] or []

        universe = spot_meta.get("universe", []) if isinstance(spot_meta, dict) else []
        tokens = spot_meta.get("tokens", []) if isinstance(spot_meta, dict) else []

        # Build token name -> token index
        token_name_to_index: dict[str, int] = {}
        if isinstance(tokens, list):
            for t in tokens:
                if not isinstance(t, dict):
                    continue
                name = (t.get("name") or "").strip()
                idx = t.get("index")
                if name and isinstance(idx, int):
                    token_name_to_index[name] = idx

        # Build base token index -> pair coin string (quote must be USDC token 0)
        base_index_to_pair: dict[int, str] = {}
        if isinstance(universe, list):
            for u in universe:
                if not isinstance(u, dict):
                    continue
                pair_name = u.get("name", "") or ""
                pair_tokens = u.get("tokens", [])
                if not pair_name or not isinstance(pair_tokens, list) or len(pair_tokens) != 2:
                    continue
                base_token, quote_token = pair_tokens[0], pair_tokens[1]
                if isinstance(base_token, int) and quote_token == 0:
                    # Map base token index -> coin string used by APIs ("PURR/USDC" or "@{index}")
                    base_index_to_pair[base_token] = pair_name

        # Now map token symbol -> pair coin string
        self._spot_token_to_pair.clear()
        for sym, idx in token_name_to_index.items():
            pair = base_index_to_pair.get(idx)
            if pair:
                self._spot_token_to_pair[sym] = pair

        # Map pair coin string -> markPx (zip by universe order)
        self._spot_pair_mark_px.clear()
        if isinstance(universe, list) and isinstance(asset_ctxs, list):
            n = min(len(universe), len(asset_ctxs))
            for i in range(n):
                u = universe[i]
                c = asset_ctxs[i]
                if not isinstance(u, dict) or not isinstance(c, dict):
                    continue
                pair_name = u.get("name", "") or ""
                if not pair_name:
                    continue
                try:
                    mark_px = float(c.get("markPx", 0) or 0)
                except Exception:
                    mark_px = 0.0
                if mark_px:
                    self._spot_pair_mark_px[pair_name] = mark_px

        self._spot_prices_ready = True
        if self.gateway.account_publish_mode == "minimal":
            self._try_publish_minimal_accounts()
        else:
            self._try_publish_total_equity()

    def _try_publish_total_equity(self) -> None:
        """Publish a synthetic AccountData that approximates HL UI total equity (perp + spot MTM)."""
        # Need: all perp dex account queries completed + spot balances + spot prices
        if self._pending_account_dex_count <= 0:
            return
        if self._account_queries_ready < self._pending_account_dex_count:
            return
        if not (self._spot_balances_ready and self._spot_prices_ready):
            return

        # 1) Perp equity: sum of each dex marginSummary.accountValue (already includes unrealized PnL)
        perp_equity: float = 0.0
        for a in self._account_by_dex.values():
            perp_equity += float(a.balance or 0)

        # 2) Spot equity: sum of token totals marked to USDC using spot markPx
        spot_equity: float = 0.0
        for coin, total in self._spot_balances.items():
            if not coin:
                continue
            if coin.upper() == "USDC":
                spot_equity += float(total)
                continue
            pair = self._spot_token_to_pair.get(coin)
            if not pair:
                continue
            px = self._spot_pair_mark_px.get(pair, 0.0)
            if px:
                spot_equity += float(total) * float(px)

        total_equity: float = perp_equity + spot_equity

        accountid: str = self.gateway.aggregate_equity_accountid or "EQUITY_TOTAL"
        total: AccountData = AccountData(
            accountid=accountid,
            balance=total_equity,
            gateway_name=self.gateway_name,
        )
        # available/frozen are not well-defined for total equity across perps+spot; keep them equal to balance/0
        total.available = total_equity
        total.frozen = 0.0

        self.gateway.write_log(
            f"Equity aggregate [{accountid}]: total={total.balance} (perp={perp_equity}, spot≈{spot_equity})"
        )
        self.gateway.on_account(total)

    def _try_publish_minimal_accounts(self) -> None:
        """
        Publish only 3 accounts for UI:
        - XYZ: the standalone dex account (published in on_query_account)
        - PERPS: sum of perp equity across all perp dexs
        - SPOT: spot balances marked to USDC using spot markPx
        """
        if self._minimal_accounts_published:
            return
        if self._pending_account_dex_count <= 0:
            return
        if self._account_queries_ready < self._pending_account_dex_count:
            return
        if not (self._spot_balances_ready and self._spot_prices_ready):
            return

        # PERPS equity: sum of each dex marginSummary.accountValue (includes unrealized PnL)
        perp_equity: float = 0.0
        perp_available: float = 0.0
        perp_frozen: float = 0.0
        for a in self._account_by_dex.values():
            perp_equity += float(a.balance or 0)
            perp_available += float(getattr(a, "available", 0) or 0)
            perp_frozen += float(getattr(a, "frozen", 0) or 0)

        perps_id: str = self.gateway.minimal_perps_accountid or "PERPS"
        perps: AccountData = AccountData(
            accountid=perps_id,
            balance=perp_equity,
            gateway_name=self.gateway_name,
        )
        perps.available = perp_available
        perps.frozen = perp_frozen if perp_frozen > 0 else (perp_equity - perp_available)
        self.gateway.on_account(perps)

        # SPOT equity: sum of token totals marked to USDC using spot markPx
        spot_equity: float = 0.0
        for coin, total in self._spot_balances.items():
            if not coin:
                continue
            if coin.upper() == "USDC":
                spot_equity += float(total)
                continue
            pair = self._spot_token_to_pair.get(coin)
            if not pair:
                continue
            px = self._spot_pair_mark_px.get(pair, 0.0)
            if px:
                spot_equity += float(total) * float(px)

        spot_id: str = self.gateway.minimal_spot_accountid or "SPOT"
        spot: AccountData = AccountData(
            accountid=spot_id,
            balance=spot_equity,
            gateway_name=self.gateway_name,
        )
        # available/frozen not well-defined; keep simple
        spot.available = spot_equity
        spot.frozen = 0.0
        self.gateway.on_account(spot)

        self.gateway.write_log(
            f"Minimal accounts published: {self.gateway.minimal_xyz_accountid}, {perps_id}, {spot_id}"
        )
        self._minimal_accounts_published = True

    def on_query_position(self, packet: dict, request: Request) -> None:
        """Callback of position query."""
        if not isinstance(packet, dict):
            return

        asset_positions = packet.get("assetPositions") or []
        for ap in asset_positions:
            pos = ap.get("position", {})
            name = pos.get("coin", "")
            contract: ContractData | None = self.gateway.get_contract_by_name(name)
            if not contract:
                continue

            szi = float(pos.get("szi", "0"))
            if szi == 0:
                continue

            direction = Direction.LONG if szi > 0 else Direction.SHORT
            volume = abs(szi)

            position: PositionData = PositionData(
                symbol=contract.symbol,
                exchange=Exchange.GLOBAL,
                direction=direction,
                volume=volume,
                price=get_float_value(pos, "entryPx"),
                pnl=get_float_value(pos, "unrealizedPnl"),
                gateway_name=self.gateway_name,
            )
            self.gateway.on_position(position)

    def on_query_order(self, packet: dict, request: Request) -> None:
        """Callback of open orders query."""
        if not isinstance(packet, list):
            return

        for order_info in packet:
            try:
                order: OrderData = self.gateway.parse_order_data(
                    order_info,
                    self.gateway_name
                )
                self.gateway.on_order(order)
            except Exception as e:
                self.gateway.write_log(f"Failed to parse order data: {e}")

        self.gateway.write_log(f"Active order data received, total: {len(packet)}")

    def on_send_order(self, packet: dict, request: Request) -> None:
        """Callback of send order."""
        orderid: str = request.extra
        order: OrderData | None = self.gateway.get_order(orderid)
        if not order:
            return

        status = packet.get("status")
        if status == "ok":
            response = packet.get("response", {})
            data = response.get("data", {})
            statuses = data.get("statuses", [])
            if statuses:
                first = statuses[0]
                if "resting" in first:
                    oid = first["resting"].get("oid")
                    if oid is not None:
                        self.gateway.oid_orderid_map[oid] = orderid
                    order.status = Status.NOTTRADED
                    self.gateway.write_log(f"Order resting: {orderid}, oid: {oid}")
                elif "filled" in first:
                    oid = first["filled"].get("oid")
                    if oid is not None:
                        self.gateway.oid_orderid_map[oid] = orderid
                    order.status = Status.ALLTRADED
                    self.gateway.write_log(f"Order filled immediately: {orderid}, oid: {oid}")
                elif "error" in first:
                    order.status = Status.REJECTED
                    msg = first["error"]
                    self.gateway.write_log(f"Order rejected: {orderid}, {msg}")
                else:
                    order.status = Status.NOTTRADED
                    self.gateway.write_log(f"Order submitted: {orderid}")
            else:
                order.status = Status.NOTTRADED
                self.gateway.write_log(f"Order submitted: {orderid}")
        else:
            order.status = Status.REJECTED
            msg = packet.get("response", "Unknown error")
            self.gateway.write_log(f"Order rejected: {orderid}, {msg}")

        self.gateway.on_order(order)

    def on_cancel_order(self, packet: dict, request: Request) -> None:
        """Callback of cancel order."""
        orderid: str = request.extra
        order: OrderData | None = self.gateway.get_order(orderid)
        if not order:
            return

        status = packet.get("status")
        if status == "ok":
            response = packet.get("response", {})
            data = response.get("data", {})
            statuses = data.get("statuses", [])
            if statuses and "error" in statuses[0]:
                msg = statuses[0]["error"]
                self.gateway.write_log(f"Cancel failed: {orderid}, {msg}")
            else:
                self.gateway.write_log(f"Cancel accepted: {orderid}")
        else:
            msg = packet.get("response", "Unknown error")
            self.gateway.write_log(f"Cancel failed: {orderid}, {msg}")


class WsApi(WebsocketClient):
    """The WebSocket API of HyperliquidGateway"""

    def __init__(self, gateway: HyperliquidGateway) -> None:
        super().__init__()

        self.gateway: HyperliquidGateway = gateway
        self.gateway_name: str = gateway.gateway_name

        self.ticks: dict[str, TickData] = {}
        self.subscribed: dict[str, SubscribeRequest] = {}

        # Tick push throttling (per symbol)
        self._last_tick_push_ms: dict[str, int] = {}

        # WS health state
        self.last_recv_ts: float = time.time()
        self.disconnected: bool = False

    def restart(self) -> bool:
        """
        Restart websocket thread (best-effort).
        Return True if a new thread is started, otherwise False.
        """
        if not self.host:
            self.gateway.write_log("WebSocket restart skipped: host not initialized")
            return False

        # If previous thread still alive, try to close then join briefly
        try:
            if self.thread and self.thread.is_alive():
                self.stop()
                self.thread.join(timeout=5)
                if self.thread.is_alive():
                    self.gateway.write_log("WebSocket restart skipped: previous thread still alive")
                    return False
        except Exception as e:
            self.gateway.write_log(f"WebSocket restart join error: {e}")

        # Start a fresh thread
        try:
            self.disconnected = False
            self.start()
            return True
        except Exception as e:
            self.gateway.write_log(f"WebSocket restart failed: {e}")
            return False

    def connect(
        self,
        proxy_host: str,
        proxy_port: int,
    ) -> None:
        self.init(WS_HOST, proxy_host, proxy_port)
        self.start()

    def subscribe(self, req: SubscribeRequest) -> None:
        contract: ContractData = cast(ContractData, self.gateway.get_contract_by_symbol(req.symbol))

        tick: TickData = TickData(
            symbol=req.symbol,
            exchange=req.exchange,
            name=contract.name,
            datetime=datetime.now(CHINA_TZ),
            gateway_name=self.gateway_name,
        )
        self.ticks[req.symbol] = tick
        self.subscribed[req.symbol] = req

        # Subscribe market data channels
        if self.gateway.subscribe_l2book:
            self.send_packet({
                "method": "subscribe",
                "subscription": {"type": "l2Book", "coin": contract.name},
            })
        if self.gateway.subscribe_trades:
            self.send_packet({
                "method": "subscribe",
                "subscription": {"type": "trades", "coin": contract.name},
            })
        if self.gateway.subscribe_active_asset_ctx:
            self.send_packet({
                "method": "subscribe",
                "subscription": {"type": "activeAssetCtx", "coin": contract.name},
            })

    def _push_tick(self, symbol: str) -> None:
        """
        Consolidate and throttle tick pushing (align with okx-style behavior).
        Websocket channels can be very frequent; pushing every packet will flood EventEngine.
        """
        tick: TickData | None = self.ticks.get(symbol)
        if not tick:
            return

        interval_ms: int = int(getattr(self.gateway, "tick_push_interval_ms", 200) or 0)
        if interval_ms > 0:
            now_ms: int = int(time.time() * 1000)
            last_ms: int = self._last_tick_push_ms.get(symbol, 0)
            if now_ms - last_ms < interval_ms:
                return
            self._last_tick_push_ms[symbol] = now_ms

        tick.datetime = datetime.now(CHINA_TZ)
        self.gateway.on_tick(copy(tick))

    def on_connected(self) -> None:
        self.gateway.write_log("WebSocket API connected")
        self.disconnected = False
        self.last_recv_ts = time.time()

        # Subscribe to user events
        if self.gateway.wallet:
            self.send_packet({
                "method": "subscribe",
                "subscription": {"type": "userEvents", "user": self.gateway.wallet.address},
            })
            self.send_packet({
                "method": "subscribe",
                "subscription": {"type": "orderUpdates", "user": self.gateway.wallet.address},
            })
            self.send_packet({
                "method": "subscribe",
                "subscription": {"type": "userFills", "user": self.gateway.wallet.address},
            })

        # Resubscribe market data
        for req in list(self.subscribed.values()):
            self.subscribe(req)

        # Re-query account and position on reconnect
        self.gateway.rest_api.query_account()
        self.gateway.rest_api.query_position()

    def on_disconnected(self, status_code: int, msg: str) -> None:
        self.disconnected = True
        self.gateway.write_log(f"WebSocket API disconnected: {status_code} {msg}")

    def on_packet(self, packet: dict) -> None:
        channel = packet.get("channel", "")

        if channel == "pong":
            self.last_recv_ts = time.time()
            return

        # Any non-pong packet counts as received data for heartbeat
        self.last_recv_ts = time.time()

        if channel == "l2Book":
            self.on_l2book(packet)
        elif channel == "trades":
            self.on_trades(packet)
        elif channel == "activeAssetCtx":
            self.on_active_asset_ctx(packet)
        elif channel == "userEvents":
            self.on_user_events(packet)
        elif channel == "orderUpdates":
            self.on_order_updates(packet)
        elif channel == "userFills":
            self.on_user_fills(packet)
        elif channel == "subscriptionResponse":
            self.on_subscription_response(packet)
        elif channel == "error":
            self.on_api_error(packet)
        else:
            # Unhandled channel
            pass

    def on_subscription_response(self, packet: dict) -> None:
        """Callback of subscription response."""
        data = packet.get("data", {})
        method = data.get("method", "")
        subscription = data.get("subscription", {})
        if method == "subscribe":
            self.gateway.write_log(f"Subscribed: {subscription}")
        elif method == "unsubscribe":
            self.gateway.write_log(f"Unsubscribed: {subscription}")

    def on_api_error(self, packet: dict) -> None:
        """Callback of API error."""
        msg = packet.get("data", "Unknown WS API error")
        self.gateway.write_log(f"WebSocket API error: {msg}")

    def on_l2book(self, packet: dict) -> None:
        data = packet.get("data", {})
        coin = data.get("coin", "")
        contract: ContractData | None = self.gateway.get_contract_by_name(coin)
        if not contract:
            return

        tick: TickData = self.ticks.get(contract.symbol)
        if not tick:
            return

        levels = data.get("levels", [[], []])
        bids = levels[0] if len(levels) > 0 else []
        asks = levels[1] if len(levels) > 1 else []

        for n in range(min(5, len(bids))):
            price = float(bids[n].get("px", 0))
            size = float(bids[n].get("sz", 0))
            tick.__setattr__(f"bid_price_{n + 1}", price)
            tick.__setattr__(f"bid_volume_{n + 1}", size)

        for n in range(min(5, len(asks))):
            price = float(asks[n].get("px", 0))
            size = float(asks[n].get("sz", 0))
            tick.__setattr__(f"ask_price_{n + 1}", price)
            tick.__setattr__(f"ask_volume_{n + 1}", size)

        self._push_tick(contract.symbol)

    def on_trades(self, packet: dict) -> None:
        data = packet.get("data", [])
        if not data:
            return

        trade = data[0]
        coin = trade.get("coin", "")
        contract: ContractData | None = self.gateway.get_contract_by_name(coin)
        if not contract:
            return

        tick: TickData = self.ticks.get(contract.symbol)
        if not tick:
            return

        tick.last_price = float(trade.get("px", 0))
        tick.last_volume = float(trade.get("sz", 0))
        self._push_tick(contract.symbol)

    def on_active_asset_ctx(self, packet: dict) -> None:
        data = packet.get("data", {})
        coin = data.get("coin", "")
        contract: ContractData | None = self.gateway.get_contract_by_name(coin)
        if not contract:
            return

        tick: TickData = self.ticks.get(contract.symbol)
        if not tick:
            return

        ctx = data.get("ctx", {})
        tick.last_price = get_float_value(ctx, "markPx")
        tick.open_price = get_float_value(ctx, "prevDayPx")
        tick.volume = get_float_value(ctx, "dayBaseVlm")
        tick.turnover = get_float_value(ctx, "dayNtlVlm")
        self._push_tick(contract.symbol)

    def on_user_events(self, packet: dict) -> None:
        data = packet.get("data", {})
        if not isinstance(data, dict):
            return

        # userEvents contains fills, funding, etc.
        fills = data.get("fills", [])
        for fill in fills:
            self.process_fill(fill)

        # Also process order updates if nested
        order_updates = data.get("orderUpdates", [])
        for ou in order_updates:
            self.process_order_update(ou)

        # Process position/account updates
        clearinghouse = data.get("clearinghouseState", {})
        if clearinghouse:
            self.process_clearinghouse_state(clearinghouse)

    def on_order_updates(self, packet: dict) -> None:
        data = packet.get("data", [])
        if not isinstance(data, list):
            return

        for order_update in data:
            self.process_order_update(order_update)

    def on_user_fills(self, packet: dict) -> None:
        data = packet.get("data", {})
        if not isinstance(data, dict):
            return

        fills = data.get("fills", [])
        for fill in fills:
            self.process_fill(fill)

    def process_clearinghouse_state(self, data: dict) -> None:
        """Process clearinghouse state update from userEvents."""
        margin_summary = data.get("marginSummary") or {}
        withdrawable = data.get("withdrawable", "0") or "0"

        account: AccountData = AccountData(
            accountid="USDC",
            balance=get_float_value(margin_summary, "accountValue"),
            gateway_name=self.gateway_name,
        )
        account.available = float(withdrawable)
        account.frozen = account.balance - account.available
        self.gateway.on_account(account)

        # Process positions
        asset_positions = data.get("assetPositions") or []
        for ap in asset_positions:
            pos = ap.get("position", {})
            name = pos.get("coin", "")
            contract: ContractData | None = self.gateway.get_contract_by_name(name)
            if not contract:
                continue

            szi = float(pos.get("szi", "0"))
            if szi == 0:
                continue

            direction = Direction.LONG if szi > 0 else Direction.SHORT
            volume = abs(szi)

            position: PositionData = PositionData(
                symbol=contract.symbol,
                exchange=Exchange.GLOBAL,
                direction=direction,
                volume=volume,
                price=get_float_value(pos, "entryPx"),
                pnl=get_float_value(pos, "unrealizedPnl"),
                gateway_name=self.gateway_name,
            )
            self.gateway.on_position(position)

    def process_order_update(self, data: dict) -> None:
        """Process order update from WebSocket."""
        order_info = data.get("order", {})
        coin = order_info.get("coin", "")
        contract: ContractData | None = self.gateway.get_contract_by_name(coin)
        if not contract:
            return

        cloid_str = order_info.get("cloid")
        oid = order_info.get("oid")

        # Match by cloid first, then by oid
        orderid: str | None = None
        if cloid_str:
            orderid = self.gateway.cloid_orderid_map.get(cloid_str)
        if not orderid and oid is not None:
            orderid = self.gateway.oid_orderid_map.get(oid)

        if not orderid:
            return

        order: OrderData | None = self.gateway.get_order(orderid)
        if not order:
            return

        # Update price/volume if available
        limit_px = order_info.get("limitPx")
        if limit_px not in (None, ""):
            order.price = float(limit_px)
        sz = order_info.get("sz")
        if sz not in (None, ""):
            order.volume = float(sz)

        # Determine status
        hl_status = data.get("status", "")
        filled_total_sz = float(order_info.get("filledTotalSz", "0") or "0")
        orig_sz = float(order_info.get("origSz", "0") or "0")

        if hl_status == "open":
            if filled_total_sz > 0 and filled_total_sz < orig_sz:
                order.status = Status.PARTTRADED
                order.traded = filled_total_sz
            else:
                order.status = Status.NOTTRADED
        elif hl_status == "filled":
            order.status = Status.ALLTRADED
            order.traded = orig_sz if orig_sz > 0 else order.volume
        elif hl_status in ("canceled", "marginCanceled"):
            if filled_total_sz > 0:
                order.status = Status.PARTTRADED
                order.traded = filled_total_sz
            else:
                order.status = Status.CANCELLED
        elif hl_status == "rejected":
            order.status = Status.REJECTED

        self.gateway.on_order(order)

    def process_fill(self, data: dict) -> None:
        """Process trade fill."""
        coin = data.get("coin", "")
        contract: ContractData | None = self.gateway.get_contract_by_name(coin)
        if not contract:
            return

        oid = data.get("oid")
        cloid_str = data.get("cloid")

        # Match by cloid first, then by oid
        orderid: str | None = None
        if cloid_str:
            orderid = self.gateway.cloid_orderid_map.get(cloid_str)
        if not orderid and oid is not None:
            orderid = self.gateway.oid_orderid_map.get(oid)

        if not orderid:
            return

        order: OrderData | None = self.gateway.get_order(orderid)
        if not order:
            return

        # Parse fill data
        fill_px = float(data.get("px", "0") or "0")
        fill_sz = float(data.get("sz", "0") or "0")
        side = data.get("side", "")
        direction = Direction.SHORT if side == "A" else Direction.LONG
        fill_time = data.get("time", 0)
        fee = float(data.get("fee", "0") or "0")
        tid = data.get("tid", 0)
        if tid and tid in self.gateway.filled_tids:
            return
        if tid:
            self.gateway.filled_tids.add(tid)

        trade: TradeData = TradeData(
            symbol=contract.symbol,
            exchange=Exchange.GLOBAL,
            orderid=orderid,
            tradeid=str(tid),
            direction=direction,
            price=fill_px,
            volume=fill_sz,
            datetime=parse_timestamp(fill_time) if fill_time else datetime.now(CHINA_TZ),
            gateway_name=self.gateway_name,
        )

        # Update order traded volume
        order.traded = order.traded + fill_sz
        if order.traded >= order.volume:
            order.status = Status.ALLTRADED
        else:
            order.status = Status.PARTTRADED

        self.gateway.on_trade(trade)
        self.gateway.on_order(order)

        self.gateway.write_log(
            f"Fill: {orderid} {contract.symbol} {direction.value} {fill_sz} @ {fill_px}, fee: {fee}"
        )

    def send_ping(self) -> None:
        if not self.active or not self.wsapp:
            return
        try:
            self.send_packet({"method": "ping"})
        except Exception:
            pass
