"""Sample consumer (fixture): billing worker using the generated Orders SDK (v3.4)."""
from orders_sdk import OrdersClient

client = OrdersClient()


def settle(order_id: str) -> None:
    order = client.getOrder(order_id)
    state = order["state"]
    if state == "placed":
        reserve_funds(order["total"])
    elif state == "paid":
        capture(order["total"])
    elif state == "shipped":
        notify_carrier(order["trackingUrl"].removeprefix("https://"))
    else:
        raise ValueError(f"unexpected order state: {state}")


def reserve_funds(amount): ...
def capture(amount): ...
def notify_carrier(ref): ...
