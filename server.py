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
        for key in ("value", "name", "schema_name"):
            if value.get(key) not in (None, ""):
                return str(value[key]).strip()
        return ""
    if isinstance(value, list):
        return ", ".join(str(x) for x in value)
    return str(value).strip() if value not in (None, "") else ""


def _flatten_attributes(source, attrs):
    """Flatten AtomicAssets attributes, including nested key/value objects."""
    if isinstance(source, dict):
        for key, value in source.items():
            key_s = str(key).strip().lower()
            if isinstance(value, dict):
                # Preserve direct value/name fields, then recurse.
                cleaned = _clean_text(value)
                if cleaned:
                    attrs.setdefault(key_s, cleaned)
                _flatten_attributes(value, attrs)
            elif isinstance(value, list):
                cleaned = _clean_text(value)
                if cleaned:
                    attrs.setdefault(key_s, cleaned)
                for item in value:
                    if isinstance(item, (dict, list)):
                        _flatten_attributes(item, attrs)
            else:
                cleaned = _clean_text(value)
                if cleaned:
                    attrs.setdefault(key_s, cleaned)


def get_asset_attributes(asset):
    """Flatten NFT attributes, including nested template.data structures."""
    attrs = {}
    for source in _all_attribute_sources(asset):
        _flatten_attributes(source, attrs)

    # Normalize keys so variants such as MoveCostIcon-immutable all match.
    normalized = {}
    for key, value in attrs.items():
        nk = re.sub(r"[^a-z0-9]", "", key.lower())
        if nk:
            normalized.setdefault(nk, value)

    # Common aliases used by different NFT schemas.
    aliases = {
        "cardname": ["cardname", "card_name", "card name"],
        "shine": ["shine", "shine_version", "shineversion", "variant"],
        "schema": ["schema", "schema_name", "schema_name_"],
        "movcost": ["movcost", "move_cost", "movement_cost", "movementcost", "move cost", "movecosticon-immutable", "movecosticon", "movecosticon_immutable"],
    }
    for canonical, keys in aliases.items():
        for key in keys:
            nk = re.sub(r"[^a-z0-9]", "", key.lower())
            if nk in normalized and normalized[nk] not in (None, ""):
                attrs[canonical] = normalized[nk]
                break

    # Last-resort schema-tolerant matching for MoveCostIcon variants.
    if "movcost" not in attrs:
        for nk, value in normalized.items():
            if "movecosticon" in nk or nk.startswith("movcost"):
                if _clean_text(value):
                    attrs["movcost"] = _clean_text(value)
                    break

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



