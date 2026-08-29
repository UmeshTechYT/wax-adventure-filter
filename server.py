import os
import re
import requests
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

WAX_API = "https://wax.greymass.com/v1/chain/get_table_rows"

def get_table_rows(table):
    rows = []
    lower_bound = ""
    while True:
        payload = {
            "json": True,
            "code": "adventure.mc",
            "scope": "adventure.mc",
            "table": table,
            "limit": 1000,
            "lower_bound": lower_bound
        }
        r = requests.post(WAX_API, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        rows.extend(data.get("rows", []))
        if not data.get("more"):
            break
        next_key = data.get("next_key")
        if not next_key or next_key == lower_bound:
            break
        lower_bound = next_key
    return rows

@app.get("/")
def home():
    return "WAX Adventure Viewer API is running!"

@app.get("/participants")
def participants():
    aid = request.args.get("adventure_id", "").strip()
    if not aid.isdigit():
        return jsonify({"error": "Adventure ID must be a number."}), 400
    try:
        rows = get_table_rows("participants")
        rows = [r for r in rows if str(r.get("adventureid")) == aid]
        return jsonify({"adventure_id": aid, "count": len(rows), "rows": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 502

@app.get("/adventure")
def adventure():
    aid = request.args.get("adventure_id", "").strip()
    if not aid.isdigit():
        return jsonify({"error": "Adventure ID must be a number."}), 400
    try:
        rows = get_table_rows("adventures")
        rows = [r for r in rows if str(r.get("adventureid", r.get("id", ""))) == aid]
        return jsonify({"adventure_id": aid, "count": len(rows), "rows": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.get("/latest-adventure")
def latest_adventure():
    try:
        rows = get_table_rows("adventures")

        def adventure_id(row):
            value = row.get("adventureid", row.get("id", ""))
            try:
                return int(value)
            except (TypeError, ValueError):
                return -1

        valid_rows = [r for r in rows if adventure_id(r) >= 0]
        if not valid_rows:
            return jsonify({"error": "No adventures found."}), 404

        latest = max(valid_rows, key=adventure_id)
        aid = adventure_id(latest)

        return jsonify({"adventure_id": aid, "row": latest})
    except Exception as e:
        return jsonify({"error": str(e)}), 502

def get_asset_ids_from_row(row):
    value = row.get("asset_ids", row.get("assets", []))
    if isinstance(value, list):
        return [str(x) for x in value if str(x).isdigit()]
    if isinstance(value, str):
        return re.findall(r"\d+", value)
    return []


ATOMIC_ASSET_APIS = [
    "https://wax.api.atomicassets.io/atomicassets/v1",
    "https://wax-atomic-api.eosphere.io/atomicassets/v1",
    "https://atomic-api.wax.cryptolions.io/atomicassets/v1",
]

IPFS_GATEWAYS = [
    "https://ipfs.io/ipfs/{path}",
    "https://dweb.link/ipfs/{path}",
    "https://gateway.pinata.cloud/ipfs/{path}",
]


def fetch_atomic_asset(asset_id):
    last_error = None
    for base_url in ATOMIC_ASSET_APIS:
        try:
            r = requests.get(f"{base_url}/assets/{asset_id}", timeout=(3, 8))
            r.raise_for_status()
            data = r.json().get("data", {}) or {}
            if data:
                return data
        except Exception as e:
            last_error = e
    raise RuntimeError(f"AtomicAssets metadata unavailable for {asset_id}: {last_error}")


def asset_field(asset, field):
    data = asset.get("data") or {}
    immutable = asset.get("immutable_data") or {}
    mutable = asset.get("mutable_data") or {}
    template = asset.get("template") or {}
    template_immutable = template.get("immutable_data") or {}
    template_data = template.get("data") or {}
    return (
        data.get(field) or immutable.get(field) or mutable.get(field)
        or template_immutable.get(field) or template_data.get(field)
        or asset.get(field) or ""
    )


def normalize_ipfs_path(value):
    if not value:
        return None
    value = str(value).strip()
    if value.startswith("ipfs://"):
        value = value[7:]
    if value.startswith(("http://", "https://")):
        m = re.match(r"^https?://[^/]+/ipfs/(.+)$", value, flags=re.IGNORECASE)
        if not m:
            return None
        value = m.group(1)
    value = value.lstrip("/")
    if value.startswith("ipfs/"):
        value = value[5:]
    if not value:
        return None
    cid = value.split("/", 1)[0]
    if not (cid.startswith("Qm") or cid.startswith("bafy") or cid.startswith("bafk") or cid.startswith("baga")):
        return None
    if any(ord(ch) < 32 for ch in value):
        return None
    return value


@app.get("/nft-image/<asset_id>")
def nft_image(asset_id):
    if not str(asset_id).isdigit():
        return jsonify({"error": "Asset ID must be a number."}), 400
    try:
        asset = fetch_atomic_asset(str(asset_id))
        ipfs_path = normalize_ipfs_path(asset_field(asset, "img"))
        if not ipfs_path:
            return jsonify({"error": "No supported IPFS image found for this NFT."}), 404

        last_error = None
        for gateway in IPFS_GATEWAYS:
            try:
                r = requests.get(
                    gateway.format(path=ipfs_path),
                    timeout=(4, 15),
                    headers={"User-Agent": "WAX-Adventure-Viewer/1.0"}
                )
                content_type = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if r.ok and r.content and (
                    content_type.startswith("image/")
                    or content_type == "application/octet-stream"
                ):
                    if not content_type.startswith("image/"):
                        content_type = "image/jpeg"
                    result = Response(r.content, status=200, content_type=content_type)
                    result.headers["Cache-Control"] = (
                        "public, max-age=86400, stale-while-revalidate=604800"
                    )
                    return result
                last_error = f"Gateway returned {r.status_code} ({content_type or 'unknown content type'})"
            except Exception as e:
                last_error = e

        return jsonify({
            "error": "All IPFS image gateways failed.",
            "detail": str(last_error) if last_error else ""
        }), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.get("/top-users-nfts")
def top_users_nfts():
    aid = request.args.get("adventure_id", "").strip()
    if not aid.isdigit():
        return jsonify({"error": "Adventure ID must be a number."}), 400

    try:
        rows = get_table_rows("participants")
        rows = [r for r in rows if str(r.get("adventureid")) == aid]

        if not rows:
            return jsonify({
                "adventure_id": int(aid),
                "highest_score": None,
                "users": []
            })

        def score(row):
            try:
                return float(row.get("score", 0))
            except (TypeError, ValueError):
                return 0

        highest = max(score(r) for r in rows)
        winners = [r for r in rows if score(r) == highest]

        users = []
        for row in winners:
            nfts = []

            for asset_id in get_asset_ids_from_row(row):
                try:
                    asset = fetch_atomic_asset(asset_id)
                    name = asset_field(asset, "name") or f"NFT #{asset_id}"
                    raw_image = asset_field(asset, "img")
                    image = (
                        f"/nft-image/{asset_id}"
                        if normalize_ipfs_path(raw_image)
                        else ""
                    )

                    nfts.append({
                        "asset_id": asset_id,
                        "name": name,
                        "image": image
                    })
                except Exception:
                    continue

            users.append({
                "wallet": str(row.get("wallet", "")),
                "score": score(row),
                "nfts": nfts
            })

        return jsonify({
            "adventure_id": int(aid),
            "highest_score": highest,
            "users": users
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
