import os
import re
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

WAX_API = "https://wax.greymass.com/v1/chain/get_table_rows"

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

# Small in-memory caches. They reset if Render restarts.
ASSET_CACHE = {}
ASSET_CACHE_TTL = 300
IMAGE_CACHE = {}
IMAGE_CACHE_TTL = 3600
IMAGE_CACHE_MAX = 32

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "WAX-Adventure-Viewer/1.0"})


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
        r = SESSION.post(WAX_API, json=payload, timeout=(5, 20))
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


def fetch_atomic_asset(asset_id):
    asset_id = str(asset_id)
    now = time.time()
    cached = ASSET_CACHE.get(asset_id)
    if cached and now - cached[0] < ASSET_CACHE_TTL:
        return cached[1]

    last_error = None
    for base_url in ATOMIC_ASSET_APIS:
        try:
            r = SESSION.get(f"{base_url}/assets/{asset_id}", timeout=(2.5, 7))
            r.raise_for_status()
            data = r.json().get("data", {}) or {}
            if data:
                ASSET_CACHE[asset_id] = (now, data)
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


def get_shine(asset):
    # Check common AtomicAssets attribute names without changing existing NFT logic.
    fields = [
        "shine", "shine_version", "shineversion", "variant", "rarity",
        "rarity_name", "quality", "edition", "tier"
    ]
    for field in fields:
        value = asset_field(asset, field)
        if value not in (None, ""):
            return str(value)
    return ""


def normalize_ipfs_path(value):
    if not value:
        return None
    value = str(value).strip()

    if value.startswith("ipfs://"):
        value = value[7:]
    elif value.startswith(("http://", "https://")):
        m = re.match(r"^https?://[^/]+/ipfs/(.+)$", value, flags=re.IGNORECASE)
        if not m:
            return None
        value = m.group(1)

    value = value.lstrip("/")
    if value.startswith("ipfs/"):
        value = value[5:]
    if not value or any(ord(ch) < 32 for ch in value):
        return None

    # Bare CID/path values.
    cid = value.split("/", 1)[0]
    if not (cid.startswith("Qm") or cid.startswith("bafy") or cid.startswith("bafk") or cid.startswith("baga")):
        return None
    return value


def cache_image(path, content, content_type):
    IMAGE_CACHE[path] = (time.time(), content, content_type)
    while len(IMAGE_CACHE) > IMAGE_CACHE_MAX:
        oldest = min(IMAGE_CACHE.items(), key=lambda item: item[1][0])[0]
        IMAGE_CACHE.pop(oldest, None)


@app.get("/nft-image/<asset_id>")
def nft_image(asset_id):
    if not str(asset_id).isdigit():
        return jsonify({"error": "Asset ID must be a number."}), 400

    try:
        # top-users-nfts passes the already-known path, avoiding another metadata API call.
        ipfs_path = normalize_ipfs_path(request.args.get("path", ""))
        if not ipfs_path:
            asset = fetch_atomic_asset(str(asset_id))
            ipfs_path = normalize_ipfs_path(asset_field(asset, "img"))

        if not ipfs_path:
            return jsonify({"error": "No supported IPFS image found for this NFT."}), 404

        cached = IMAGE_CACHE.get(ipfs_path)
        if cached and time.time() - cached[0] < IMAGE_CACHE_TTL:
            _, content, content_type = cached
            result = Response(content, status=200, content_type=content_type)
            result.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=604800"
            return result

        last_error = None
        for gateway in IPFS_GATEWAYS:
            try:
                r = SESSION.get(gateway.format(path=ipfs_path), timeout=(2.5, 8))
                content_type = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if r.ok and r.content and (
                    content_type.startswith("image/") or content_type == "application/octet-stream"
                ):
                    if not content_type.startswith("image/"):
                        content_type = "image/jpeg"
                    cache_image(ipfs_path, r.content, content_type)
                    result = Response(r.content, status=200, content_type=content_type)
                    result.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=604800"
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


