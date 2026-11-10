import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SAMPLES = ROOT / "examples" / "sample_data"


def make_config(tmp: Path, modality: str, k: int = 1, sources=None, **extra) -> Path:
    raw = yaml.safe_load((ROOT / "personality_annotator" / "config.example.yaml").read_text())
    if sources is None:
        folder = {"video": "videos", "audio": "audio", "text": "texts"}[modality]
        dst = tmp / folder
        shutil.copytree(SAMPLES / folder, dst)
        sources = [{"type": "local", "path": str(dst)}]
    raw.update(modality=modality, sources=sources, annotators_per_item=k,
               data_dir=str(tmp / "data"), **extra)
    p = tmp / "config.yaml"
    p.write_text(yaml.safe_dump(raw))
    return p


@pytest.fixture
def make_client(tmp_path):
    from fastapi.testclient import TestClient
    from personality_annotator.app import create_app
    clients = []

    def _make(modality="video", k=1, **kw):
        sub = tmp_path / f"{modality}_{len(clients)}"
        sub.mkdir()
        app = create_app(make_config(sub, modality, k, **kw))
        c = TestClient(app)
        c.__enter__()
        clients.append(c)
        return c

    yield _make
    for c in clients:
        c.__exit__(None, None, None)
