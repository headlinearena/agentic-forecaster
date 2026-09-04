#!/usr/bin/env python3
import argparse
import json
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect an AkShare dataset")
    parser.add_argument("--dataset", required=True, help="AkShare function name")
    parser.add_argument("--params", default="{}", help="JSON params object")
    parser.add_argument("--rows", type=int, default=3, help="Number of preview rows")
    return parser.parse_args()


def import_akshare():
    try:
        import akshare as ak  # type: ignore
    except ImportError as exc:
        raise RuntimeError("akshare is not installed; run `uv pip install akshare pandas`") from exc
    return ak


def main() -> int:
    args = parse_args()
    params: dict[str, Any] = json.loads(args.params)
    ak = import_akshare()
    func = getattr(ak, args.dataset, None)
    if func is None:
        raise RuntimeError(f"AkShare does not expose dataset {args.dataset}")
    data = func(**params)
    payload = {
        "dataset": args.dataset,
        "type": type(data).__name__,
    }
    if hasattr(data, "columns"):
        payload["columns"] = [str(column) for column in data.columns]
        payload["rows"] = data.head(args.rows).to_dict(orient="records")
    else:
        payload["preview"] = str(data)[:1000]
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
