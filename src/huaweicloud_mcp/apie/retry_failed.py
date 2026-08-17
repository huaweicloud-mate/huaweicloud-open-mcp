"""重试 raw/apis_detail.json 中 failed 项（429 加大退避）。"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import region_paths

BASE = "https://console.huaweicloud.com/apiexplorer/new/v4/apis/detail"
REGION = region_paths.current_region()
DETAIL_PATH = region_paths.raw_detail_path()


def _request(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    for attempt in range(10):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                jb = json.loads(body)
            except Exception:
                jb = {}
            if e.code == 429:
                print(f"    429, sleep 20s (attempt {attempt + 1})", flush=True)
                time.sleep(20)
                continue
            return jb, e
        except Exception as e:
            if attempt == 9:
                raise
            time.sleep(5)
    return {}, Exception("exhausted retries")


def fetch_detail(product_short, name):
    data, err = _request(f"{BASE}?{urllib.parse.urlencode({'product_short': product_short, 'name': name, 'region_id': REGION})}")
    if err is None:
        return data, None
    if data.get("error_code") == "APIEXPLORER.1055":
        data2, err2 = _request(f"{BASE}?{urllib.parse.urlencode({'product_short': product_short, 'name': name})}")
        if err2 is None:
            return data2, None
        if data2.get("error_code") == "APIEXPLORER.1055":
            return {"product_short": product_short, "name": name, "empty": True}, None
        return data2, err2
    return data, err


def main():
    with open(DETAIL_PATH, "r", encoding="utf-8") as f:
        result = json.load(f)

    failed = result["failed"]
    done = result["apis"]
    still_failed = []

    print(f"retrying {len(failed)} failed items", flush=True)
    for f in failed:
        key = f"{f['product_short']}::{f['name']}"
        try:
            det, err = fetch_detail(f["product_short"], f["name"])
            if err is None:
                done[key] = det
                print(f"  OK {key}", flush=True)
            else:
                still_failed.append({**f, "error": str(err)})
                print(f"  FAIL {key}: {err}", flush=True)
        except Exception as e:
            still_failed.append({**f, "error": str(e)})
            print(f"  FAIL {key}: {e}", flush=True)
        time.sleep(2)

    result["apis"] = done
    result["failed"] = still_failed
    result["total_apis"] = len(done)
    with open(DETAIL_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"Done. total={len(done)} failed={len(still_failed)}", flush=True)


if __name__ == "__main__":
    main()