def get_mod_parameters(adventure_row):
    """Return the adventure's score modifiers from mods.parameters."""
    mods = adventure_row.get("mods", []) or []
    if isinstance(mods, dict):
        mods = mods.get("parameters", mods.get("mods", [])) or []
    if isinstance(mods, str):
        return []
    if isinstance(mods, list):
        # Some rows store parameters directly in mods; others nest them.
        params = []
        for item in mods:
            if isinstance(item, dict) and isinstance(item.get("parameters"), list):
                params.extend(item["parameters"])
            elif isinstance(item, dict) and "affix_type" in item:
                params.append(item)
        return params
    return []


def _all_attribute_sources(asset):
    template = asset.get("template") or {}
    return [
        asset.get("data") or {},
        asset.get("immutable_data") or {},
        asset.get("mutable_data") or {},
        template.get("immutable_data") or {},
        template.get("data") or {},
        asset,
    ]


def _clean_text(value):
    if isinstance(value, dict):
        # AtomicAssets schema/template objects commonly contain schema_name.
        for key in ("schema_name", "name", "value"):
            if value.get(key) not in (None, ""):
                return str(value[key])
        return ""
    if isinstance(value, list):
        return ", ".join(str(x) for x in value)
    return str(value).strip() if value not in (None, "") else ""


def get_asset_attributes(asset):
    """Flatten NFT attributes, including nested AtomicAssets schema/template data."""
    attrs = {}
    for source in _all_attribute_sources(asset):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            cleaned = _clean_text(value)
            if cleaned:
                attrs.setdefault(str(key).lower(), cleaned)

    # Common aliases used by different NFT schemas.
    aliases = {
        "cardname": ["cardname", "card_name", "card name"],
        "shine": ["shine", "shine_version", "shineversion", "variant"],
        "schema": ["schema", "schema_name", "schema_name_"],
        "movcost": ["movcost", "move_cost", "movement_cost", "movementcost", "move cost", "movecosticon-immutable", "movecosticon", "movecosticon_immutable"],
    }
    for canonical, keys in aliases.items():
        for key in keys:
            if key in attrs and attrs[key]:
                attrs[canonical] = attrs[key]
                break

    # AtomicAssets asset.schema can itself be an object.
    schema_obj = asset.get("schema")
    if isinstance(schema_obj, dict):
        schema_name = schema_obj.get("schema_name") or schema_obj.get("name")
        if schema_name:
            attrs["schema"] = str(schema_name)

    return attrs


def _numbers_equal(a, b):
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return False


def score_suggestion_for_asset(asset, parameters):
    """Calculate every matching score modifier for one NFT.

    Multiple modifiers can stack on the same NFT. This includes movcost and
    schema when those attributes are present in the NFT metadata.
    """
    attrs = get_asset_attributes(asset)
    matches = []
    total = 0

    for mod in parameters:
        if not isinstance(mod, dict) or str(mod.get("mod_type", "score")).lower() != "score":
            continue

        affix = str(mod.get("affix_type", "")).strip().lower()
        expected_string = _clean_text(mod.get("affix_string", ""))
        expected_value = mod.get("affix_value")
        try:
            bonus = float(mod.get("mod_value", 0))
        except (TypeError, ValueError):
            bonus = 0

        actual = ""
        matched = False

        if affix == "cardname":
            actual = attrs.get("cardname", attrs.get("name", ""))
            matched = actual.lower() == expected_string.lower()

        elif affix == "shine":
            actual = attrs.get("shine", "")
            matched = actual.lower() == expected_string.lower()

        elif affix == "schema":
            actual = attrs.get("schema", "")
            matched = actual.lower() == expected_string.lower()

        elif affix == "movcost":
            actual = attrs.get("movcost", "")
            # In mods, affix_string is '-' and the actual required number is affix_value.
            matched = _numbers_equal(actual, expected_value)

        if matched and bonus:
            total += bonus
            shown_value = expected_string if expected_string not in ("", "-") else str(expected_value)
            matches.append({
                "type": affix,
                "value": shown_value,
                "bonus": bonus,
            })

    return total, matches

