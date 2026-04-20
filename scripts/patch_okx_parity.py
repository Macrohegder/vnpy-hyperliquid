import re

path = "/root/.openclaw/workspace/cta_developer/vnpy_hyperliquid/hyperliquid_gateway.py"
with open(path, "r") as f:
    original = f.read()

# ============================================================
# PATCH 1: Symbol naming conflict - modify on_query_contract
# ============================================================
old_contract = """        # Create contract data
        for idx, asset_info in enumerate(meta.get("universe", [])):
            name = asset_info["name"]
            sz_decimals = asset_info["szDecimals"]
            max_leverage = asset_info.get("maxLeverage", 1)

            pricetick = 10 ** -(6 - sz_decimals)
            min_volume = 10 ** -sz_decimals

            contract: ContractData = ContractData(
                symbol=name,
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

            self.gateway.on_contract(contract)

        self.gateway.write_log(f"Contract query complete, total: {len(meta.get('universe', []))}")

        # After contract query, connect WS and query account
        self.gateway.connect_ws_api()
        self.query_account()"""

new_contract = """        # Create contract data
        for idx, asset_info in enumerate(meta.get("universe", [])):
            name = asset_info["name"]
            sz_decimals = asset_info["szDecimals"]
            max_leverage = asset_info.get("maxLeverage", 1)

            pricetick = 10 ** -(6 - sz_decimals)
            min_volume = 10 ** -sz_decimals

            symbol: str = f"{name}_SWAP_HL"

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

            self.gateway.on_contract(contract)

        self.gateway.write_log(f"Contract query complete, total: {len(meta.get('universe', []))}")

        # After contract query, connect WS, query account and active orders
        self.gateway.connect_ws_api()
        self.query_account()
        self.query_order()"""

assert old_contract in original, "on_query_contract block not found"
original = original.replace(old_contract, new_contract)

# ============================================================
# PATCH 2: Add query_order / on_query_order to RestApi
# ============================================================
old_query_position = """    def query_position(self) -> None:
        if not self.wallet:
            return
        self.add_request(
            method="POST",
            path="/info",
            data=json.dumps({"type": "clearinghouseState", "user": self.wallet.address}),
            headers={"Content-Type": "application/json"},
            callback=self.on_query_position,
        )"""

new_query_position = """    def query_position(self) -> None:
        if not self.wallet:
            return
        self.add_request(
            method="POST",
            path="/info",
            data=json.dumps({"type": "clearinghouseState", "user": self.wallet.address}),
            headers={"Content-Type": "application/json"},
            callback=self.on_query_position,
        )

    def query_order(self) -> None:
        if not self.wallet:
            return
        self.add_request(
            method="POST",
            path="/info",
            data=json.dumps({"type": "openOrders", "user": self.wallet.address}),
            headers={"Content-Type": "application/json"},
            callback=self.on_query_order,
        )"""

assert old_query_position in original, "query_position block not found"
original = original.replace(old_query_position, new_query_position)

# ============================================================
# PATCH 3: Add on_query_order callback to RestApi
# ============================================================
old_on_query_position = """    def on_query_position(self, packet: dict, request: Request) -> None:
        \"\"\"Callback of position query.\"\"\"
        if not isinstance(packet, dict):
            return

        asset_positions = packet.get("assetPositions", [])
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

    def on_send_order"""

new_on_query_position = """    def on_query_position(self, packet: dict, request: Request) -> None:
        \"\"\"Callback of position query.\"\"\"
        if not isinstance(packet, dict):
            return

        asset_positions = packet.get("assetPositions", [])
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
        \"\"\"Callback of open orders query.\"\"\"
        if not isinstance(packet, list):
            return

        for order_info in packet:
            order: OrderData = self.gateway.parse_order_data(
                order_info,
                self.gateway_name
            )
            self.gateway.on_order(order)

        self.gateway.write_log(f"Active order data received, total: {len(packet)}")

    def on_send_order"""

