"""analytics service: unrelated to the Orders API, same vocabulary."""
import requests

def handle_analytics(item_id):
    record = requests.get(f"https://analytics.example.com/analytics/{item_id}").json()
    state = record["state"]
    if state == "placed":
        return record["trackingUrl"]
    if state == "shipped":
        return record["trackingUrl"].lower()
    if state == "paid":
        return record["state"]
    return record["trackingUrl"], record["state"], record["total"]
