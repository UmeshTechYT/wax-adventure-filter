import os
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