def fetch_collection_assets(collection_name, search_terms=None, max_assets=5000):
    """Fetch collection NFTs, plus targeted searches for modifier-specific candidates."""
    if not collection_name:
        return []

    search_terms = [str(x).strip() for x in (search_terms or []) if str(x).strip()]
    results = []
    seen = set()

    def add_assets(data):
        for asset in data or []:
            aid = str(asset.get("asset_id", ""))
            if aid and aid not in seen:
                seen.add(aid)
                results.append(asset)

    # First do a normal collection scan. This catches movcost/shine/schema rules
    # even when they cannot be searched reliably by attribute name.
    for base_url in ATOMIC_ASSET_APIS:
        try:
            page = 1
            while len(results) < max_assets:
                r = SESSION.get(
                    f"{base_url}/assets",
                    params={"collection_name": collection_name, "limit": 1000, "page": page},
                    timeout=(3, 15),
                )
                r.raise_for_status()
                data = (r.json().get("data", []) or [])
                if not data:
                    break
                add_assets(data)
                if len(data) < 1000:
                    break
                page += 1
            if results:
                break
        except Exception:
            continue

    # AtomicAssets supports searching asset/template names. This targeted pass is
    # important for collections larger than max_assets: a missing cardname such as
    # "Robotron Soldier" must still be discoverable even if it is on a later page.
    for term in search_terms:
        for base_url in ATOMIC_ASSET_APIS:
            try:
                r = SESSION.get(
                    f"{base_url}/assets",
                    params={
                        "collection_name": collection_name,
                        "search": term,
                        "limit": 1000,
                        "page": 1,
                    },
                    timeout=(3, 15),
                )
                r.raise_for_status()
                add_assets((r.json().get("data", []) or []))
                break
            except Exception:
                continue

    return results

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

        # Build the BEST COMBINATION of up to 3 new NFTs from the whole collection.
        # Only modifiers not already covered by the current highest-score NFT set are
        # considered.  We also deduplicate identical NFT variants so the UI does not
        # show the same card repeatedly just because multiple copies exist.
        current_assets = []
        current_asset_ids = set()
        for row in winners:
            for asset_id in get_asset_ids_from_row(row):
                asset_id = str(asset_id)
                if asset_id in current_asset_ids:
                    continue
                current_asset_ids.add(asset_id)
                try:
                    current_assets.append(fetch_atomic_asset(asset_id))
                except Exception:
                    pass

        score_parameters = [
            mod for mod in parameters
            if isinstance(mod, dict) and str(mod.get("mod_type", "score")).lower() == "score"
        ]

        covered_modifier_indexes = set()
        for idx, mod in enumerate(score_parameters):
            if any(score_suggestion_for_asset(asset, [mod])[1] for asset in current_assets):
                covered_modifier_indexes.add(idx)

        missing_indexes = [
            idx for idx in range(len(score_parameters))
            if idx not in covered_modifier_indexes
        ]

        # Discover the collection from an NFT already owned by a highest-score user.
        collection_name = ""
        for asset in current_assets:
            collection_name = str(asset.get("collection_name") or "")
            if not collection_name:
                collection = asset.get("collection")
                if isinstance(collection, dict):
                    collection_name = str(
                        collection.get("collection_name") or collection.get("name") or ""
                    )
            if collection_name:
                break

        # Search specifically for card names mentioned by the Adventure modifiers as
        # well as scanning the collection. This prevents a valid third recommendation
        # from being missed just because its assets are on a later API page.
        targeted_terms = []
        for mod in score_parameters:
            if str(mod.get("affix_type", "")).strip().lower() == "cardname":
                value = _clean_text(mod.get("affix_string", ""))
                if value:
                    targeted_terms.append(value)
        collection_assets = fetch_collection_assets(collection_name, targeted_terms) if collection_name else []

        # For every collection NFT, determine exactly which missing modifiers it can
        # activate.  Keep only the best representative for each modifier combination.
        by_mask = {}
        for asset in collection_assets:
            asset_id = str(asset.get("asset_id", ""))
            if not asset_id or asset_id in current_asset_ids:
                continue

            bonus, all_matches = score_suggestion_for_asset(asset, score_parameters)
            if bonus <= 0:
                continue

            match_indexes = []
            matches = []
            for idx, mod in enumerate(score_parameters):
                if idx not in missing_indexes:
                    continue
                _, one_match = score_suggestion_for_asset(asset, [mod])
                if one_match:
                    match_indexes.append(idx)
                    matches.extend(one_match)

            if not match_indexes:
                continue

            mask = sum(1 << idx for idx in match_indexes)
            item = {
                "asset_id": asset_id,
                "name": str(asset_field(asset, "name") or f"NFT #{asset_id}"),
                "shine": get_shine(asset),
                "bonus": sum(float(m.get("bonus", 0)) for m in matches),
                "matches": matches,
                "_mask": mask,
            }

            # Same card/shine/modifier combination may have many copies in a
            # collection. Keep only one representative for the suggestion.
            signature = (
                item["name"].strip().lower(),
                item["shine"].strip().lower(),
                tuple(sorted((m["type"], str(m["value"]).lower(), float(m["bonus"])) for m in matches)),
            )
            existing = by_mask.get(mask)
            if existing is None or item["bonus"] > existing["bonus"]:
                by_mask[mask] = {**item, "_signature": signature}
            elif existing.get("_signature") == signature:
                continue

        # Deduplicate by card variant, while preserving the strongest modifier mask.
        unique_candidates = {}
        for item in by_mask.values():
            sig = item.pop("_signature", None)
            old = unique_candidates.get(sig)
            if old is None or item["bonus"] > old["bonus"]:
                unique_candidates[sig] = item

        candidate_list = list(unique_candidates.values())

        # Dynamic programming: find the best set of up to 3 NFTs where a modifier is
        # counted only once across the whole set. This is what makes the recommendation
        # a real "best 3 to add" result instead of simply listing the top 3 bonuses.
        best_states = {(0, 0): {"bonus": 0, "items": []}}
        for item_index, item in enumerate(candidate_list):
            # Recover the mask from the matches already computed for this candidate.
            item_mask = int(item.get("_mask", 0))
            if not item_mask:
                continue

            snapshot = list(best_states.items())
            for (count, mask), state in snapshot:
                if count >= 3:
                    continue
                new_mask = mask | item_mask
                # Recompute score from the union of modifier bits. Since each bit is
                # a single Adventure modifier, only newly covered bits add their value.
                added_bonus = 0
                for idx in missing_indexes:
                    bit = 1 << idx
                    if (new_mask & bit) and not (mask & bit):
                        try:
                            added_bonus += float(score_parameters[idx].get("mod_value", 0))
                        except (TypeError, ValueError):
                            pass
                new_bonus = state["bonus"] + added_bonus
                key = (count + 1, new_mask)
                if key not in best_states or new_bonus > best_states[key]["bonus"]:
                    best_states[key] = {
                        "bonus": new_bonus,
                        "items": state["items"] + [item_index],
                    }

        best_combo = None
        for (count, mask), state in best_states.items():
            if count == 0:
                continue
            if best_combo is None or state["bonus"] > best_combo["bonus"] or (
                state["bonus"] == best_combo["bonus"] and count < best_combo["count"]
            ):
                best_combo = {"count": count, "bonus": state["bonus"], "items": state["items"], "mask": mask}

        best_three = []
        if best_combo:
            for idx in best_combo["items"]:
                item = dict(candidate_list[idx])
                item["projected_score"] = highest + best_combo["bonus"]
                item.pop("_mask", None)
                best_three.append(item)

        # Sort the selected NFTs in descending individual contribution for readability.
        best_three.sort(key=lambda x: (-x["bonus"], x["name"].lower()))

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

            # Show whether this modifier is already covered by any NFT
            # belonging to the current highest-score user(s).
            matched_nfts = []
            for asset in current_assets:
                _, asset_matches = score_suggestion_for_asset(asset, [mod])
                if asset_matches:
                    matched_nfts.append({
                        "asset_id": str(asset.get("asset_id", "")),
                        "name": str(asset_field(asset, "name") or f"NFT #{asset.get('asset_id', '')}"),
                        "shine": get_shine(asset),
                    })

            available_mods.append({
                "type": affix,
                "value": str(label),
                "bonus": bonus,
                "covered": bool(matched_nfts),
                "matched_nfts": matched_nfts,
            })

        return jsonify({
            "adventure_id": int(aid),
            "highest_score": highest,
            "users": users,
            "best_three": best_three,
            "possible": best_three,
            "modifiers": available_mods,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
