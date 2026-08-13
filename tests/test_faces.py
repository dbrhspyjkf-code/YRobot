import base64
import json

import numpy as np

from yrobot.faces import FaceDB


def test_face_db_refreshes_profiles_written_by_dashboard(tmp_path):
    path = tmp_path / "faces.json"
    db = FaceDB(path)
    sample = base64.b64encode(np.zeros((200, 200, 3), dtype=np.uint8).tobytes()).decode()
    path.write_text(
        json.dumps({"version": 1, "profiles": {"阿皮": [sample]}, "last_seen": {}}),
        encoding="utf-8",
    )

    assert db.refresh_if_changed() is True
    assert db.names == ["阿皮"]
