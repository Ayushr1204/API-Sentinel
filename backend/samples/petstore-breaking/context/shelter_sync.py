"""Sample consumer (fixture): nightly shelter sync job, built against Pet Store API v1."""
import requests

BASE = "https://api.petstore.example.com/v1"


def push_new_arrivals(arrivals):
    for a in arrivals:
        body = {"name": a.name, "tag": a.microchip_id}
        r = requests.post(f"{BASE}/pets", json=body, timeout=10)
        r.raise_for_status()


def remove_adopted(pet_ids):
    for pet_id in pet_ids:
        requests.delete(f"{BASE}/pets/{pet_id}", timeout=10).raise_for_status()


def archived_pets():
    r = requests.get(f"{BASE}/pets", params={"status": "archived", "limit": 100}, timeout=10)
    total = int(r.headers["X-Total-Count"])
    return total, r.json()
