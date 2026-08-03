from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]


@dataclass(frozen=True)
class AppPaths:
    data_root: Path
    database: Path
    manuals_original: Path
    manuals_extracted: Path
    exports: Path
    logs: Path

    @classmethod
    def from_environment(cls) -> "AppPaths":
        configured = os.getenv("APP_DATA_DIR", "data")
        root = Path(configured)
        if not root.is_absolute():
            root = PROJECT_ROOT / root
        return cls(
            data_root=root,
            database=root / "network_automation.db",
            manuals_original=root / "manuals" / "original",
            manuals_extracted=root / "manuals" / "extracted",
            exports=root / "exports",
            logs=root / "logs",
        )

    def ensure(self) -> None:
        for directory in (
            self.data_root,
            self.manuals_original,
            self.manuals_extracted,
            self.exports,
            self.logs,
        ):
            directory.mkdir(parents=True, exist_ok=True)


paths = AppPaths.from_environment()