assert old_on_query_position in original, "on_query_position block not found"
original = original.replace(old_on_query_position, new_on_query_position)

# ============================================================
# PATCH 4: Add on_error to RestApi
# ============================================================
old_rest_init = """    def connect(
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

        self.query_contract()"""

new_rest_init = """    def connect(
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
        \"\"\"General error callback for REST API.\"\"\"
        detail: str = self.exception_detail(exception_type, exception_value, tb, request)
        detail = detail.replace("{", "{{").replace("}", "}}")
        msg: str = f"REST API exception: {detail}"
        self.gateway.write_log(msg)"""

assert old_rest_init in original, "RestApi.connect block not found"
original = original.replace(old_rest_init, new_rest_init)

# ============================================================
# PATCH 5: Add parse_order_data to Gateway
# ============================================================
old_get_contract = """    def get_contract_by_symbol(self, symbol: str) -> ContractData | None:
        return self.symbol_contract_map.get(symbol, None)

    def get_contract_by_name(self, name: str) -> ContractData | None:
        return self.name_contract_map.get(name, None)

    def generate_cloid"""

new_get_contract = """    def get_contract_by_symbol(self, symbol: str) -> ContractData | None:
        return self.symbol_contract_map.get(symbol, None)

    def get_contract_by_name(self, name: str) -> ContractData | None:
        return self.name_contract_map.get(name, None)

    def parse_order_data(self, data: dict, gateway_name: str) -> OrderData:
        \"\"\"Parse HL order dict to OrderData.\"\"\"
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

    def generate_cloid"""

assert old_get_contract in original, "get_contract block not found"
original = original.replace(old_get_contract, new_get_contract)

# ============================================================
# PATCH 6: WsApi.on_connected - re-query on reconnect
# ============================================================
old_on_connected = """    def on_connected(self) -> None:
        self.gateway.write_log("WebSocket API connected")

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

        # Resubscribe market data
        for req in list(self.subscribed.values()):
            self.subscribe(req)"""

new_on_connected = """    def on_connected(self) -> None:
        self.gateway.write_log("WebSocket API connected")

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
        self.gateway.rest_api.query_position()"""

assert old_on_connected in original, "WsApi.on_connected block not found"
original = original.replace(old_on_connected, new_on_connected)

# ============================================================
# PATCH 7: WsApi.on_packet - add api error handling
# ============================================================
old_on_packet = """    def on_packet(self, packet: dict) -> None:
        channel = packet.get("channel", "")

        if channel == "pong":
            return
        elif channel == "l2Book":
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
        else:
            # Unhandled channel
            pass"""

new_on_packet = """    def on_packet(self, packet: dict) -> None:
        channel = packet.get("channel", "")

        if channel == "pong":
            return
        elif channel == "l2Book":
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
        \"\"\"Callback of subscription response.\"\"\"
        data = packet.get("data", {})
        method = data.get("method", "")
        subscription = data.get("subscription", {})
        if method == "subscribe":
            self.gateway.write_log(f"Subscribed: {subscription}")
        elif method == "unsubscribe":
            self.gateway.write_log(f"Unsubscribed: {subscription}")

    def on_api_error(self, packet: dict) -> None:
        \"\"\"Callback of API error.\"\"\"
        msg = packet.get("data", "Unknown WS API error")
        self.gateway.write_log(f"WebSocket API error: {msg}")"""

assert old_on_packet in original, "WsApi.on_packet block not found"
original = original.replace(old_on_packet, new_on_packet)

# ============================================================
# Write back
# ============================================================
with open(path, "w") as f:
    f.write(original)

print("All OKX-parity patches applied successfully!")
print("New file length:", len(original))

# Syntax check
import py_compile
try:
    py_compile.compile(path, doraise=True)
    print("Syntax check: PASSED")
except py_compile.PyCompileError as e:
    print("Syntax check: FAILED", e)
