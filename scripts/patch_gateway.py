import re

path = "/root/.openclaw/workspace/cta_developer/vnpy_hyperliquid/hyperliquid_gateway.py"
with open(path, "r") as f:
    original = f.read()

# ============================================================
# PATCH 1: Add Cloid to imports
# ============================================================
old_import = """from hyperliquid.utils.signing import (
    get_timestamp_ms,
    order_request_to_order_wire,
    order_wires_to_order_action,
    sign_l1_action,
    OrderRequest as HlOrderRequest,
    OrderType as HlOrderType,
    OrderWire,
)"""

new_import = """from hyperliquid.utils.signing import (
    get_timestamp_ms,
    order_request_to_order_wire,
    order_wires_to_order_action,
    sign_l1_action,
    OrderRequest as HlOrderRequest,
    OrderType as HlOrderType,
    OrderWire,
)
from hyperliquid.utils.types import Cloid"""

assert old_import in original, "Import block not found"
original = original.replace(old_import, new_import)

# ============================================================
# PATCH 2: Gateway __init__ add mapping tables
# ============================================================
old_init = """        self.orders: dict[str, OrderData] = {}
        self.local_orderids: set[str] = set()

        self.symbol_contract_map: dict[str, ContractData] = {}
        self.name_contract_map: dict[str, ContractData] = {}"""

new_init = """        self.orders: dict[str, OrderData] = {}
        self.local_orderids: set[str] = set()

        self.symbol_contract_map: dict[str, ContractData] = {}
        self.name_contract_map: dict[str, ContractData] = {}

        # Order tracking: local_orderid <-> exchange oid/cloid
        self.orderid_cloid_map: dict[str, str] = {}
        self.cloid_orderid_map: dict[str, str] = {}
        self.oid_orderid_map: dict[int, str] = {}
        self._cloid_counter: int = 0"""

assert old_init in original, "Init block not found"
original = original.replace(old_init, new_init)

# ============================================================
# PATCH 3: Add generate_cloid method to Gateway
# ============================================================
old_method = """    def get_contract_by_name(self, name: str) -> ContractData | None:
        return self.name_contract_map.get(name, None)

    def process_timer_event"""

new_method = """    def get_contract_by_name(self, name: str) -> ContractData | None:
        return self.name_contract_map.get(name, None)

    def generate_cloid(self) -> str:
        self._cloid_counter += 1
        unique_int: int = (int(time.time() * 1000) << 16) | (self._cloid_counter & 0xFFFF)
        return Cloid.from_int(unique_int).to_raw()

    def process_timer_event"""

assert old_method in original, "get_contract_by_name block not found"
original = original.replace(old_method, new_method)

# ============================================================
# PATCH 4: RestApi.send_order - add cloid and fix OrderData
# ============================================================
old_send_order = """    def send_order(self, req: OrderRequest, contract: ContractData) -> str:
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
        is_spot = asset >= 10000
        limit_px = round_hyperliquid_price(req.price, sz_decimals, is_spot)

        # Build order request
        hl_order: HlOrderRequest = {
            "coin": contract.name,
            "is_buy": DIRECTION_VT2HL[req.direction],
            "sz": req.volume,
            "limit_px": limit_px,
            "order_type": ORDERTYPE_VT2HL.get(req.type, {"limit": {"tif": "Gtc"}}),
            "reduce_only": False,
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

        return orderid"""

new_send_order = """    def send_order(self, req: OrderRequest, contract: ContractData) -> str:
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
        is_spot = asset >= 10000
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

        return orderid"""

assert old_send_order in original, "send_order block not found"
original = original.replace(old_send_order, new_send_order)

