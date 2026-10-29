"""carrier_feed service: unrelated to the Orders API, same vocabulary."""
import requests

def handle_carrier_feed(item_id):
    record = requests.get(f"https://carrier_feed.example.com/carrier_feed/{item_id}").json()
    state = record["state"]
    if state == "placed":
        return record["trackingUrl"]
    if state == "shipped":
        return record["trackingUrl"].lower()
    if state == "paid":
        return record["state"]
    return record["trackingUrl"], record["state"], record["total"]
