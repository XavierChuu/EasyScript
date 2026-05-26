# Override for stock PyInstaller webrtcvad hook.
# On systems where webrtcvad-wheels (prebuilt binary wheel) is installed
# instead of webrtcvad, copy_metadata('webrtcvad') fails. Try both names.

from PyInstaller.utils.hooks import copy_metadata

datas = []
for name in ("webrtcvad", "webrtcvad-wheels"):
    try:
        datas += copy_metadata(name)
        break
    except Exception:
        continue