# ============================================================
# PATCH 5: RestApi.cancel_order - full implementation
# ============================================================
old_cancel = """    def cancel_order(self, req: CancelRequest, contract: ContractData) -> None:
        if not self.wallet:
            return

        order: OrderData | None = self.gateway.get_order(req.orderid)
        if not order:
            self.gateway.write_log(f"Cancel failed, order not found: {req.orderid}")
            return

        # Try to cancel by cloid if available, otherwise need oid from exchange
        # For now we cancel by oid, but we need the exchange oid.
        # In HL, local order id is cloid. We need to track oid returned by exchange.
        # Simplified: cancel by coin + oid. We need to maintain local orderid -> oid mapping.
        # TODO: implement proper oid tracking
        self.gateway.write_log(f"Cancel not fully implemented yet for {req.orderid}")"""

new_cancel = """    def cancel_order(self, req: CancelRequest, contract: ContractData) -> None:
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

        self.gateway.write_log(f"Cancel request sent: {req.orderid}, cloid: {cloid_str}")"""

assert old_cancel in original, "cancel_order block not found"
original = original.replace(old_cancel, new_cancel)

# ============================================================
# PATCH 6: Replace on_send_order and add on_cancel_order
# ============================================================
old_on_send = """    def on_send_order(self, packet: dict, request: Request) -> None:
        \"\"\"Callback of send order.\"\"\"
        orderid: str = request.extra
        order: OrderData | None = self.gateway.get_order(orderid)
        if not order:
            return

        status = packet.get("status")
        if status == "ok":
            # Order accepted
            order.status = Status.NOTTRADED
            self.gateway.write_log(f"Order submitted: {orderid}")
        else:
            # Error
            order.status = Status.REJECTED
            msg = packet.get("response", "Unknown error")
            self.gateway.write_log(f"Order rejected: {orderid}, {msg}")

        self.gateway.on_order(order)


class WsApi(WebsocketClient):"""

new_on_send = """    def on_send_order(self, packet: dict, request: Request) -> None:
        \"\"\"Callback of send order.\"\"\"
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
        \"\"\"Callback of cancel order.\"\"\"
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


class WsApi(WebsocketClient):"""

assert old_on_send in original, "on_send_order block not found"
original = original.replace(old_on_send, new_on_send)

# ============================================================
# PATCH 7: WsApi.process_order_update - full implementation
# ============================================================
old_process_order = """    def process_order_update(self, data: dict) -> None:
        \"\"\"Process order update from WebSocket.\"\"\"
        coin = data.get("coin", "")
        contract: ContractData | None = self.gateway.get_contract_by_name(coin)
        if not contract:
            return

        # HL order updates may not include cloid directly in some formats
        # We try to match by status and oid
        # TODO: maintain oid -> local_orderid mapping for robust matching
        pass"""

new_process_order = """    def process_order_update(self, data: dict) -> None:
        \"\"\"Process order update from WebSocket.\"\"\"
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

        self.gateway.on_order(order)"""

assert old_process_order in original, "process_order_update block not found"
original = original.replace(old_process_order, new_process_order)

# ============================================================
# PATCH 8: WsApi.process_fill - full implementation
# ============================================================
old_process_fill = """    def process_fill(self, data: dict) -> None:
        \"\"\"Process trade fill.\"\"\"
        coin = data.get("coin", "")
        contract: ContractData | None = self.gateway.get_contract_by_name(coin)
        if not contract:
            return

        # Find matching order
        # TODO: proper order matching using cloid/oid
        pass"""

new_process_fill = """    def process_fill(self, data: dict) -> None:
        \"\"\"Process trade fill.\"\"\"
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
        )"""

assert old_process_fill in original, "process_fill block not found"
original = original.replace(old_process_fill, new_process_fill)

# ============================================================
# Write back
# ============================================================
with open(path, "w") as f:
    f.write(original)

print("All patches applied successfully!")
print("New file length:", len(original))

# Syntax check
import py_compile
try:
    py_compile.compile(path, doraise=True)
    print("Syntax check: PASSED")
except py_compile.PyCompileError as e:
    print("Syntax check: FAILED", e)
