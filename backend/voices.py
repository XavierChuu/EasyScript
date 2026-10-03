"""
Voice library: remember named speakers across videos.

After diarization every speaker has a centroid embedding. When the user names
a speaker ("Speaker A" → "Lan") the panel can save that embedding; on later
runs each new speaker is compared (cosine) with the saved voices and takes the
name of the best match above THRESHOLD. Matching is one-to-one: two speakers
in the same video never get the same saved name.

Stored in ~/.easyscript/voices.json as unit-length float lists; a saved voice
is a running mean of every embedding saved under that name.
"""

import json
import os
import threading
import time

import numpy as np

VOICES_PATH = os.path.join(os.path.expanduser("~"), ".easyscript", "voices.json")
# Cosine similarity needed to reuse a saved name. With the community-1
# embeddings on AMI ES2004a, the same speaker in the two halves of the meeting
# scored 0.87–0.96 and different speakers at most 0.24.
THRESHOLD = 0.5
_lock = threading.Lock()


def _unit(v):
    v = np.asarray(v, dtype=np.float64).ravel()
    n = np.linalg.norm(v)
    if not np.isfinite(n) or n == 0:
        raise ValueError("empty embedding")
    return v / n


def _load(path=None):
    try:
        with open(path or VOICES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and isinstance(data.get("voices"), list) else {"voices": []}
    except (FileNotFoundError, json.JSONDecodeError):
        return {"voices": []}


def _save(data, path=None):
    path = path or VOICES_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def list_voices(path=None):
    """[{name, samples, updated}] without the embeddings."""
    return [{"name": v["name"], "samples": v.get("samples", 1), "updated": v.get("updated")}
            for v in _load(path)["voices"]]


def save_voice(name, embedding, path=None):
    name = (name or "").strip()
    if not name:
        raise ValueError("name required")
    emb = _unit(embedding)
    with _lock:
        data = _load(path)
        for v in data["voices"]:
            if v["name"].casefold() == name.casefold():
                n = int(v.get("samples", 1))
                mean = _unit(np.asarray(v["embedding"]) * n + emb)
                v.update(embedding=mean.tolist(), samples=n + 1, updated=int(time.time()), name=name)
                break
        else:
            data["voices"].append({"name": name, "embedding": emb.tolist(), "samples": 1,
                                   "updated": int(time.time())})
        _save(data, path)
    return {"name": name}


def delete_voice(name, path=None):
    with _lock:
        data = _load(path)
        before = len(data["voices"])
        data["voices"] = [v for v in data["voices"] if v["name"].casefold() != (name or "").casefold()]
        _save(data, path)
    return before != len(data["voices"])


def match(centroids, threshold=THRESHOLD, path=None):
    """{speaker_id: {"name", "score"}} for speakers that match a saved voice.

    centroids: {speaker_id: embedding}. Greedy one-to-one assignment by
    descending similarity.
    """
    voices = _load(path)["voices"]
    if not voices or not centroids:
        return {}
    names = [v["name"] for v in voices]
    V = np.stack([_unit(v["embedding"]) for v in voices])
    pairs = []
    for spk, emb in centroids.items():
        try:
            e = _unit(emb)
        except ValueError:
            continue
        if e.shape[0] != V.shape[1]:
            continue
        for j, s in enumerate(V @ e):
            if s >= threshold:
                pairs.append((float(s), spk, j))
    pairs.sort(reverse=True)
    used_spk, used_voice, out = set(), set(), {}
    for s, spk, j in pairs:
        if spk in used_spk or j in used_voice:
            continue
        used_spk.add(spk)
        used_voice.add(j)
        out[spk] = {"name": names[j], "score": round(s, 3)}
    return out
