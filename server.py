import os
import re
import requests
from flask import Flask, request, jsonify
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


@app.get("/top-users-nfts")
def top_users_nfts():
    aid = request.args.get("adventure_id", "").strip()
    if not aid.isdigit():
        return jsonify({"error": "Adventure ID must be a number."}), 400

    try:
        rows = get_table_rows("participants")
        rows = [r for r in rows if str(r.get("adventureid")) == aid]

        if not rows:
            return jsonify({"adventure_id": int(aid), "highest_score": None, "users": []})

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
                    r = requests.get(
                        f"https://wax.api.atomicassets.io/atomicassets/v1/assets/{asset_id}",
                        timeout=15
                    )
                    if not r.ok:
                        continue

                    asset = r.json().get("data", {})
                    immutable = asset.get("immutable_data") or {}
                    mutable = asset.get("mutable_data") or {}
                    img = immutable.get("img") or mutable.get("img") or ""

                    if img and not str(img).startswith(("http://", "https://")):
                        image = "https://ipfs.io/ipfs/" + str(img).replace("ipfs://", "")
                    else:
                        image = str(img)

                    name = (
                        immutable.get("name")
                        or mutable.get("name")
                        or asset.get("name")
                        or f"NFT #{asset_id}"
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
