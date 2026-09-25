from __future__ import annotations

from typing import Any

from ..a2a import A2AMessage, Agent, CaseContext
from ..timeline import Window, brl, claimed_topic, in_window, money, total, unique_rows

PRODUCT_TOPIC = "unavailable_order_paid"


class OrderAgent(Agent):
    """Collect item, seller and product context for the resolved order."""

    actor = "order-agent"
    tools = frozenset({"get_order_items", "get_product_context", "get_sellers"})

    async def handle(self, ctx: CaseContext, message: A2AMessage) -> A2AMessage:
        order_id = message.payload["order_id"]
        window = Window.from_payload(message.payload.get("window"))
        refs: dict[str, str] = {}
        payload: dict[str, Any] = {"item_ids": [], "seller_ids": [], "items": []}

        items = await ctx.fetch(self, "get_order_items", order_id=order_id)
        raw_rows = items.data if items is not None and isinstance(items.data, list) else []
        rows = unique_rows(row for row in raw_rows if isinstance(row, dict))
        if items is not None:
            refs["item"] = items.ref
        # Keep item rows whose seller handoff limit falls in the case episode.
        scoped = in_window(rows, window, "shipping_limit_date") or rows
        payload["items"] = [
            {
                "order_item_id": row.get("order_item_id"),
                "product_id": row.get("product_id"),
                "seller_id": row.get("seller_id"),
                "shipping_limit_at": row.get("shipping_limit_date"),
                "price_brl": _float(row.get("price")),
                "freight_brl": _float(row.get("freight_value")),
            }
            for row in scoped
        ]
        payload["item_ids"] = _unique(row.get("order_item_id") for row in scoped)
        payload["seller_ids"] = _unique(row.get("seller_id") for row in scoped)
        payload["product_ids"] = _unique(row.get("product_id") for row in scoped)
        if scoped:
            payload["order_value_brl"] = brl(
                total(money(row.get("price")) for row in scoped)
                + total(money(row.get("freight_value")) for row in scoped)
            )
            payload["freight_brl"] = brl(total(money(row.get("freight_value")) for row in scoped))

        duplicated = bool(message.payload.get("duplicated_records"))
        if rows and (duplicated or not payload["seller_ids"]):
            sellers = await ctx.fetch(self, "get_sellers", order_id=order_id)
            if sellers is not None and isinstance(sellers.data, list):
                refs["seller"] = sellers.ref
                if not payload["seller_ids"]:
                    payload["seller_ids"] = _unique(row.get("seller_id") for row in sellers.data)

        # Product context only supports an unavailable-product conclusion (or cross-checks a
        # duplicated order record); skip it otherwise.
        episode_status = (message.payload.get("episode_order") or {}).get("order_status")
        wants_products = ctx.case.get("investigation_scope", {}).get(
            "include_product_context", True
        ) and (
            duplicated
            or episode_status == "unavailable"
            or claimed_topic(ctx.case) == PRODUCT_TOPIC
        )
        if wants_products:
            products = await ctx.fetch(self, "get_product_context", order_id=order_id)
            if products is not None and isinstance(products.data, list):
                refs["product"] = products.ref
                wanted = set(payload["item_ids"])
                payload["categories"] = _unique(
                    row.get("category_name_english")
                    for row in products.data
                    if isinstance(row, dict) and (not wanted or row.get("order_item_id") in wanted)
                )

        payload["evidence"] = refs
        intent = "findings" if scoped else "insufficient_evidence"
        return self.reply(message, intent, payload, refs.values())


def _unique(values: Any) -> list[str]:
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value))


def _float(value: Any) -> float | None:
    amount = money(value)
    return brl(amount) if amount is not None else None
