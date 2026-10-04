# Build EasyScript 2.2 trên macOS (Apple Silicon)

Hướng dẫn build lại **backend** cho Mac từ code hiện tại (bản 2.2: Turbo + word timing, speaker community-1, voice library, NLLB trên CTranslate2) và đóng gói bản phát hành.

> Panel (`.zxp`) **không cần build lại trên Mac**: file `dist/EasyScript-Premiere.zxp` 2.2.0 build trên Windows dùng được cho cả hai hệ điều hành. Chỉ có backend là phải build riêng cho từng máy.

---

## 0. Chuẩn bị

| Cần có | Ghi chú |
|---|---|
| Mac Apple Silicon (M1–M4), macOS 13+ | Build trên Mac Intel cũng chạy được nhưng không có Metal cho Whisper |
| Python **3.11 arm64** | `brew install python@3.11` hoặc bản cài từ python.org. Kiểm tra: `python3.11 -c "import platform; print(platform.machine())"` phải ra `arm64` |
| Xcode Command Line Tools | `xcode-select --install` |
| ~20 GB trống, có Internet | venv + bản build ~6–8 GB; lần chạy đầu tải model |

### Lấy code

Code 2.2 nằm trên nhánh `main` của GitHub (tag `v2.2.0`). Trên Mac:

```bash
cd ~/EasyScript_APP          # thư mục repo có sẵn trên Mac
git checkout main
git pull
git log --oneline -1         # phải thấy commit 2.2 (vd. "Release readiness…" hoặc mới hơn)
```

Nếu repo trên Mac có thay đổi chưa commit (file .DS_Store, venv…), `git status` trước; các thư mục `backend/venv`, `backend/bin` đã được git bỏ qua nên không ảnh hưởng.

Nếu không dùng GitHub thì copy cả thư mục repo, **trừ** các thư mục chỉ dùng cho Windows: `backend/venv-win`, `backend/build_backend`, `backend/dist_backend`, `backend/models`.

### File không có trong git (chỉ cần khi ký lại ZXP)

- `cep-extension-v2.1/easyscript_cert.p12` — chứng chỉ ký panel (mật khẩu ghi trong `package_zxp.sh`)
- `cep-extension-v2.1/ZXPSignCmd` — bản ZXPSignCmd cho macOS

Hai file này đã có sẵn trên máy Mac cũ. Đừng commit chúng.

---

## 1. Tạo môi trường Python

Venv cũ trên Mac (`backend/venv`) được tạo cho bản 2.1 với pyannote 3.x, faster-whisper 1.1. **Tạo lại từ đầu** cho sạch:

```bash
cd ~/EasyScript_APP
rm -rf backend/venv
python3.11 -m venv backend/venv
backend/venv/bin/pip install --upgrade pip
backend/venv/bin/pip install -r backend/requirements.txt
```

Những gói chính được cài (đã pin trong `requirements.txt`):

- `faster-whisper==1.2.1`, `mlx-whisper` (Whisper trên Metal)
- `torch` / `torchaudio` 2.8, `torchcodec` 0.7 (cặp khớp với torch 2.8)
- `pyannote.audio` 4.x (speaker community-1)
- `onnx` (chỉ dùng lúc build), `python-multipart`, `setuptools<81`

Các gói `nvidia-*` và `onnxruntime-directml` chỉ cài trên Windows (đã có điều kiện `sys_platform == "win32"`). Trên Mac, `onnxruntime` thường (đi kèm faster-whisper) là đủ.

Kiểm tra nhanh:

```bash
backend/venv/bin/python -m pip check
backend/venv/bin/python -m unittest discover -s backend/tests -t backend     # phải ra: OK (48 tests)
```

---

## 2. Chạy thử bản dev (trước khi build)

```bash
cd backend
EASYSCRIPT_TOKEN=dev PORT=9877 venv/bin/python server.py
```

Mở terminal khác, kiểm tra:

```bash
curl -s http://127.0.0.1:9877/health
```

Hoặc chạy `start_dev.command` (dùng cổng 9876, token `dev`) để mở panel trong trình duyệt.

---

## 3. Build backend

```bash
cd ~/EasyScript_APP
./scripts/build_backend_mac.sh
```

Script sẽ:

1. Tải model speaker community-1 (~33 MB) vào `backend/models/speaker-diarization-community-1`
2. Export phần embedding sang ONNX (`tools/export_embedding_onnx.py`)
3. Chạy PyInstaller với `backend/easyscript_backend.spec`
4. Ra kết quả: `backend/dist_backend/EasyScript-backend/EasyScript-backend`

Các cảnh báo `Hidden import ... not found` (`torchcodec.decoders`, `multipart.multipart`…) là **bình thường**.

---

## 4. Kiểm tra bản build (quan trọng)

Chạy bản build ở cổng riêng, **không đụng** backend đang cài ở `~/.easyscript/backend`:

```bash
PORT=9878 EASYSCRIPT_TOKEN=smoke ./backend/dist_backend/EasyScript-backend/EasyScript-backend &
sleep 15; curl -s http://127.0.0.1:9878/health
```

