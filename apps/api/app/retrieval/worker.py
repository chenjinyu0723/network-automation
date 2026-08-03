from __future__ import annotations

import argparse

from app.retrieval.embeddings import run_embedding_job


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    run_embedding_job(args.job_id)


if __name__ == "__main__":
    main()
