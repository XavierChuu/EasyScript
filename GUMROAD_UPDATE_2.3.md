# EasyScript 2.3 — Gumroad update post

Copy the block you need into Gumroad ▸ Product ▸ Content ▸ "Send update" (or the product's update post).

---

## ENGLISH

**Subject:** EasyScript 2.3 is here — separate voice from music, right inside Premiere Pro

Hi everyone,

EasyScript 2.3 is out, and it's a free update for all customers. The big addition: you can now split any clip into **voice** and **music** without leaving Premiere.

**✦ What's new**

**🎙 Voice / music separation (new)**
Click **Separate voice / music…** and pick:
- **Voice only** — music removed, speech kept. Cleaner transcription, even on videos with loud background music or songs.
- **Music only** — voice removed. The best input for beat detection.

The separated audio replaces the original in the waveform, playback, transcription and Beats, and the **Original | Voice | Music** switch toggles between them instantly. Runs locally with a state-of-the-art model (Mel-Band RoFormer) — nothing is uploaded.

**📥 Import voice / music to the timeline (new)**
One click places the separated audio back at its original timecode, on the first audio track that is free for the whole range. It never overwrites existing audio, skips locked tracks, and adds a new track if every track is busy. Files are saved in an **EasyScript Stems** folder and project bin.

**🥁 Beat markers: "Selected clip" (new)**
Markers can now go on the clip you select in the timeline (for example the music you just imported) and move with it. With nothing selected, they go on the clip of the audio loaded in the panel. The status line tells you which clip was marked.

**⚡ GPU first**
Separation and speaker detection use your GPU when available — Apple Silicon (Metal), NVIDIA (CUDA) or any Windows GPU (DirectML) — and fall back to the CPU otherwise. The progress line shows which device is running.

**✦ A typical new workflow**
1. Select a clip → **Load audio**
2. **Separate voice / music…** → Voice only → Transcribe (clean text, no music bleed)
3. Separate again → Music only → **Beats** → add markers to the music clip
4. **Import voice / music to timeline** to keep the stems in your project

**✦ How to update**
1. Quit Premiere Pro completely.
2. Download the new package from your Gumroad library.
3. Run the installer (**install-mac.command** on Mac, **install-win.bat** on Windows). It replaces both the panel and the backend.
4. Reopen Premiere → Window ▸ Extensions ▸ EasyScript.

⚠ **Important:** update the *whole package*, not only the .zxp. The new separation feature needs the new backend; installing just the panel will show "Separation failed: Not Found".

**✦ Good to know**
- The first Separate run downloads a ~870 MB model (internet needed once, then everything is offline).
- Free disk space: about 9 GB in total if you use every feature (backend ~3 GB + models).
- Updated User Guide (English + Vietnamese PDF) is included.

Questions or feedback? Just reply to this message.

Happy editing!

---

## TIẾNG VIỆT

**Tiêu đề:** EasyScript 2.3 ra mắt — tách giọng và nhạc ngay trong Premiere Pro

Chào mọi người,

EasyScript 2.3 đã có mặt và là bản cập nhật miễn phí cho tất cả khách hàng. Điểm mới lớn nhất: giờ bạn có thể tách bất kỳ clip nào thành **giọng nói** và **nhạc** mà không phải rời Premiere.

**✦ Có gì mới**

**🎙 Tách giọng / nhạc (mới)**
Bấm **Separate voice / music…** rồi chọn:
- **Voice only** — bỏ nhạc, giữ lời. Phiên âm sạch hơn, kể cả video có nhạc nền lớn hoặc bài hát.
- **Music only** — bỏ lời, giữ nhạc. Đầu vào tốt nhất để dò nhịp.

Audio đã tách thay vào sóng âm, phát lại, phiên âm và Beats; thanh **Original | Voice | Music** chuyển qua lại tức thì. Chạy hoàn toàn trên máy bạn bằng model hiện đại (Mel-Band RoFormer) — không upload gì lên mạng.

**📥 Đưa giọng / nhạc đã tách lên timeline (mới)**
Một cú click đặt bản đã tách về đúng timecode gốc, trên track audio đầu tiên còn trống suốt đoạn đó. Không bao giờ đè lên audio có sẵn, bỏ qua track bị khóa, và tự thêm track mới nếu mọi track đều bận. File được lưu trong thư mục **EasyScript Stems** và bin của project.

**🥁 Marker theo nhịp: "Selected clip" (mới)**
Marker giờ có thể gắn thẳng vào clip bạn đang chọn trên timeline (ví dụ clip nhạc vừa import) và di chuyển theo clip. Nếu không chọn clip nào, marker vào clip của audio đang nạp trong panel. Dòng trạng thái cho biết clip nào đã được gắn.

**⚡ Ưu tiên GPU**
Tách giọng / nhạc và phân biệt người nói dùng GPU khi có — Apple Silicon (Metal), NVIDIA (CUDA) hoặc mọi GPU trên Windows (DirectML) — và tự quay về CPU nếu không có. Dòng tiến trình cho biết thiết bị đang dùng.

**✦ Quy trình mẫu**
1. Chọn clip → **Load audio**
2. **Separate voice / music…** → Voice only → Transcribe (chữ sạch, không lẫn nhạc)
3. Tách lại → Music only → **Beats** → gắn marker vào clip nhạc
4. **Import voice / music to timeline** để giữ bản đã tách trong project

**✦ Cách cập nhật**
1. Thoát hẳn Premiere Pro.
2. Tải gói mới từ thư viện Gumroad của bạn.
3. Chạy file cài (**install-mac.command** trên Mac, **install-win.bat** trên Windows). File này thay cả panel lẫn backend.
4. Mở lại Premiere → Window ▸ Extensions ▸ EasyScript.

⚠ **Lưu ý quan trọng:** hãy cập nhật *cả gói*, không chỉ file .zxp. Tính năng tách giọng / nhạc cần backend mới; nếu chỉ cài panel sẽ báo lỗi "Separation failed: Not Found".

**✦ Cần biết**
- Lần đầu tách sẽ tải model ~870 MB (cần mạng một lần, sau đó chạy offline hoàn toàn).
- Dung lượng trống: khoảng 9 GB nếu dùng đủ mọi tính năng (backend ~3 GB + các model).
- Có kèm Hướng dẫn sử dụng bản mới (PDF tiếng Anh + tiếng Việt).

Có thắc mắc hay góp ý? Cứ trả lời trực tiếp tin nhắn này nhé.

Dựng vui nhé!