def build_nft(asset_id):
    try:
        asset = fetch_atomic_asset(asset_id)
        name = asset_field(asset, "name") or f"NFT #{asset_id}"
        raw_image = asset_field(asset, "img")
        ipfs_path = normalize_ipfs_path(raw_image)
        return {
            "asset_id": str(asset_id),
            "name": str(name),
            "shine": get_shine(asset),
            "image": (
                f"/nft-image/{asset_id}?path={requests.utils.quote(ipfs_path, safe='')}"
                if ipfs_path else ""
            )
        }
    except Exception:
        return None


@app.get("/top-users-nfts")
def top_users_nfts():
    aid = request.args.get("adventure_id", "").strip()
    if not aid.isdigit():
        return jsonify({"error": "Adventure ID must be a number."}), 400

    try:
        participant_rows = get_table_rows("participants")
        participant_rows = [r for r in participant_rows if str(r.get("adventureid")) == aid]

        adventure_rows = get_table_rows("adventures")
        adventure_rows = [r for r in adventure_rows if str(r.get("adventureid", r.get("id", ""))) == aid]
        adventure_row = adventure_rows[0] if adventure_rows else {}
        parameters = get_mod_parameters(adventure_row)

        if not participant_rows:
            return jsonify({"adventure_id": int(aid), "highest_score": None, "users": [], "possible": []})

        def score(row):
            try:
                return float(row.get("score", 0))
            except (TypeError, ValueError):
                return 0

        highest = max(score(r) for r in participant_rows)
        winners = [r for r in participant_rows if score(r) == highest]
        users = []

        for row in winners:
            asset_ids = get_asset_ids_from_row(row)
            nfts = []
            if asset_ids:
                with ThreadPoolExecutor(max_workers=min(8, len(asset_ids))) as executor:
                    futures = [executor.submit(build_nft, asset_id) for asset_id in asset_ids]
                    for future in as_completed(futures):
                        nft = future.result()
                        if nft:
                            nfts.append(nft)

            users.append({
                "wallet": str(row.get("wallet", "")),
                "score": score(row),
                "nfts": nfts
            })

        # Build suggestions from NFTs already associated with the highest-score users.
        candidates = []
        seen = set()
        candidate_ids = []
        for row in winners:
            for asset_id in get_asset_ids_from_row(row):
                if asset_id not in seen:
                    seen.add(asset_id)
                    candidate_ids.append(asset_id)

        if parameters and candidate_ids:
            with ThreadPoolExecutor(max_workers=min(8, len(candidate_ids))) as executor:
                futures = {executor.submit(fetch_atomic_asset, asset_id): asset_id for asset_id in candidate_ids}
                for future in as_completed(futures):
                    asset_id = futures[future]
                    try:
                        asset = future.result()
                        bonus, matches = score_suggestion_for_asset(asset, parameters)
                        if bonus > 0:
                            candidates.append({
                                "asset_id": str(asset_id),
                                "name": str(asset_field(asset, "name") or f"NFT #{asset_id}"),
                                "shine": get_shine(asset),
                                "bonus": bonus,
                                "matches": matches,
                            })
                    except Exception:
                        continue

        candidates.sort(key=lambda x: (-x["bonus"], x["name"].lower()))

        # Unique modifier list for a compact explanation in the UI.
        available_mods = []
        for mod in parameters:
            if not isinstance(mod, dict) or str(mod.get("mod_type", "score")).lower() != "score":
                continue
            affix = str(mod.get("affix_type", ""))
            label = mod.get("affix_string", "")
            value = mod.get("affix_value")
            try:
                bonus = float(mod.get("mod_value", 0))
            except (TypeError, ValueError):
                bonus = 0
            if affix == "movcost" and label in ("", "-"):
                label = f"movcost {value}"
            available_mods.append({"type": affix, "value": str(label), "bonus": bonus})

        return jsonify({
            "adventure_id": int(aid),
            "highest_score": highest,
            "users": users,
            "possible": candidates[:10],
            "modifiers": available_mods,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
