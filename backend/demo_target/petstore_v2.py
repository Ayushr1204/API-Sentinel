"""Demo deployment for live tests: behaves like samples/petstore-breaking/candidate.yaml (Pet Store v2).

Run:  uvicorn demo_target.petstore_v2:app --port 8100
Then investigate with target URL http://127.0.0.1:8100/v1
It exists only so live HTTP tests have a real server to hit; it is not part of API Sentinel itself.
"""
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

app = FastAPI(title="Pet Store v2 (demo target)")
PETS = [
    {"id": 1, "name": "Rex", "species": "dog", "status": "available"},
    {"id": 2, "species": "cat", "status": "pending"},          # v2: name optional, new status value, no tag
]


@app.get("/v1/pets")
def list_pets(limit: int = Query(20, ge=1, le=50), status: str | None = None,
              x_tenant_id: str | None = Header(None)):
    if not x_tenant_id:
        return JSONResponse({"detail": "X-Tenant-Id header required"}, status_code=400)
    if status is not None and status not in ("available", "adopted"):
        return JSONResponse({"detail": f"invalid status {status}"}, status_code=400)
    return [p for p in PETS if status is None or p["status"] == status][:limit]


@app.post("/v1/pets", status_code=201)
def create_pet(body: dict, x_api_key: str | None = Header(None)):
    if not x_api_key:
        raise HTTPException(401, "X-API-Key required")
    if "species" not in body:
        raise HTTPException(422, "species is required")
    return {"id": 3, **body, "status": "available"}


@app.get("/v1/pets/{pet_id}")
def get_pet(pet_id: int):
    for p in PETS:
        if p["id"] == pet_id:
            return p
    raise HTTPException(404, "not found")