Rồi mở panel trong trình duyệt nối vào cổng đó: phục vụ `cep-extension-v2.1/` và mở `index.html?token=smoke&port=9878` (hoặc cài bản build rồi test trong Premiere). Kiểm tra lần lượt:

| Tính năng | Kết quả mong đợi |
|---|---|
| Transcribe (model mặc định **Turbo**) | Lần đầu tải ~1,6 GB. Dòng thiết bị ghi **Metal GPU**. Câu có timestamp từng từ (chế độ hiển thị "Max words" ngắt đúng chỗ) |
| Vocabulary | Nhập tên riêng → được nhận đúng hơn |
| Speakers | **Không cần HF token**. Tiến trình ghi `GPU · Metal`. Câu có 2 người bị tách đúng chỗ |
| Đổi tên speaker | Hiện "Voice … saved". Chạy lại Speakers → tên tự gán (dấu ✓) |
| Tag speaker → New sequence | Sequence mới, clip cắt tại chỗ đổi người, đặt tên theo người, nhạc nền không bị cắt |
| Translate → NLLB | Download lần đầu ~2,4 GB rồi "Optimizing…". Trên Mac chạy CPU (CTranslate2 không có Metal): ~0,7 giây/câu |
| Song mode | Demucs tải model lần đầu, rồi transcribe lời bài hát |
| Detect silence / Cut / Beats / XML | Như bản 2.1 |

Log nằm ở `~/.easyscript/backend_out.log` và `~/.easyscript/backend.log`.

Dừng bản test: `kill %1` (hoặc `pkill -f "dist_backend/EasyScript-backend"`).

### Những điểm Windows chưa kiểm chứng được (cần chú ý trên Mac)

- **mlx-whisper** giờ nhận audio dạng mảng numpy (cắt chunk tại chỗ im lặng) và vocabulary qua `initial_prompt`. Nếu transcribe lỗi, xem log trước tiên.
- **pyannote trên Metal (MPS)**: backend đã bật `PYTORCH_ENABLE_MPS_FALLBACK=1`. Nếu Speakers vẫn lỗi hoặc ra kết quả lạ trên MPS, chạy thử với `EASYSCRIPT_DIARIZE_DEVICE=cpu` (diarization dùng ONNX trên CPU, vẫn nhanh hơn torch CPU). Ví dụ: `EASYSCRIPT_DIARIZE_DEVICE=cpu PORT=9878 EASYSCRIPT_TOKEN=smoke ./backend/dist_backend/EasyScript-backend/EasyScript-backend`. Nếu CPU ổn mà MPS lỗi, sửa `detect_torch_device()` trong `backend/diarizer.py` để mặc định dùng CPU trên Mac.
- Màu label của clip sau **Tag speaker → New sequence** (ghi theo chuẩn FCP7 XML `<labels><label2>`).

---

## 5. Cài vào máy để dùng

```bash
pkill -f "\.easyscript/backend/EasyScript-backend" || true      # dừng backend cũ nếu đang chạy
mv ~/.easyscript/backend ~/.easyscript/backend.prev 2>/dev/null || true
mkdir -p ~/.easyscript/backend
cp -R backend/dist_backend/EasyScript-backend/. ~/.easyscript/backend/
xattr -dr com.apple.quarantine ~/.easyscript/backend
```

Cài panel 2.2.0 (`dist/EasyScript-Premiere.zxp`) bằng ZXP installer, mở lại Premiere. Panel tự khởi động backend.

---

## 6. Đóng gói bản phát hành cho người dùng

```bash
./scripts/make_release.sh                         # ký ZXP (cần ZXPSignCmd + .p12) và tạo release/EasyScript/
cp -R backend/dist_backend/EasyScript-backend/. release/EasyScript/backend/
rm release/EasyScript/backend/PUT-BACKEND-BUILD-HERE.txt
cd release && ditto -c -k --keepParent EasyScript EasyScript-2.2-macOS.zip
```

Dùng `ditto` (không dùng Finder/zip thường) để giữ quyền thực thi và symlink trong bản build. Người dùng giải nén và chạy `install-mac.command`.

Nếu không có ZXPSignCmd/.p12 trên Mac: bỏ dòng ký trong `make_release.sh` và copy sẵn `dist/EasyScript-Premiere.zxp` (bản 2.2.0 từ Windows) vào `release/EasyScript/`.

---

## Sự cố thường gặp

| Triệu chứng | Nguyên nhân / cách xử lý |
|---|---|
| Speakers đứng ở "Loading speaker model…" | Xem log. Bản 2.2 đã loại `torchcodec` khỏi bundle — nếu spec bị sửa lại, đừng thêm torchcodec vào |
| Translate lỗi "Unrolling kwargs … None class" | `module_collection_mode={"transformers": "py"}` trong spec bị mất |
| "DLL/dylib load failed …" khi import | Gói thiếu thư viện đi kèm; chạy lại build, kiểm tra cảnh báo của PyInstaller |
| Lần Speakers đầu tiên chậm (~10 s) | Bình thường: dựng cache font matplotlib (lưu ở `~/.easyscript/matplotlib`, chỉ một lần) |
| Gatekeeper chặn backend | `xattr -dr com.apple.quarantine ~/.easyscript/backend` |
