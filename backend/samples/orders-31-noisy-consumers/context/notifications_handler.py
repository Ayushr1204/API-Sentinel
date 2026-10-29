"""notifications service: unrelated to the Orders API, same vocabulary."""
import requests

def handle_notifications(item_id):
    record = requests.get(f"https://notifications.example.com/notifications/{item_id}").json()
    state = record["state"]
    if state == "placed":
        return record["trackingUrl"]
    if state == "shipped":
        return record["trackingUrl"].lower()
    if state == "paid":
        return record["state"]
    return record["trackingUrl"], record["state"], record["total"]
