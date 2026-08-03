from __future__ import annotations

import argparse
import traceback

from app.ingestion.pipeline import run_import


def main() -> None:
    parser = argparse.ArgumentParser(description="Local persisted manual-import worker")
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    try:
        run_import(args.job_id)
    except Exception:  # pragma: no cover - final defense; normal errors are written into the job.
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
